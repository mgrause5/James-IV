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
