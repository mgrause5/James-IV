"""Persistence: booking caps and alert dedupe must survive a restart."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from jamesiv.models import Booking, Slot
from jamesiv.state import Store
from jamesiv.timeutil import NYC, now_utc


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def make_slot(hhmm: str = "19:00", day: date = date(2026, 9, 23)) -> Slot:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return Slot(
        config_id="cfg",
        start=datetime(day.year, day.month, day.day, hour, minute, tzinfo=NYC),
        seating_type="Dining Room",
        venue_id=42,
        day=day,
        party_size=2,
    )


def make_booking(token: str = "rt-1", target: str = "Tatiana") -> Booking:
    return Booking(
        resy_token=token,
        reservation_id="9",
        slot=make_slot(),
        target_name=target,
        booked_at=now_utc(),
    )


class TestBookings:
    def test_recording_a_booking_counts_toward_the_cap(self, store):
        assert store.booking_count("Tatiana") == 0
        store.record_booking(make_booking())
        assert store.booking_count("Tatiana") == 1

    def test_cancelling_frees_the_cap_again(self, store):
        store.record_booking(make_booking())
        store.mark_cancelled("rt-1")
        assert store.booking_count("Tatiana") == 0
        assert store.active_bookings() == []

    def test_counts_are_per_target(self, store):
        store.record_booking(make_booking("rt-1", "Tatiana"))
        store.record_booking(make_booking("rt-2", "Semma"))
        assert store.booking_count("Tatiana") == 1
        assert store.booking_count("Semma") == 1

    def test_has_booking_on_blocks_a_second_table_the_same_night(self, store):
        store.record_booking(make_booking())
        assert store.has_booking_on("Tatiana", "2026-09-23")
        assert not store.has_booking_on("Tatiana", "2026-09-24")

    def test_rebooking_the_same_token_does_not_double_count(self, store):
        store.record_booking(make_booking())
        store.record_booking(make_booking())
        assert store.booking_count("Tatiana") == 1

    def test_state_survives_reopening_the_database(self, tmp_path):
        path = tmp_path / "persist.db"
        with Store(path) as first:
            first.record_booking(make_booking())
        with Store(path) as second:
            assert second.booking_count("Tatiana") == 1


class TestSlotDedupe:
    def test_first_sighting_is_new_and_the_second_is_not(self, store):
        slot = make_slot()
        assert store.is_new_slot(slot, "Tatiana")
        assert not store.is_new_slot(slot, "Tatiana")

    def test_dedupe_is_scoped_per_target(self, store):
        slot = make_slot()
        assert store.is_new_slot(slot, "Tatiana")
        assert store.is_new_slot(slot, "Semma")

    def test_different_times_are_different_slots(self, store):
        assert store.is_new_slot(make_slot("19:00"), "Tatiana")
        assert store.is_new_slot(make_slot("19:30"), "Tatiana")

    def test_a_stale_sighting_re_arms_after_the_ttl(self, store):
        slot = make_slot()
        store.is_new_slot(slot, "Tatiana")
        stale = (now_utc() - timedelta(hours=9)).isoformat()
        store.conn.execute("UPDATE seen_slots SET seen_at = ?", (stale,))
        assert store.is_new_slot(slot, "Tatiana", ttl_hours=6.0)

    def test_prune_clears_old_sightings(self, store):
        store.is_new_slot(make_slot(), "Tatiana")
        store.conn.execute(
            "UPDATE seen_slots SET seen_at = ?",
            ((now_utc() - timedelta(days=30)).isoformat(),),
        )
        assert store.prune(older_than_days=7) == 1
