"""Ranking and filtering: the logic that decides what gets booked."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from jamesiv.config import Target
from jamesiv.matching import best_slots, candidate_days, drop_target_day, slot_matches
from jamesiv.models import Slot
from jamesiv.timeutil import NYC

TODAY = date(2026, 8, 24)  # a Monday


def make_slot(day: date, hhmm: str, seating: str = "Dining Room", party: int = 2) -> Slot:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return Slot(
        config_id=f"cfg-{day}-{hhmm}-{seating}",
        start=datetime(day.year, day.month, day.day, hour, minute, tzinfo=NYC),
        seating_type=seating,
        venue_id=1,
        day=day,
        party_size=party,
    )


def base_target(**kwargs) -> Target:
    defaults = dict(name="Test", slug="test", party_size=2)
    defaults.update(kwargs)
    return Target(**defaults)


class TestCandidateDays:
    def test_sweeps_the_range_inclusive(self):
        target = base_target(days_ahead_min=1, days_ahead_max=3)
        assert candidate_days(target, TODAY) == [
            date(2026, 8, 25), date(2026, 8, 26), date(2026, 8, 27)
        ]

    def test_filters_by_weekday(self):
        target = base_target(days_ahead_min=0, days_ahead_max=13, weekdays=["fri", "sat"])
        days = candidate_days(target, TODAY)
        assert all(d.weekday() in (4, 5) for d in days)
        assert len(days) == 4

    def test_explicit_dates_win_and_drop_the_past(self):
        target = base_target(dates=[date(2026, 8, 20), date(2026, 9, 1)], days_ahead_max=90)
        assert candidate_days(target, TODAY) == [date(2026, 9, 1)]

    def test_zero_offset_includes_today(self):
        target = base_target(days_ahead_min=0, days_ahead_max=0)
        assert candidate_days(target, TODAY) == [TODAY]


class TestDropTargetDay:
    def test_returns_the_released_date(self):
        target = base_target(drop={"days_ahead": 30, "at": "09:00"})
        assert drop_target_day(target, TODAY) == date(2026, 9, 23)

    def test_none_when_no_drop_configured(self):
        assert drop_target_day(base_target(), TODAY) is None

    def test_none_when_released_date_fails_weekday_filter(self):
        # 2026-09-23 is a Wednesday; this target only wants weekends.
        target = base_target(drop={"days_ahead": 30}, weekdays=["sat", "sun"])
        assert drop_target_day(target, TODAY) is None


class TestSlotMatches:
    def test_rejects_outside_time_bounds(self):
        target = base_target(earliest="18:00", latest="21:00")
        assert not slot_matches(target, make_slot(TODAY, "17:30"))
        assert slot_matches(target, make_slot(TODAY, "18:00"))
        assert slot_matches(target, make_slot(TODAY, "21:00"))
        assert not slot_matches(target, make_slot(TODAY, "21:30"))

    def test_excluded_seating_beats_included_seating(self):
        target = base_target(seating_types=["Bar"], exclude_seating=["Bar"])
        assert not slot_matches(target, make_slot(TODAY, "19:00", "Bar Room"))

    def test_seating_match_is_substring_and_case_insensitive(self):
        target = base_target(seating_types=["dining"])
        assert slot_matches(target, make_slot(TODAY, "19:00", "Dining Room"))
        assert not slot_matches(target, make_slot(TODAY, "19:00", "Chef's Counter"))

    def test_empty_seating_list_accepts_anything_not_excluded(self):
        target = base_target()
        assert slot_matches(target, make_slot(TODAY, "19:00", "Anything At All"))


class TestRanking:
    def test_preferred_window_outranks_seating_preference(self):
        target = base_target(
            earliest="17:00",
            latest="22:00",
            preferred_windows=[{"start": "19:00", "end": "20:00"}],
            seating_types=["Dining Room", "Bar"],
        )
        bar_in_window = make_slot(TODAY, "19:30", "Bar")
        dining_outside = make_slot(TODAY, "17:30", "Dining Room")
        ranked = best_slots(target, [dining_outside, bar_in_window])
        assert ranked[0] is bar_in_window

    def test_seating_preference_breaks_ties_within_a_window(self):
        target = base_target(
            earliest="17:00",
            latest="22:00",
            preferred_windows=[{"start": "19:00", "end": "20:00"}],
            seating_types=["Dining Room", "Bar"],
        )
        bar = make_slot(TODAY, "19:30", "Bar")
        dining = make_slot(TODAY, "19:45", "Dining Room")
        assert best_slots(target, [bar, dining])[0] is dining

    def test_requested_party_size_beats_fallback(self):
        target = base_target(
            party_size=2, party_size_fallback=[3], earliest="17:00", latest="22:00"
        )
        three = make_slot(TODAY, "19:00", party=3)
        two = make_slot(TODAY, "19:00", party=2)
        assert best_slots(target, [three, two])[0] is two

    def test_earlier_date_wins_all_else_equal(self):
        target = base_target(earliest="17:00", latest="22:00")
        later = make_slot(date(2026, 9, 5), "19:00")
        sooner = make_slot(date(2026, 8, 30), "19:00")
        assert best_slots(target, [later, sooner])[0] is sooner

    def test_non_matching_slots_are_dropped_entirely(self):
        target = base_target(earliest="19:00", latest="20:00")
        assert best_slots(target, [make_slot(TODAY, "22:00")]) == []


class TestTargetValidation:
    def test_rejects_inverted_time_bounds(self):
        with pytest.raises(ValueError, match="earliest"):
            base_target(earliest="21:00", latest="18:00")

    def test_rejects_inverted_days_ahead(self):
        with pytest.raises(ValueError, match="days_ahead_min"):
            base_target(days_ahead_min=20, days_ahead_max=5)

    def test_rejects_unknown_weekday_name(self):
        with pytest.raises(ValueError, match="unknown weekday"):
            base_target(weekdays=["funday"])

    def test_party_sizes_dedupes_fallback(self):
        assert base_target(party_size=2, party_size_fallback=[2, 3]).party_sizes == [2, 3]
