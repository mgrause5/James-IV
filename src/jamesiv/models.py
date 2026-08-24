"""Domain objects: what a bookable table looks like once Resy's JSON is tamed."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .timeutil import parse_slot_datetime


@dataclass(slots=True, frozen=True)
class Slot:
    """One bookable seating on one date."""

    config_id: str
    start: datetime
    seating_type: str
    venue_id: int
    day: date
    party_size: int
    min_size: int = 0
    max_size: int = 0
    raw: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @property
    def clock(self) -> str:
        return self.start.strftime("%-I:%M %p")

    @property
    def key(self) -> str:
        """Stable identity for dedupe across polls."""
        return f"{self.venue_id}:{self.day.isoformat()}:{self.start:%H%M}:{self.seating_type}"

    def __str__(self) -> str:
        return (
            f"{self.day:%a %b %-d} {self.clock} · {self.seating_type} "
            f"· party of {self.party_size}"
        )

    @classmethod
    def from_find_payload(
        cls, slot: dict[str, Any], *, venue_id: int, party_size: int
    ) -> Slot | None:
        """Build a Slot from one entry of `/4/find` -> venues[].slots[].

        Returns None for anything we cannot book: Resy mixes real inventory in
        with waitlist stubs and unbookable placeholders, and the distinguishing
        feature is simply whether a config token is present.
        """
        config = slot.get("config") or {}
        token = config.get("token")
        if not token:
            return None

        date_block = slot.get("date") or {}
        start_raw = date_block.get("start")
        if not start_raw:
            return None
        try:
            start = parse_slot_datetime(start_raw)
        except ValueError:
            return None

        size = slot.get("size") or {}
        return cls(
            config_id=str(token),
            start=start,
            seating_type=str(config.get("type") or "Unknown").strip(),
            venue_id=venue_id,
            day=start.date(),
            party_size=party_size,
            min_size=int(size.get("min") or 0),
            max_size=int(size.get("max") or 0),
            raw=slot,
        )


@dataclass(slots=True)
class Venue:
    id: int
    name: str
    slug: str

    def __str__(self) -> str:
        return f"{self.name} (#{self.id})"


@dataclass(slots=True)
class Booking:
    """A confirmed reservation."""

    resy_token: str
    reservation_id: str | None
    slot: Slot
    target_name: str
    booked_at: datetime

    def __str__(self) -> str:
        return f"{self.target_name}: {self.slot}"


class ResyError(RuntimeError):
    """Any non-recoverable failure talking to Resy."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class SlotTaken(ResyError):
    """Someone beat us to it. Expected, common, not worth an alert."""


class AuthError(ResyError):
    """Credentials rejected or token expired."""


class RateLimited(ResyError):
    """Resy asked us to slow down. Always honour this."""

    def __init__(self, message: str, *, retry_after: float = 30.0, **kwargs):
        super().__init__(message, **kwargs)
        self.retry_after = retry_after
