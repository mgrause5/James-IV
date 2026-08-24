"""Clock sync and time parsing -- the machinery drop-sniping accuracy rests on."""

from __future__ import annotations

import time
from datetime import date, datetime
from datetime import time as dtime

import httpx
import pytest
import respx

from jamesiv.timeutil import (
    NYC,
    ClockOffset,
    measure_clock_offset,
    nyc_at,
    parse_hhmm,
    parse_slot_datetime,
)


class TestParsing:
    def test_hhmm_and_hhmmss(self):
        assert parse_hhmm("09:00") == dtime(9, 0)
        assert parse_hhmm("09:00:30") == dtime(9, 0, 30)

    def test_rejects_garbage(self):
        with pytest.raises(ValueError):
            parse_hhmm("9am")

    def test_slot_datetimes_are_interpreted_as_venue_local(self):
        parsed = parse_slot_datetime("2026-09-23 19:00:00")
        assert parsed.tzinfo is NYC
        assert (parsed.hour, parsed.minute) == (19, 0)

    def test_nyc_at_produces_an_aware_datetime(self):
        combined = nyc_at(date(2026, 9, 23), dtime(9, 0))
        assert combined.tzinfo is NYC
        assert combined.utcoffset().total_seconds() == -4 * 3600  # EDT in September


class TestClockOffset:
    def test_offset_shifts_server_time(self):
        # Local clock reads 2s later than the server's.
        offset = ClockOffset(offset=2.0, uncertainty=0.05, samples=8)
        target = datetime(2026, 9, 23, 13, 0, 0, tzinfo=NYC)
        fire_at = offset.local_time_for_server_time(target)
        assert (fire_at - target).total_seconds() == 2.0

    def test_trustworthiness_requires_samples_and_tight_bounds(self):
        assert ClockOffset(0.1, 0.05, 8).is_trustworthy
        assert not ClockOffset(0.1, 0.9, 8).is_trustworthy
        assert not ClockOffset(0.0, 0.0, 0).is_trustworthy


class TestMeasureClockOffset:
    @respx.mock
    async def test_recovers_a_known_skew_from_date_headers(self):
        # Pretend the server is exactly 5s behind our local clock.
        skew = 5.0

        def responder(request):
            server_epoch = time.time() - skew
            stamp = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(server_epoch))
            return httpx.Response(200, headers={"Date": stamp})

        respx.head("https://api.resy.com/3/venue").mock(side_effect=responder)

        async with httpx.AsyncClient(base_url="https://api.resy.com") as client:
            offset = await measure_clock_offset(client, "/3/venue", probes=8)

        assert offset.samples == 8
        # Date headers truncate to the second, so we can only pin this to ~1s.
        assert abs(offset.offset - skew) < 1.0
        assert offset.uncertainty < 1.0

    @respx.mock
    async def test_returns_zero_offset_when_every_probe_fails(self):
        respx.head("https://api.resy.com/3/venue").mock(
            side_effect=httpx.ConnectError("nope")
        )
        async with httpx.AsyncClient(base_url="https://api.resy.com") as client:
            offset = await measure_clock_offset(client, "/3/venue", probes=3)

        assert offset.samples == 0
        assert offset.offset == 0.0
        assert not offset.is_trustworthy

    @respx.mock
    async def test_missing_date_header_is_skipped_not_fatal(self):
        respx.head("https://api.resy.com/3/venue").mock(return_value=httpx.Response(200))
        async with httpx.AsyncClient(base_url="https://api.resy.com") as client:
            offset = await measure_clock_offset(client, "/3/venue", probes=3)
        assert offset.samples == 0


class TestNextOccurrence:
    """REGRESSION: 'today at HH:MM' was assembled by hand in three places, and
    it is wrong twice a day in exactly the ways that lose reservations."""

    def test_a_future_time_today_is_today(self):
        now = datetime(2026, 8, 24, 8, 0, tzinfo=NYC)
        from jamesiv.timeutil import next_occurrence_nyc
        assert next_occurrence_nyc(dtime(10, 0), now) == datetime(2026, 8, 24, 10, 0, tzinfo=NYC)

    def test_midnight_drop_seen_from_the_evening_before_is_tonight(self):
        # The scheduler wakes 75s early: at 23:58:45 the "next midnight" must be
        # 76 seconds away, not 24 hours ago. This exact case silently killed
        # every midnight-release venue before the fix.
        from jamesiv.timeutil import next_occurrence_nyc
        now = datetime(2026, 8, 24, 23, 58, 45, tzinfo=NYC)
        nxt = next_occurrence_nyc(dtime(0, 0), now)
        assert nxt == datetime(2026, 8, 25, 0, 0, tzinfo=NYC)
        assert (nxt - now).total_seconds() == 75

    def test_a_just_missed_drop_still_counts_as_now(self):
        # 30s late must fire into the tail of the release, not roll to tomorrow.
        from jamesiv.timeutil import next_occurrence_nyc
        now = datetime(2026, 8, 24, 10, 0, 30, tzinfo=NYC)
        assert next_occurrence_nyc(dtime(10, 0), now) == datetime(2026, 8, 24, 10, 0, tzinfo=NYC)

    def test_a_long_missed_drop_rolls_to_tomorrow(self):
        from jamesiv.timeutil import next_occurrence_nyc
        now = datetime(2026, 8, 24, 10, 5, 0, tzinfo=NYC)
        assert next_occurrence_nyc(dtime(10, 0), now) == datetime(2026, 8, 25, 10, 0, tzinfo=NYC)

    def test_midnight_drop_books_the_right_night(self):
        # Composition with drop_target_day: armed at 23:58 on the 24th for a
        # midnight drop with days_ahead=30, the released date is Sep 24
        # (25th + 30), not Sep 23 (24th + 30).
        from datetime import date

        from jamesiv.config import Target
        from jamesiv.matching import drop_target_day
        from jamesiv.timeutil import next_occurrence_nyc
        now = datetime(2026, 8, 24, 23, 58, 45, tzinfo=NYC)
        target = Target(name="T", slug="t", drop={"at": "00:00", "days_ahead": 30})
        release = next_occurrence_nyc(target.drop.at, now)
        assert drop_target_day(target, release.date()) == date(2026, 9, 24)
