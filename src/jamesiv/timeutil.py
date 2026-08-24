"""Time helpers.

Everything user-facing is in America/New_York, because every venue this bot
cares about is. Everything internal is UTC.

The interesting part of this module is `ClockSync`. Resy drops inventory on a
wall-clock boundary (9:00:00 AM ET, typically). If your machine's clock is 400ms
slow you fire late and lose; if it is 400ms fast you fire early, get an empty
result set, and burn your first burst on nothing. So we do not trust the local
clock -- we measure the offset against Resy's own servers and correct for it.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

NYC = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def now_nyc() -> datetime:
    return datetime.now(tz=NYC)


def today_nyc() -> date:
    return now_nyc().date()


def nyc_at(day: date, t: dtime) -> datetime:
    """Combine a date and a wall-clock time into an aware NYC datetime.

    Uses fold=0, so on the ambiguous 1-2am DST fallback hour this resolves to
    the first (EDT) occurrence. No restaurant drops inventory at 1:30am, so this
    is a documented shrug rather than a real decision.
    """
    return datetime.combine(day, t, tzinfo=NYC)


def parse_hhmm(value: str) -> dtime:
    """Parse '19:30' or '19:30:00' into a time."""
    parts = value.strip().split(":")
    if len(parts) == 2:
        h, m, s = int(parts[0]), int(parts[1]), 0
    elif len(parts) == 3:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    else:
        raise ValueError(f"expected HH:MM or HH:MM:SS, got {value!r}")
    return dtime(hour=h, minute=m, second=s)


def next_occurrence_nyc(t: dtime, now: datetime, *, grace_seconds: float = 60.0) -> datetime:
    """The next time the NYC wall clock reads `t`, seen from `now`.

    A drop whose instant passed less than `grace_seconds` ago still counts as
    "now": a bot that wakes slightly late should fire into the tail of the
    release, not write the whole day off. Beyond the grace it rolls to
    tomorrow.

    This exists because "today at HH:MM" is wrong twice a day in exactly the
    ways that lose reservations: woken 75s before a midnight drop, "today" is
    still yesterday; woken just after a 10am drop, "today at 10" is in the
    past. All drop scheduling must go through here rather than combining
    `today` with a wall-clock time by hand.
    """
    candidate = nyc_at(now.astimezone(NYC).date(), t)
    if candidate < now - timedelta(seconds=grace_seconds):
        candidate = nyc_at(candidate.date() + timedelta(days=1), t)
    return candidate


def parse_slot_datetime(value: str) -> datetime:
    """Resy returns slot times as naive 'YYYY-MM-DD HH:MM:SS' in venue-local time."""
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=NYC)


@dataclass(slots=True)
class ClockOffset:
    """How far ahead of the server our local clock is, in seconds.

    A positive `offset` means the local clock reads later than the server's.
    To convert a local monotonic-ish reading to server time: local - offset.
    """

    offset: float
    uncertainty: float
    samples: int

    @property
    def is_trustworthy(self) -> bool:
        return self.samples > 0 and self.uncertainty < 0.5

    def server_now(self) -> datetime:
        return now_utc() - timedelta(seconds=self.offset)

    def local_time_for_server_time(self, server_target: datetime) -> datetime:
        """The local-clock instant at which the server will read `server_target`."""
        return server_target + timedelta(seconds=self.offset)


ZERO_OFFSET = ClockOffset(offset=0.0, uncertainty=0.0, samples=0)


async def measure_clock_offset(client, url: str, probes: int = 12) -> ClockOffset:
    """Estimate local-vs-server clock skew from HTTP `Date` headers.

    The `Date` header only has one-second resolution, which sounds useless for
    sub-second work. The trick is that we do not care about the value, we care
    about the *transition*: the instant the header flips from N to N+1 is a hard
    server-side second boundary. Sampling across a boundary pins us to roughly
    the sampling interval rather than to a full second.

    We keep the probe count low and the requests cheap on purpose. This runs
    once before a drop, not in a loop.
    """
    samples: list[tuple[float, float]] = []  # (local_midpoint_epoch, server_epoch)

    for _ in range(probes):
        t0 = time.time()
        try:
            resp = await client.head(url)
        except Exception:
            await asyncio.sleep(0.15)
            continue
        t1 = time.time()

        raw = resp.headers.get("date")
        if not raw:
            await asyncio.sleep(0.15)
            continue
        try:
            server_dt = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            continue
        if server_dt.tzinfo is None:
            server_dt = server_dt.replace(tzinfo=UTC)

        # The header was stamped somewhere inside the request; the midpoint is
        # the least-wrong single guess, and the round trip bounds our error.
        samples.append(((t0 + t1) / 2.0, server_dt.timestamp()))
        await asyncio.sleep(0.15)

    if not samples:
        return ZERO_OFFSET

    # The server stamped `server_epoch` at some unknown point within that second,
    # so the true server time at sampling was in [server_epoch, server_epoch + 1).
    # Each sample therefore bounds the offset:
    #   local - server_epoch - 1 < offset <= local - server_epoch
    lower = max(local - server - 1.0 for local, server in samples)
    upper = min(local - server for local, server in samples)

    if lower > upper:
        # Contradictory bounds: the clock moved under us, or a proxy is rewriting
        # Date headers. Fall back to the naive midpoint rather than pretending.
        naive = sum(local - server - 0.5 for local, server in samples) / len(samples)
        return ClockOffset(offset=naive, uncertainty=1.0, samples=len(samples))

    return ClockOffset(
        offset=(lower + upper) / 2.0,
        uncertainty=(upper - lower) / 2.0,
        samples=len(samples),
    )


async def sleep_until(target_local: datetime, *, spin_window: float = 0.05) -> None:
    """Sleep until a local-clock instant, then busy-wait the last few ms.

    `asyncio.sleep` is accurate to maybe 1-15ms depending on the event loop and
    how loaded the box is. That is usually fine, but the last stretch before a
    drop is the one moment it is worth burning CPU to be exact.
    """
    while True:
        remaining = (target_local - now_utc()).total_seconds()
        if remaining <= spin_window:
            break
        await asyncio.sleep(min(remaining - spin_window, 30.0))

    deadline = time.time() + max(0.0, (target_local - now_utc()).total_seconds())
    while time.time() < deadline:
        await asyncio.sleep(0)


def humanize_delta(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"
