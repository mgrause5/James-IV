"""Release-policy discovery: parsing the venue's own page so nobody has to
transcribe 'reservations open 30 days in advance at 9AM' by hand."""

from __future__ import annotations

from datetime import time as dtime

import pytest

from jamesiv.policy import DropPolicy, extract_policy, parse_policy_text


class TestProseParsing:
    """Phrasings collected from how NYC venues actually write these notes."""

    @pytest.mark.parametrize(
        ("text", "days", "at"),
        [
            ("Reservations open 30 days in advance at 9:00AM ET.", 30, dtime(9, 0)),
            ("Reservations are released 28 days out at 10 a.m.", 28, dtime(10, 0)),
            ("Tables become available at 12:00 PM, 21 days in advance.", 21, dtime(12, 0)),
            ("Bookings open daily at midnight, 30 days ahead.", 30, dtime(0, 0)),
            ("Reservations open two weeks in advance at 9am.", 14, dtime(9, 0)),
            ("Reservations open thirty days in advance at 9 AM.", 30, dtime(9, 0)),
            ("We release tables 14 days prior at 10:30am sharp.", 14, dtime(10, 30)),
        ],
    )
    def test_recovers_days_and_time(self, text, days, at):
        policy = parse_policy_text(text)
        assert policy is not None, text
        assert policy.days_ahead == days
        assert policy.at == at
        assert policy.cadence == "daily"
        assert policy.complete

    def test_days_without_a_stated_time(self):
        policy = parse_policy_text("Reservations are available 30 days in advance.")
        assert policy is not None
        assert policy.days_ahead == 30
        assert policy.at is None
        assert not policy.complete

    def test_time_without_a_stated_day_count(self):
        policy = parse_policy_text("New reservations open every day at 9:00 AM.")
        assert policy is not None
        assert policy.at == dtime(9, 0)
        assert policy.days_ahead is None

    def test_pm_times_do_not_get_read_as_am(self):
        policy = parse_policy_text("Tables released 7 days ahead at 3pm.")
        assert policy.at == dtime(15, 0)

    def test_noon_is_twelve_not_zero(self):
        policy = parse_policy_text("Reservations open 30 days out at noon.")
        assert policy.at == dtime(12, 0)

    def test_monthly_cadence_is_flagged_not_faked(self):
        policy = parse_policy_text(
            "Reservations open on the 1st of the month for the following month."
        )
        assert policy is not None
        assert policy.cadence == "monthly"
        assert not policy.complete, "a monthly release must never look snipe-ready"

    def test_irrelevant_prose_parses_to_nothing(self):
        assert parse_policy_text("Our tasting menu changes with the seasons.") is None
        # Numbers alone should not trigger: this is a wine list, not a policy.
        assert parse_policy_text("Over 400 labels, with bottles from 1985.") is None

    def test_snippet_quotes_the_sentence_that_was_parsed(self):
        text = (
            "A neighborhood institution. Reservations open 30 days in advance "
            "at 9:00AM. Walk-ins welcome at the bar."
        )
        policy = parse_policy_text(text)
        assert "30 days in advance" in policy.snippet
        assert "Walk-ins" not in policy.snippet


class TestVenuePayloadExtraction:
    def test_structured_lead_time_plus_prose_time_merge(self):
        venue = {
            "id": {"resy": 1},
            "config": {"lead_time_in_days": 30},
            "content": [
                {
                    "name": "need_to_know",
                    "body": "Reservations open 30 days in advance at 9:00AM daily.",
                }
            ],
        }
        policy = extract_policy(venue)
        assert policy.days_ahead == 30
        assert policy.at == dtime(9, 0)
        assert policy.source == "structured+text"
        assert policy.complete

    def test_structured_field_wins_over_contradicting_prose_for_days(self):
        # The machine-readable field drives the site's own calendar; prose lags
        # reality when venues change policy.
        venue = {
            "config": {"lead_time_in_days": 21},
            "content": [{"body": "Reservations open 30 days in advance at 9am."}],
        }
        policy = extract_policy(venue)
        assert policy.days_ahead == 21
        assert policy.at == dtime(9, 0)

    def test_structured_only(self):
        policy = extract_policy({"settings": {"lead_time_in_days": 14}})
        assert policy.days_ahead == 14
        assert policy.at is None
        assert policy.source == "structured"

    def test_prose_buried_anywhere_in_the_tree_is_found(self):
        venue = {
            "metadata": {
                "sections": [
                    {"blocks": [{"text": "Tables are released 28 days out at 10 a.m."}]}
                ]
            }
        }
        policy = extract_policy(venue)
        assert policy.days_ahead == 28
        assert policy.at == dtime(10, 0)

    def test_a_page_that_states_nothing_yields_none(self):
        venue = {
            "id": {"resy": 1},
            "name": "Quiet Venue",
            "content": [{"body": "Seasonal Italian in a landmark townhouse."}],
        }
        assert extract_policy(venue) is None

    def test_richest_text_candidate_wins(self):
        venue = {
            "a": "Reservations open 30 days in advance.",                 # days only
            "b": "Reservations open 30 days in advance at 9:00AM ET.",    # days + time
        }
        policy = extract_policy(venue)
        assert policy.at == dtime(9, 0)


class TestDescribe:
    def test_reads_like_a_sentence(self):
        policy = DropPolicy(30, dtime(9, 0), "daily", "text", "")
        assert policy.describe() == "30 days ahead, at 09:00 ET"
