"""Back-tests for the SevenRooms (DoorDash) provider: the real Hunter and the
real SevenRoomsClient against the stateful fake, over real event-loop timing.
Scenario parity with the Resy back-test suite."""

from __future__ import annotations

from datetime import timedelta

import pytest

from jamesiv.config import Config, Secrets, Settings, Target
from jamesiv.hunter import Hunter
from jamesiv.models import ResyError
from jamesiv.notify import Notifier
from jamesiv.resy import ResyClient
from jamesiv.sevenrooms import SevenRoomsClient, venue_key
from jamesiv.simulator import SimSevenRooms, sr_slot_at
from jamesiv.state import Store
from jamesiv.timeutil import now_nyc

GUEST = {"first_name": "Michael", "last_name": "G", "email": "m@example.com",
         "phone": "+12125551234"}


class RecordingNotifier(Notifier):
    def __init__(self):
        super().__init__(Secrets())
        self.sent = []

    @property
    def enabled(self):
        return True

    async def send(self, title, message, **kwargs):
        self.sent.append((title, message))

    def titles(self):
        return " | ".join(t for t, _ in self.sent)


def build_target(**kwargs) -> Target:
    defaults = dict(
        name="Corner Store", slug="sim-corner-store", provider="sevenrooms",
        party_size=2, action="book", earliest="17:00", latest="22:00",
        days_ahead_min=0, days_ahead_max=35,
    )
    defaults.update(kwargs)
    return Target(**defaults)


@pytest.fixture
async def rig(tmp_path):
    made = []

    async def _factory(target, *, guest=GUEST, **settings_kwargs):
        settings = Settings(state_path=str(tmp_path / "sr.db"), **settings_kwargs)
        config = Config(settings=settings, targets=[target])
        resy = ResyClient(rate=200, burst=200)
        sr = SevenRoomsClient(rate=200, burst=200, guest=guest)
        notifier = RecordingNotifier()
        store = Store(settings.state_path)
        hunter = Hunter(config=config, secrets=Secrets(), client=resy, store=store,
                        notifier=notifier, sevenrooms_client=sr)
        made.append((resy, sr, store))
        return hunter, notifier

    yield _factory
    for resy, sr, store in made:
        await resy.aclose()
        await sr.aclose()
        store.close()


async def test_no_login_is_needed_for_a_pure_sevenrooms_config(rig):
    sim = SimSevenRooms()
    with sim.mock():
        hunter, _ = await rig(build_target())
        await hunter.login()   # must not raise despite empty Resy credentials
    assert hunter.client.auth_token is None


async def test_books_a_cancellation_via_hold_then_complete(rig):
    sim = SimSevenRooms()
    with sim.mock():
        hunter, notifier = await rig(build_target())
        await hunter.login()
        target = hunter.config.targets[0]

        assert await hunter.poll_once(target) is None   # nothing bookable yet
        sim.add(sr_slot_at(2, "19:30"))                # a table frees up

        booking = await hunter.poll_once(target)

    assert booking is not None
    assert len(sim.booked) == 1
    assert sim.hold_calls == 1 and sim.book_calls == 1
    assert "Booked" in notifier.titles()


async def test_request_queue_rows_are_never_treated_as_tables(rig):
    sim = SimSevenRooms()   # emits request rows on every day even with no slots
    with sim.mock():
        hunter, notifier = await rig(build_target(days_ahead_min=0, days_ahead_max=3))
        await hunter.login()
        booking = await hunter.poll_once(hunter.config.targets[0])

    assert booking is None
    assert sim.hold_calls == 0
    assert notifier.sent == [], "request rows must not produce alerts"


async def test_snipes_a_sevenrooms_drop(rig):
    sim = SimSevenRooms()
    with sim.mock():
        target = build_target(
            weekdays=[],
            drop={
                "days_ahead": 14,
                "at": (now_nyc() + timedelta(seconds=3)).strftime("%H:%M:%S"),
                "lead_ms": 200,
                "burst_interval_ms": 300,
                "max_requests": 8,
                "clock_probes": 2,
            },
        )
        hunter, notifier = await rig(target)
        await hunter.login()
        sim.release_in(3.0, sr_slot_at(14, "19:00"), sr_slot_at(14, "20:30"))

        booking = await hunter.snipe(target)

    assert booking is not None
    assert sim.booked[0].start.hour == 19
    assert "Booked" in notifier.titles()


async def test_a_contested_hold_falls_through_and_still_books(rig):
    sim = SimSevenRooms()
    with sim.mock():
        hunter, _ = await rig(build_target())
        await hunter.login()
        # Fastest competitor wins the first table; the second is free.
        sim.add(sr_slot_at(2, "19:00", contested=1), sr_slot_at(2, "20:00"))
        booking = await hunter.poll_once(hunter.config.targets[0])

    assert booking is not None
    assert len(sim.booked) == 1
    assert sim.booked[0].start.hour in (19, 20)


async def test_the_request_budget_holds_on_sevenrooms_too(rig):
    sim = SimSevenRooms()
    with sim.mock():
        target = build_target(
            weekdays=[],
            drop={
                "days_ahead": 14,
                "at": (now_nyc() + timedelta(seconds=1)).strftime("%H:%M:%S"),
                "lead_ms": 100,
                "burst_interval_ms": 100,
                "clock_probes": 2,
            },
        )
        assert target.drop.max_requests == 5
        hunter, _ = await rig(target)
        await hunter.login()
        await hunter.snipe(target)   # nothing ever released

    assert sim.find_calls <= 5, f"budget leaked on sevenrooms: {sim.find_calls}"


async def test_dry_run_never_holds_or_books(rig):
    sim = SimSevenRooms()
    with sim.mock():
        sim.add(sr_slot_at(2, "19:30"))
        hunter, notifier = await rig(build_target(), dry_run=True)
        await hunter.login()
        booking = await hunter.poll_once(hunter.config.targets[0])

    assert booking is None
    assert sim.hold_calls == 0 and sim.book_calls == 0
    assert "dry run" in notifier.titles().lower()


async def test_a_captcha_venue_fails_loudly_with_a_deep_link(rig):
    sim = SimSevenRooms()
    sim.captcha_on_book = True
    with sim.mock():
        sim.add(sr_slot_at(2, "19:30"))
        hunter, notifier = await rig(build_target())
        await hunter.login()
        booking = await hunter.poll_once(hunter.config.targets[0])

    assert booking is None
    assert any("Auto-book failed" in t for t, _ in notifier.sent), (
        "a captcha wall must page the owner immediately, not fail silently"
    )


async def test_missing_guest_details_surface_before_any_hold_is_wasted(rig):
    sim = SimSevenRooms()
    with sim.mock():
        sim.add(sr_slot_at(2, "19:30"))
        hunter, notifier = await rig(build_target(), guest={})
        await hunter.login()
        booking = await hunter.poll_once(hunter.config.targets[0])

    assert booking is None
    assert sim.hold_calls == 0, "must not lock a table it can never complete"
    assert any("GUEST_FIRST_NAME" in m for _, m in notifier.sent), (
        "the alert must name the exact .env keys to fill in"
    )


async def test_a_blocked_sevenrooms_edge_raises_instead_of_reporting_no_tables(rig):
    sim = SimSevenRooms()
    sim.availability_status_override = 500
    with sim.mock():
        hunter, _ = await rig(build_target(days_ahead_min=0, days_ahead_max=2))
        await hunter.login()
        with pytest.raises(ResyError):
            await hunter.poll_once(hunter.config.targets[0])


async def test_slot_identity_is_distinct_across_providers(rig):
    # Same wall-clock table at a Resy venue and an SR venue must never collide
    # in the dedupe store.
    a = venue_key("sim-corner-store")
    b = venue_key("theeightysix")
    assert a != b
    assert a > 0 and b > 0
