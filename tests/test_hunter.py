"""Hunter decision-making, with the network and notifications stubbed out.

These are the tests that matter most: everything here is about *not* charging
the user's card when it should not.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from jamesiv.config import Config, Secrets, Settings, Target
from jamesiv.hunter import Hunter
from jamesiv.models import Slot, SlotTaken
from jamesiv.state import Store
from jamesiv.timeutil import NYC


class FakeClient:
    """Stands in for ResyClient. Records what it was asked to do."""

    def __init__(self, slots=None, book_fails=0):
        self.slots = slots or []
        self.book_fails = book_fails
        self.payment_method_id = 222
        self.auth_token = "tok"
        self.booked: list[Slot] = []
        self.details_calls = 0

    async def find(self, *, venue_id, day, party_size, venue_slug=None, throttle=True,
                   retries=2):
        return [s for s in self.slots if s.day.isoformat() == day and s.party_size == party_size]

    async def venue_by_slug(self, slug, location="ny"):
        raise AssertionError("venue_id should be configured in these tests")

    async def book_token_for(self, slot):
        self.details_calls += 1
        if self.book_fails > 0:
            self.book_fails -= 1
            raise SlotTaken("taken")
        return "bt-1"

    async def book(self, slot, book_token):
        self.booked.append(slot)
        return f"rt-{len(self.booked)}", "res-1"


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.enabled = True

    async def send(self, title, message, **kwargs):
        self.sent.append((title, message))

    async def aclose(self):
        pass


def make_slot(hhmm="19:00", day=date(2026, 9, 23), seating="Dining Room", party=2) -> Slot:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return Slot(
        config_id=f"cfg-{hhmm}-{seating}",
        start=datetime(day.year, day.month, day.day, hour, minute, tzinfo=NYC),
        seating_type=seating,
        venue_id=42,
        day=day,
        party_size=party,
    )


def build(store, *, slots=None, book_fails=0, action="book", dry_run=False, max_run=3, **tkw):
    target = Target(
        name="Tatiana", slug="tatiana", venue_id=42, action=action,
        dates=[date(2026, 9, 23)], earliest="17:00", latest="22:00", **tkw
    )
    config = Config(
        settings=Settings(dry_run=dry_run, max_bookings_per_run=max_run), targets=[target]
    )
    client = FakeClient(slots=slots, book_fails=book_fails)
    notifier = FakeNotifier()
    hunter = Hunter(
        config=config, secrets=Secrets(), client=client, store=store, notifier=notifier
    )
    return hunter, target, client, notifier


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "hunter.db")
    yield s
    s.close()


class TestBooking:
    async def test_books_the_best_ranked_slot(self, store):
        slots = [make_slot("21:45"), make_slot("19:30"), make_slot("18:00")]
        hunter, target, client, _ = build(
            store, slots=slots, preferred_windows=[{"start": "19:00", "end": "20:00"}]
        )
        booking = await hunter.handle(target, await hunter.search(target, [date(2026, 9, 23)]))

        assert booking is not None
        assert client.booked[0].start.hour == 19
        assert store.booking_count("Tatiana") == 1

    async def test_falls_through_to_the_next_slot_when_beaten(self, store):
        slots = [make_slot("19:00"), make_slot("19:30")]
        hunter, target, client, _ = build(store, slots=slots, book_fails=1)
        booking = await hunter.handle(target, await hunter.search(target, [date(2026, 9, 23)]))

        assert booking is not None
        assert client.details_calls == 2
        assert client.booked[0].start.minute == 30

    async def test_respects_the_per_target_cap(self, store):
        hunter, target, client, _ = build(store, slots=[make_slot()], max_bookings=1)
        day = [date(2026, 9, 23)]
        await hunter.handle(target, await hunter.search(target, day))
        await hunter.handle(target, await hunter.search(target, day))

        assert len(client.booked) == 1

    async def test_respects_the_global_per_run_budget(self, store):
        hunter, target, client, _ = build(
            store, slots=[make_slot("19:00"), make_slot("20:00")], max_run=1, max_bookings=5
        )
        # Two different nights so the same-night guard does not mask the budget.
        hunter.bookings_this_run = 1
        await hunter.handle(target, await hunter.search(target, [date(2026, 9, 23)]))
        assert client.booked == []

    async def test_will_not_book_two_tables_the_same_night(self, store):
        hunter, target, client, _ = build(
            store, slots=[make_slot("19:00"), make_slot("20:00")], max_bookings=5
        )
        day = [date(2026, 9, 23)]
        await hunter.handle(target, await hunter.search(target, day))
        await hunter.handle(target, await hunter.search(target, day))
        assert len(client.booked) == 1


class TestDryRun:
    async def test_dry_run_never_calls_book(self, store):
        hunter, target, client, notifier = build(store, slots=[make_slot()], dry_run=True)
        booking = await hunter.handle(target, await hunter.search(target, [date(2026, 9, 23)]))

        assert booking is None
        assert client.booked == []
        assert client.details_calls == 0
        assert store.booking_count("Tatiana") == 0
        assert any("dry run" in title.lower() for title, _ in notifier.sent)


class TestNotifyAction:
    async def test_notify_targets_alert_without_booking(self, store):
        hunter, target, client, notifier = build(store, slots=[make_slot()], action="notify")
        booking = await hunter.handle(target, await hunter.search(target, [date(2026, 9, 23)]))

        assert booking is None
        assert client.booked == []
        assert len(notifier.sent) == 1
        assert "Open table" in notifier.sent[0][0]

    async def test_the_same_slot_only_alerts_once(self, store):
        hunter, target, _, notifier = build(store, slots=[make_slot()], action="notify")
        day = [date(2026, 9, 23)]
        await hunter.handle(target, await hunter.search(target, day))
        await hunter.handle(target, await hunter.search(target, day))

        assert len(notifier.sent) == 1

    async def test_nothing_available_sends_nothing(self, store):
        hunter, target, _, notifier = build(store, slots=[], action="notify")
        await hunter.handle(target, await hunter.search(target, [date(2026, 9, 23)]))
        assert notifier.sent == []


class TestSearch:
    async def test_fallback_party_size_is_only_probed_when_the_first_is_empty(self, store):
        # Only a 3-top exists; the target wants 2 but accepts 3.
        hunter, target, _, _ = build(
            store, slots=[make_slot(party=3)], party_size=2, party_size_fallback=[3]
        )
        found = await hunter.search(target, [date(2026, 9, 23)])
        assert len(found) == 1 and found[0].party_size == 3

    async def test_slots_outside_the_time_bounds_are_filtered_out(self, store):
        hunter, target, _, _ = build(store, slots=[make_slot("23:30")])
        assert await hunter.search(target, [date(2026, 9, 23)]) == []


class TestEnginePlan:
    """The drops-only posture: with polling off, the bot must plan ZERO poll
    engines -- its only provider contact is around each release time."""

    def _hunter(self, store, poll: bool):
        sniper = Target(name="Sniper", slug="s", venue_id=1,
                        drop={"at": "10:00", "days_ahead": 7})
        watcher = Target(name="Watcher", slug="w", venue_id=2)
        config = Config(
            settings=Settings(poll_for_cancellations=poll),
            targets=[sniper, watcher],
        )
        return Hunter(config=config, secrets=Secrets(), client=FakeClient(),
                      store=store, notifier=FakeNotifier())

    def test_polling_off_plans_only_snipes(self, store):
        plan = self._hunter(store, poll=False).engine_plan()
        assert all(kind == "snipe" for kind, _ in plan)
        assert [t.name for _, t in plan] == ["Sniper"]

    def test_watcher_only_target_is_idle_when_polling_is_off(self, store):
        plan = self._hunter(store, poll=False).engine_plan()
        assert "Watcher" not in {t.name for _, t in plan}

    def test_polling_on_plans_both_engines(self, store):
        plan = self._hunter(store, poll=True).engine_plan()
        kinds = sorted(f"{k}:{t.name}" for k, t in plan)
        assert kinds == ["poll:Sniper", "poll:Watcher", "snipe:Sniper"]
