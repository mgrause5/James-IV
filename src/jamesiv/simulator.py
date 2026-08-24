"""A simulated Resy, for back-testing the bot against realistic dynamics.

Ships with the package rather than living in the test suite, because the most
useful thing you can do with it is point it at *your own config* -- see
`james simulate` -- and watch a drop play out end to end before you trust the
thing with a real card.

This is not a mock in the usual sense -- it is a small stateful server that
models the things that actually happen at a drop: inventory appearing at an
instant, competitors taking tables out from under you, holds expiring and
tables bouncing back, sessions going stale, and rate limits.

The real `Hunter` and the real `ResyClient` run against it unmodified, over the
real event loop with real timing, so what is under test is the actual code path
that will run at 9am -- not a paraphrase of it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import httpx

from .resy import BASE_URL
from .timeutil import NYC, now_nyc

VENUE_ID = 4242
VENUE_NAME = "Simulated Torrisi"


@dataclass
class SimSlot:
    day: date
    start: datetime
    seating_type: str = "Dining Room"
    party_size: int = 2
    taken: bool = False
    # Wall-clock (monotonic) instant this slot becomes visible. None = always.
    visible_at: float | None = None
    # Number of book attempts that will lose the race before one succeeds.
    # Models competitors holding the table ahead of you.
    contested: int = 0

    @property
    def config_id(self) -> str:
        return f"cfg-{self.day}-{self.start:%H%M}-{self.seating_type}-{self.party_size}"

    def visible(self) -> bool:
        if self.taken:
            return False
        return self.visible_at is None or time.monotonic() >= self.visible_at

    def to_payload(self) -> dict:
        return {
            "config": {"token": self.config_id, "type": self.seating_type},
            "date": {
                "start": self.start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": (self.start + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
            },
            "size": {"min": self.party_size, "max": self.party_size + 2},
        }


@dataclass
class SimResy:
    """Stateful fake Resy. Register with `with sim.mock():`."""

    slots: list[SimSlot] = field(default_factory=list)
    # Extra keys merged into the /3/venue response -- e.g. a booking-policy
    # blurb, for exercising drop-policy discovery.
    venue_extra: dict = field(default_factory=dict)
    # Session control
    valid_token: str = "sim-token-1"
    expire_after_finds: int | None = None
    # Rate limit control: 429 the Nth find call (1-indexed), once.
    rate_limit_on_find: int | None = None
    # Force every find to return this status. Models Resy's edge throttling
    # /4/find with empty 500s, as observed against production.
    find_status_override: int | None = None
    # Counters, for assertions
    find_calls: int = 0
    book_calls: int = 0
    details_calls: int = 0
    login_calls: int = 0
    booked: list[SimSlot] = field(default_factory=list)
    _token_serial: int = 1

    # ------------------------------------------------------------- authoring

    def add(self, *slots: SimSlot) -> None:
        self.slots.extend(slots)

    def release_in(self, seconds: float, *slots: SimSlot) -> None:
        """Make slots appear this many seconds from now. Models a drop."""
        at = time.monotonic() + seconds
        for slot in slots:
            slot.visible_at = at
        self.slots.extend(slots)

    def expire_session(self) -> None:
        """Invalidate the current token, as Resy does on a stale session."""
        self._token_serial += 1
        self.valid_token = f"sim-token-{self._token_serial}"

    # -------------------------------------------------------------- handlers

    def _authed(self, request: httpx.Request) -> bool:
        return request.headers.get("x-resy-auth-token") == self.valid_token

    def _handle_login(self, request: httpx.Request) -> httpx.Response:
        self.login_calls += 1
        return httpx.Response(
            200,
            json={
                "token": self.valid_token,
                "id": 999,
                "payment_methods": [{"id": 777, "is_default": True}],
            },
        )

    def _handle_venue(self, request: httpx.Request) -> httpx.Response:
        if not self._authed(request):
            return httpx.Response(419)
        payload = {"id": {"resy": VENUE_ID}, "name": VENUE_NAME, **self.venue_extra}
        return httpx.Response(200, json=payload)

    def _handle_head(self, request: httpx.Request) -> httpx.Response:
        stamp = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
        return httpx.Response(200, headers={"Date": stamp})

    def _handle_find(self, request: httpx.Request) -> httpx.Response:
        self.find_calls += 1

        if self.find_status_override is not None:
            return httpx.Response(self.find_status_override)

        if self.rate_limit_on_find == self.find_calls:
            return httpx.Response(429, headers={"Retry-After": "1"})

        if self.expire_after_finds is not None and self.find_calls > self.expire_after_finds:
            if not self._authed(request):
                return httpx.Response(419)
        if not self._authed(request):
            return httpx.Response(419)

        params = request.url.params
        day = params.get("day")
        party = int(params.get("party_size", 2))

        visible = [
            s for s in self.slots
            if s.visible() and s.day.isoformat() == day and s.party_size == party
        ]
        return httpx.Response(
            200,
            json={"results": {"venues": [{"slots": [s.to_payload() for s in visible]}]}},
        )

    def _slot_by_config(self, config_id: str) -> SimSlot | None:
        return next((s for s in self.slots if s.config_id == config_id), None)

    def _handle_details(self, request: httpx.Request) -> httpx.Response:
        self.details_calls += 1
        if not self._authed(request):
            return httpx.Response(419)

        config_id = _form(request).get("config_id", "")
        slot = self._slot_by_config(config_id)
        if slot is None or slot.taken:
            return httpx.Response(412)
        return httpx.Response(200, json={"book_token": {"value": f"bt::{config_id}"}})

    def _handle_book(self, request: httpx.Request) -> httpx.Response:
        self.book_calls += 1
        if not self._authed(request):
            return httpx.Response(419)

        token = _form(request).get("book_token", "")
        config_id = token.removeprefix("bt::")
        slot = self._slot_by_config(config_id)

        if slot is None or slot.taken:
            return httpx.Response(409)
        if slot.contested > 0:
            # A competitor is holding it. Their hold will lapse; try again.
            slot.contested -= 1
            return httpx.Response(409)

        slot.taken = True
        self.booked.append(slot)
        return httpx.Response(
            201,
            json={"resy_token": f"rt::{config_id}", "reservation_id": 10_000 + len(self.booked)},
        )

    def _handle_cancel(self, request: httpx.Request) -> httpx.Response:
        token = _form(request).get("resy_token", "").removeprefix("rt::")
        slot = self._slot_by_config(token)
        if slot is not None:
            slot.taken = False
        return httpx.Response(200, json={})

    # ------------------------------------------------------------ registration

    def mock(self):
        """Register the fake endpoints. Requires respx (`pip install respx`)."""
        try:
            import respx
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise RuntimeError(
                "The simulator needs respx. Install it with:\n"
                "    pip install respx\n"
                'or install the package with its dev extras: pip install -e ".[dev]"'
            ) from exc

        router = respx.mock(assert_all_called=False)
        router.post(f"{BASE_URL}/3/auth/password").mock(side_effect=self._handle_login)
        router.head(f"{BASE_URL}/3/venue").mock(side_effect=self._handle_head)
        router.get(f"{BASE_URL}/3/venue").mock(side_effect=self._handle_venue)
        router.get(f"{BASE_URL}/4/find").mock(side_effect=self._handle_find)
        router.post(f"{BASE_URL}/3/details").mock(side_effect=self._handle_details)
        router.post(f"{BASE_URL}/3/book").mock(side_effect=self._handle_book)
        router.post(f"{BASE_URL}/3/cancel").mock(side_effect=self._handle_cancel)
        return router


def _form(request: httpx.Request) -> dict[str, str]:
    from urllib.parse import parse_qs

    parsed = parse_qs(request.content.decode())
    return {k: v[0] for k, v in parsed.items()}


# ----------------------------------------------------------------- shorthands


def slot_at(days_out: int, hhmm: str, *, seating="Dining Room", party=2, contested=0) -> SimSlot:
    """A slot N days from today at a given NYC wall-clock time."""
    hour, minute = (int(x) for x in hhmm.split(":"))
    day = now_nyc().date() + timedelta(days=days_out)
    return SimSlot(
        day=day,
        start=datetime(day.year, day.month, day.day, hour, minute, tzinfo=NYC),
        seating_type=seating,
        party_size=party,
        contested=contested,
    )


def slot_relative(minutes_from_now: int, *, seating="Dining Room", party=2) -> SimSlot:
    """A slot a fixed number of minutes from right now. For lead-time tests."""
    start = (now_nyc() + timedelta(minutes=minutes_from_now)).replace(second=0, microsecond=0)
    return SimSlot(
        day=start.date(), start=start, seating_type=seating, party_size=party
    )
