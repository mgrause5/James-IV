"""Client for Resy's web API.

This talks to the same endpoints resy.com's own front end uses, authenticated as
you, with your account and your saved card. There is no public/documented API, so
the shapes below are observed rather than contracted -- if Resy reshuffles their
JSON, `Slot.from_find_payload` and `_extract_book_token` are the two places that
will need attention.

Two things this module takes seriously:

1. **Rate limiting.** Every request goes through a token bucket, and a 429 is
   obeyed rather than retried through. Hammering the endpoint is how you get your
   account flagged, and a flagged account books nothing.
2. **Connection reuse.** One HTTP/2 client for the process lifetime, kept warm.
   At a drop, TLS handshake latency is the difference between a table and a
   notification that you missed one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from .models import AuthError, RateLimited, ResyError, Slot, SlotTaken, Venue

log = logging.getLogger("jamesiv.resy")

BASE_URL = "https://api.resy.com"

# Resy's web front end ships a public API key in its JS bundle. It identifies the
# web client, not you -- your identity is the auth token. It changes rarely, but
# if every request starts coming back 401 this is the first thing to re-check:
# open resy.com devtools -> Network -> any api.resy.com request -> the
# `Authorization: ResyAPI api_key="..."` header.
DEFAULT_API_KEY = "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"

# Coordinates sent with availability searches. The web client always sends the
# user's real position and the API turns out to care: lat=0/long=0 -- which this
# client originally sent -- is rejected with an empty HTTP 500 by Resy's edge.
# Discovered by probing production; the simulator happily accepted 0/0.
NYC_LAT = 40.7128
NYC_LONG = -74.0060

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class TokenBucket:
    """Simple async token bucket. Smooths bursts without serialising everything."""

    def __init__(self, rate: float, burst: int):
        self.rate = float(rate)
        self.capacity = float(burst)
        self._tokens = float(burst)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) / self.rate)

    def drain(self) -> None:
        """Spend the whole bucket. Used after a 429 so we back off honestly."""
        self._tokens = 0.0
        self._updated = time.monotonic()


class ResyClient:
    provider = "resy"
    clock_probe_path = "/3/venue"

    def __init__(
        self,
        *,
        api_key: str = DEFAULT_API_KEY,
        rate: float = 4.0,
        burst: int = 8,
        timeout: float = 8.0,
        base_url: str = BASE_URL,
        proxy: str | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._bucket = TokenBucket(rate=rate, burst=burst)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            http2=True,
            proxy=proxy or None,
            timeout=httpx.Timeout(timeout, connect=4.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=20),
            headers={
                "Authorization": f'ResyAPI api_key="{api_key}"',
                "User-Agent": BROWSER_UA,
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://resy.com",
                "Referer": "https://resy.com/",
                "X-Origin": "https://resy.com",
                "Cache-Control": "no-cache",
            },
        )
        self.auth_token: str | None = None
        self.payment_method_id: int | None = None
        self.user_id: int | None = None

    # ---------------------------------------------------------------- plumbing

    @property
    def http(self) -> httpx.AsyncClient:
        return self._client

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> ResyClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    def _auth_headers(self) -> dict[str, str]:
        if not self.auth_token:
            return {}
        return {
            "X-Resy-Auth-Token": self.auth_token,
            "X-Resy-Universal-Auth": self.auth_token,
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        authed: bool = True,
        throttle: bool = True,
        retries: int = 2,
    ) -> httpx.Response:
        if throttle:
            await self._bucket.acquire()

        headers = self._auth_headers() if authed else {}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = await self._client.request(
                    method, path, params=params, data=data, headers=headers
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt >= retries:
                    raise ResyError(f"{method} {path} failed: {exc}") from exc
                await asyncio.sleep(0.4 * (2**attempt))
                continue

            if resp.status_code == 429:
                self._bucket.drain()
                retry_after = _retry_after_seconds(resp, default=30.0)
                raise RateLimited(
                    f"rate limited on {path}",
                    retry_after=retry_after,
                    status=429,
                    body=resp.text[:500],
                )

            if resp.status_code in (401, 419):
                raise AuthError(
                    f"auth rejected on {path} (HTTP {resp.status_code})",
                    status=resp.status_code,
                    body=resp.text[:500],
                )

            if resp.status_code >= 500 and attempt < retries:
                await asyncio.sleep(0.3 * (2**attempt))
                continue

            return resp

        raise ResyError(f"{method} {path} exhausted retries: {last_exc}")

    # ------------------------------------------------------------------- auth

    async def authenticate(self, email: str, password: str) -> None:
        """Log in and cache the auth token plus the default payment method.

        The payment method id is required to book; Resy will not accept a booking
        without it even for venues that take no deposit.
        """
        resp = await self._request(
            "POST",
            "/3/auth/password",
            data={"email": email, "password": password},
            authed=False,
        )
        if resp.status_code != 200:
            raise AuthError(
                "login failed -- check RESY_EMAIL / RESY_PASSWORD",
                status=resp.status_code,
                body=resp.text[:500],
            )

        body = resp.json()
        token = body.get("token")
        if not token:
            raise AuthError("login succeeded but no token in response", body=resp.text[:500])

        self.auth_token = token
        self.user_id = body.get("id")
        self.payment_method_id = _default_payment_method(body)

        if self.payment_method_id is None:
            log.warning(
                "No saved payment method found on your Resy account. Most hard "
                "venues require one to book -- add a card at resy.com/account."
            )
        log.info("Authenticated with Resy as user %s", self.user_id)

    def set_token(self, token: str, payment_method_id: int | None = None) -> None:
        """Use a token captured from the browser instead of email/password."""
        self.auth_token = token
        if payment_method_id is not None:
            self.payment_method_id = payment_method_id

    # ----------------------------------------------------------------- venues

    async def venue_raw(self, slug: str, *, location: str = "ny") -> dict[str, Any]:
        """The full `/3/venue` payload. Besides the id, this carries the venue's
        booking-policy prose and (often) a structured lead-time field -- see
        `policy.extract_policy`."""
        resp = await self._request(
            "GET", "/3/venue", params={"url_slug": slug, "location": location}
        )
        if resp.status_code == 404:
            raise ResyError(f"no venue with slug {slug!r} in location {location!r}")
        if resp.status_code != 200:
            raise ResyError(f"venue lookup failed for {slug!r}", status=resp.status_code)
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise ResyError(f"venue lookup for {slug!r} returned non-JSON") from exc

    async def venue_by_slug(self, slug: str, *, location: str = "ny") -> Venue:
        body = await self.venue_raw(slug, location=location)
        venue_id = (body.get("id") or {}).get("resy")
        if venue_id is None:
            raise ResyError(f"venue lookup for {slug!r} returned no id")
        return Venue(id=int(venue_id), name=str(body.get("name") or slug), slug=slug)

    # ------------------------------------------------------------------ slots

    async def find(
        self,
        *,
        venue_id: int,
        day: str,
        party_size: int,
        venue_slug: str | None = None,  # accepted for provider parity; unused
        throttle: bool = True,
        retries: int = 2,
    ) -> list[Slot]:
        """Return every bookable slot at a venue on a date. The hot path.

        Production `/4/find` also fails intermittently with an empty 500 even on
        well-formed requests (observed live; likely edge/bot mitigation). The
        default retries absorb that in the poll loop; the burst passes
        `retries=0` because its own loop re-fires faster than a backoff would.
        """
        resp = await self._request(
            "GET",
            "/4/find",
            params={
                "lat": NYC_LAT,
                "long": NYC_LONG,
                "day": day,
                "party_size": party_size,
                "venue_id": venue_id,
            },
            throttle=throttle,
            retries=retries,
        )
        if resp.status_code >= 500:
            # Resy's edge intermittently rejects /4/find with an empty 500 --
            # and when an IP is being throttled it does so persistently. That
            # must surface as an error: a blocked bot silently returning "no
            # availability" is indistinguishable from a full restaurant, which
            # is the one failure mode an unattended hunter cannot afford.
            raise ResyError(
                f"availability search rejected (HTTP {resp.status_code}) -- "
                "Resy's edge may be throttling this network",
                status=resp.status_code,
            )
        if resp.status_code != 200:
            log.debug("find returned HTTP %s: %s", resp.status_code, resp.text[:200])
            return []

        try:
            venues = (resp.json().get("results") or {}).get("venues") or []
        except json.JSONDecodeError:
            return []

        slots: list[Slot] = []
        for venue in venues:
            for raw in venue.get("slots") or []:
                slot = Slot.from_find_payload(raw, venue_id=venue_id, party_size=party_size)
                if slot is not None:
                    slots.append(slot)
        slots.sort(key=lambda s: s.start)
        return slots

    # ---------------------------------------------------------------- booking

    async def book_token_for(self, slot: Slot) -> str:
        """Exchange a slot config id for a short-lived book token.

        The token expires fast (seconds to a couple of minutes), so this is
        deliberately not cached -- fetch it immediately before booking.
        """
        resp = await self._request(
            "POST",
            "/3/details",
            data={
                "config_id": slot.config_id,
                "day": slot.day.isoformat(),
                "party_size": slot.party_size,
            },
            throttle=False,
        )
        if resp.status_code in (410, 412):
            raise SlotTaken(f"slot gone before details: {slot}", status=resp.status_code)
        if resp.status_code != 200:
            raise ResyError(
                f"details failed for {slot}", status=resp.status_code, body=resp.text[:400]
            )
        return _extract_book_token(resp.json(), slot)

    async def book(self, slot: Slot, book_token: str) -> tuple[str, str | None]:
        """Commit the booking. Returns (resy_token, reservation_id).

        This is the irreversible one: on success you own a reservation with a
        real cancellation policy attached.
        """
        data: dict[str, Any] = {"book_token": book_token}
        if self.payment_method_id is not None:
            data["struct_payment_method"] = json.dumps({"id": self.payment_method_id})
        data["source_id"] = "resy.com-frontend-web"

        resp = await self._request("POST", "/3/book", data=data, throttle=False, retries=0)

        if resp.status_code in (409, 410, 412):
            raise SlotTaken(f"beaten to {slot}", status=resp.status_code, body=resp.text[:300])
        if resp.status_code not in (200, 201):
            raise ResyError(
                f"booking failed for {slot}", status=resp.status_code, body=resp.text[:400]
            )

        body = resp.json()
        resy_token = body.get("resy_token")
        if not resy_token:
            raise ResyError(f"booking response had no resy_token for {slot}", body=resp.text[:300])
        return str(resy_token), _stringify(body.get("reservation_id"))

    async def cancel(self, resy_token: str) -> bool:
        resp = await self._request("POST", "/3/cancel", data={"resy_token": resy_token})
        return resp.status_code in (200, 201, 204)

    async def warm(self) -> None:
        """Establish the TLS connection and HTTP/2 session ahead of a drop.

        Cheap, and it moves ~100-300ms of handshake off the critical path.
        """
        try:
            await self._client.head("/3/venue", params={"url_slug": "resy", "location": "ny"})
        except Exception as exc:  # warming is best-effort by definition
            log.debug("connection warm failed (harmless): %s", exc)


# ------------------------------------------------------------------- helpers


def _retry_after_seconds(resp: httpx.Response, *, default: float) -> float:
    raw = resp.headers.get("retry-after")
    if not raw:
        return default
    try:
        return max(1.0, float(raw))
    except ValueError:
        return default


def _default_payment_method(body: dict[str, Any]) -> int | None:
    methods = body.get("payment_methods") or []
    if not isinstance(methods, list) or not methods:
        return None
    for method in methods:
        if method.get("is_default"):
            return _as_int(method.get("id"))
    return _as_int(methods[0].get("id"))


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _stringify(value: Any) -> str | None:
    return None if value is None else str(value)


def _extract_book_token(body: dict[str, Any], slot: Slot) -> str:
    token_block = body.get("book_token")
    if isinstance(token_block, dict):
        value = token_block.get("value")
        if value:
            return str(value)
    if isinstance(token_block, str) and token_block:
        return token_block
    raise ResyError(f"no book_token in details response for {slot}")
