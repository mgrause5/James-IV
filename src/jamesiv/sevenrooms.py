"""Client for SevenRooms -- the reservation system behind DoorDash bookings.

DoorDash acquired SevenRooms in 2025; when a restaurant "books through
DoorDash", the widget on its site and the DoorDash app both drive this API.
The availability endpoint below was verified live against The Corner Store
and The Eighty Six (Aug 2026); its shape:

    GET /api-yoa/availability/widget/range?venue=<slug>&...
    -> data.availability.<YYYY-MM-DD>: [shift, ...]
       shift.times[]: {type: "book"|"request", time_iso, access_persistent_id}

Only `type: "book"` entries are real inventory. `"request"` entries are a
request-queue form that exists for every time slot of every day -- treating
them as tables would notify the owner about nothing, forever, so they are
filtered here at the parse boundary.

The booking flow (hold -> complete with guest details) follows the widget's
observed behaviour but, like Resy's, touches real inventory and is therefore
verified only by the owner's first deliberate booking. Venues can also require
a captcha at completion; when that happens the failure surfaces as an urgent
notification with a deep link rather than a silent miss.

Errors reuse the same taxonomy as the Resy client (ResyError and friends from
models.py) so the Hunter's recovery, blindness alarm, and burst logic work
unchanged across providers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import zlib
from datetime import datetime
from typing import Any

import httpx

from .models import RateLimited, ResyError, Slot, SlotTaken
from .resy import BROWSER_UA, TokenBucket, _retry_after_seconds
from .timeutil import NYC

log = logging.getLogger("jamesiv.sevenrooms")

BASE_URL = "https://www.sevenrooms.com"
CHANNEL = "SEVENROOMS_WIDGET"


def venue_key(slug: str) -> int:
    """Stable synthetic venue id for a SevenRooms slug.

    Slot identity (dedupe keys, same-night guards) is keyed on an integer
    venue id everywhere; a CRC of the slug gives SevenRooms venues distinct,
    deterministic ids without touching the Slot schema.
    """
    return zlib.crc32(slug.encode()) & 0x7FFFFFFF


class SevenRoomsClient:
    provider = "sevenrooms"
    clock_probe_path = "/"

    def __init__(
        self,
        *,
        rate: float = 4.0,
        burst: int = 8,
        timeout: float = 8.0,
        base_url: str = BASE_URL,
        guest: dict[str, str] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.guest = guest or {}
        self._bucket = TokenBucket(rate=rate, burst=burst)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            http2=True,
            timeout=httpx.Timeout(timeout, connect=4.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=20),
            headers={
                "User-Agent": BROWSER_UA,
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.sevenrooms.com/",
            },
        )

    @property
    def http(self) -> httpx.AsyncClient:
        return self._client

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        throttle: bool = True,
        retries: int = 2,
    ) -> httpx.Response:
        if throttle:
            await self._bucket.acquire()
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = await self._client.request(method, path, params=params, data=data)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt >= retries:
                    raise ResyError(f"{method} {path} failed: {exc}") from exc
                await asyncio.sleep(0.4 * (2**attempt))
                continue

            if resp.status_code == 429:
                self._bucket.drain()
                raise RateLimited(
                    f"rate limited on {path}",
                    retry_after=_retry_after_seconds(resp, default=30.0),
                    status=429,
                )
            if resp.status_code >= 500 and attempt < retries:
                await asyncio.sleep(0.3 * (2**attempt))
                continue
            return resp
        raise ResyError(f"{method} {path} exhausted retries: {last_exc}")

    # ------------------------------------------------------------------ slots

    async def find(
        self,
        *,
        venue_id: int,
        day: str,
        party_size: int,
        venue_slug: str | None = None,
        throttle: bool = True,
        retries: int = 2,
    ) -> list[Slot]:
        """Bookable slots at a venue on a date. Mirrors ResyClient.find.

        `venue_slug` is required here (SevenRooms is slug-keyed); `venue_id`
        is the synthetic CRC id used for slot identity.
        """
        if not venue_slug:
            raise ResyError("sevenrooms find requires venue_slug")

        # The API wants MM-DD-YYYY.
        m, d, y = day[5:7], day[8:10], day[0:4]
        resp = await self._request(
            "GET",
            "/api-yoa/availability/widget/range",
            params={
                "venue": venue_slug,
                "time_slot": "19:00",
                "party_size": party_size,
                "halo_size_interval": 16,
                "start_date": f"{m}-{d}-{y}",
                "num_days": 1,
                "channel": CHANNEL,
            },
            throttle=throttle,
            retries=retries,
        )
        if resp.status_code >= 500:
            raise ResyError(
                f"availability search rejected (HTTP {resp.status_code})",
                status=resp.status_code,
            )
        if resp.status_code != 200:
            log.debug("availability HTTP %s for %s: %s",
                      resp.status_code, venue_slug, resp.text[:200])
            return []

        try:
            days = (resp.json().get("data") or {}).get("availability") or {}
        except json.JSONDecodeError:
            return []

        slots: list[Slot] = []
        for shifts in days.values():
            for shift in shifts or []:
                for entry in shift.get("times") or []:
                    slot = self._parse_time(entry, venue_id, venue_slug, party_size)
                    if slot is not None:
                        slots.append(slot)
        slots.sort(key=lambda s: s.start)
        return slots

    @staticmethod
    def _parse_time(
        entry: dict[str, Any], venue_id: int, venue_slug: str, party_size: int
    ) -> Slot | None:
        # "request" rows exist for every slot of every day; only "book" rows
        # with an access id are actual inventory.
        if entry.get("type") != "book":
            return None
        access_id = entry.get("access_persistent_id")
        time_iso = entry.get("time_iso")
        if not access_id or not time_iso:
            return None
        try:
            start = datetime.strptime(time_iso, "%Y-%m-%d %H:%M:%S").replace(tzinfo=NYC)
        except ValueError:
            return None
        return Slot(
            config_id=f"{venue_slug}|{access_id}|{time_iso}",
            start=start,
            seating_type=str(entry.get("public_description_title")
                             or entry.get("shift_category") or "Dining").strip() or "Dining",
            venue_id=venue_id,
            day=start.date(),
            party_size=party_size,
            raw=entry,
        )

    # ---------------------------------------------------------------- booking

    def _require_guest(self) -> None:
        missing = [k for k in ("first_name", "last_name", "email", "phone")
                   if not self.guest.get(k)]
        if missing:
            raise ResyError(
                "sevenrooms booking needs guest details in .env: "
                + ", ".join(f"GUEST_{m.upper()}" for m in missing)
            )

    async def book_token_for(self, slot: Slot) -> str:
        """Place a hold on the slot. Returns the hold id.

        A SevenRooms hold locks the table while checkout completes -- unlike
        Resy, winning the hold wins the race. Guest details are checked BEFORE
        holding: a hold we can never complete would lock a table away from its
        rightful next taker for nothing.
        """
        self._require_guest()
        venue_slug, access_id, time_iso = slot.config_id.split("|", 2)
        day = time_iso[:10]
        clock = slot.start.strftime("%-I:%M %p")
        resp = await self._request(
            "POST",
            "/api-yoa/reservations/hold",
            data={
                "venue": venue_slug,
                "access_persistent_id": access_id,
                "date": f"{day[5:7]}-{day[8:10]}-{day[0:4]}",
                "time_slot": clock,
                "party_size": slot.party_size,
                "channel": CHANNEL,
            },
            throttle=False,
            retries=0,
        )
        if resp.status_code in (404, 409, 410, 412, 422):
            raise SlotTaken(f"hold refused for {slot}", status=resp.status_code,
                            body=resp.text[:300])
        if resp.status_code not in (200, 201):
            raise ResyError(f"hold failed for {slot}", status=resp.status_code,
                            body=resp.text[:400])
        body = resp.json()
        hold_id = ((body.get("data") or {}).get("hold_id")
                   or (body.get("data") or {}).get("id") or body.get("hold_id"))
        if not hold_id:
            raise ResyError(f"hold response had no id for {slot}", body=resp.text[:300])
        return str(hold_id)

    async def book(self, slot: Slot, book_token: str) -> tuple[str, str | None]:
        """Complete the held reservation with the configured guest details."""
        self._require_guest()
        venue_slug, _access_id, _ = slot.config_id.split("|", 2)
        resp = await self._request(
            "POST",
            "/api-yoa/reservations",
            data={
                "venue": venue_slug,
                "hold_id": book_token,
                "first_name": self.guest["first_name"],
                "last_name": self.guest["last_name"],
                "email": self.guest["email"],
                "phone_number": self.guest["phone"],
                "channel": CHANNEL,
                "agreed_to_venue_policies": "true",
            },
            throttle=False,
            retries=0,
        )
        if resp.status_code in (409, 410, 412):
            raise SlotTaken(f"beaten to {slot}", status=resp.status_code)
        if resp.status_code not in (200, 201):
            raise ResyError(
                f"booking completion failed for {slot} (HTTP {resp.status_code}) -- "
                "the venue may require a captcha, which cannot be automated; "
                "book by hand from the alert link",
                status=resp.status_code,
                body=resp.text[:400],
            )
        body = resp.json()
        data = body.get("data") or {}
        ref = data.get("reservation_id") or data.get("id") or data.get("reference_code")
        if not ref:
            raise ResyError(f"booking response had no reference for {slot}",
                            body=resp.text[:300])
        return str(ref), str(ref)

    async def cancel(self, resy_token: str) -> bool:
        # Widget-made reservations are cancelled via the emailed link; there is
        # no stable anonymous cancel API. Be honest rather than pretend.
        log.warning("SevenRooms reservations are cancelled from the confirmation "
                    "email, not the bot: %s", resy_token)
        return False

    async def warm(self) -> None:
        try:
            await self._client.head("/")
        except Exception as exc:
            log.debug("sevenrooms warm failed (harmless): %s", exc)


def deep_link(slug: str) -> str:
    return f"https://www.sevenrooms.com/reservations/{slug}"
