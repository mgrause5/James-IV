"""Back-tests: the real Hunter against a simulated Resy, over real time.

Each test is a scenario that actually happens in the wild. Several exist
specifically because they caught a bug -- those are marked REGRESSION.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from jamesiv.config import Config, Secrets, Settings, Target
from jamesiv.hunter import Hunter
from jamesiv.models import AuthError
from jamesiv.notify import Notifier
from jamesiv.resy import ResyClient
from jamesiv.simulator import VENUE_ID, SimResy, slot_at, slot_relative
from jamesiv.state import Store
from jamesiv.timeutil import now_nyc


class RecordingNotifier(Notifier):
    def __init__(self):
        super().__init__(Secrets())
        self.sent: list[tuple[str, str]] = []

    @property
    def enabled(self) -> bool:
        return True

    async def send(self, title, message, **kwargs):
        self.sent.append((title, message))

    def titles(self) -> str:
        return " | ".join(t for t, _ in self.sent)


def build_target(**kwargs) -> Target:
    defaults = dict(
        name="Torrisi",
        slug="torrisi",
        venue_id=VENUE_ID,
        party_size=2,
        action="book",
        earliest="17:00",
        latest="22:00",
        days_ahead_min=0,
        days_ahead_max=35,
    )
    defaults.update(kwargs)
    return Target(**defaults)


async def make_hunter(tmp_path, target, **settings_kwargs):
    settings = Settings(state_path=str(tmp_path / "bt.db"), **settings_kwargs)
    config = Config(settings=settings, targets=[target])
    secrets = Secrets(resy_email="sim@example.com", resy_password="pw")
    client = ResyClient(rate=200, burst=200)
    notifier = RecordingNotifier()
    store = Store(settings.state_path)
    hunter = Hunter(
        config=config, secrets=secrets, client=client, store=store, notifier=notifier
    )
    return hunter, client, notifier, store


@pytest.fixture
async def rig(tmp_path):
    """Yields a factory; tears down the client and store afterwards."""
    made = []

    async def _factory(target, **settings_kwargs):
        hunter, client, notifier, store = await make_hunter(tmp_path, target, **settings_kwargs)
        made.append((client, store))
        return hunter, notifier

    yield _factory

    for client, store in made:
        await client.aclose()
        store.close()


# ---------------------------------------------------------------------------
# Scenario 1: somebody cancels and a table appears mid-poll. The bread and
# butter of this bot, and the case the user asked about directly.
# ---------------------------------------------------------------------------


async def test_books_a_cancellation_that_appears_between_polls(rig):
    sim = SimResy()
    with sim.mock():
        hunter, notifier = await rig(build_target())
        await hunter.login()

        # First poll: the restaurant is full.
        assert await hunter.poll_once(hunter.config.targets[0]) is None
        assert sim.booked == []

        # Someone cancels a 7:30pm table three weeks out.
        sim.add(slot_at(2, "19:30"))

        booking = await hunter.poll_once(hunter.config.targets[0])

    assert booking is not None
    assert len(sim.booked) == 1
    assert sim.booked[0].start.hour == 19
    assert "Booked" in notifier.titles()


async def test_a_cancellation_today_is_booked_when_it_is_far_enough_out(rig):
    sim = SimResy()
    # Tonight, four hours from now -- reachable, so we want it.
    tonight = slot_relative(minutes_from_now=240)
    if not (17 <= tonight.start.hour <= 21):
        pytest.skip("wall clock puts the synthetic slot outside dining hours")

    with sim.mock():
        sim.add(tonight)
        hunter, _ = await rig(build_target(days_ahead_min=0, days_ahead_max=1))
        await hunter.login()
        booking = await hunter.poll_once(hunter.config.targets[0])

    assert booking is not None
    assert len(sim.booked) == 1


# REGRESSION: `slot_matches` checked time-of-day but not whether the slot had
# already happened, so a poll at 8pm would try to book tonight's 7pm table.
async def test_does_not_book_a_table_that_has_already_passed(rig):
    sim = SimResy()
    with sim.mock():
        sim.add(slot_relative(minutes_from_now=-120))   # two hours ago
        sim.add(slot_relative(minutes_from_now=20))     # unreachably soon
        hunter, _ = await rig(build_target(
            days_ahead_min=0, days_ahead_max=1, earliest="00:00", latest="23:59",
            min_lead_minutes=90,
        ))
        await hunter.login()
        booking = await hunter.poll_once(hunter.config.targets[0])

    assert booking is None
    assert sim.booked == []
    assert sim.details_calls == 0, "must not even request details for an unbookable slot"


# ---------------------------------------------------------------------------
# Scenario 2: the drop. Inventory materialises at an instant and the room is
# gone in seconds.
# ---------------------------------------------------------------------------


async def test_snipes_inventory_the_moment_it_is_released(rig):
    sim = SimResy()
    with sim.mock():
        target = build_target(
            weekdays=[],
            drop={
                "days_ahead": 30,
                "at": (now_nyc() + timedelta(seconds=3)).strftime("%H:%M:%S"),
                "lead_ms": 300,
                "burst_seconds": 6,
                "burst_concurrency": 3,
                "burst_interval_ms": 60,
                "max_requests": 300,
                "clock_probes": 3,
            },
        )
        hunter, notifier = await rig(target)
        await hunter.login()

        # The venue releases its 30-days-out inventory in 3 seconds.
        sim.release_in(3.0, slot_at(30, "19:00"), slot_at(30, "20:45"))

        booking = await hunter.snipe(target)

    assert booking is not None, "snipe failed to catch the drop"
    assert len(sim.booked) == 1
    assert sim.booked[0].start.hour == 19, "should take the better-ranked 7pm slot"
    assert sim.find_calls > 1, "should have been polling before inventory landed"
    assert "Booked" in notifier.titles()


# REGRESSION: the burst used to stop the instant it saw inventory, so losing
# the first race ended the snipe -- even though tables bounce back within
# seconds as competitors' holds lapse.
async def test_keeps_hunting_after_losing_the_first_races(rig):
    sim = SimResy()
    with sim.mock():
        target = build_target(
            weekdays=[],
            drop={
                "days_ahead": 30,
                "at": (now_nyc() + timedelta(seconds=2)).strftime("%H:%M:%S"),
                "lead_ms": 200,
                "burst_seconds": 8,
                "burst_concurrency": 2,
                "burst_interval_ms": 80,
                "max_requests": 300,
                "clock_probes": 3,
            },
        )
        hunter, _ = await rig(target)
        await hunter.login()

        # Two competitors beat us to this table before their holds lapse.
        sim.release_in(2.0, slot_at(30, "19:00", contested=2))

        booking = await hunter.snipe(target)

    assert booking is not None, "gave up after losing a race instead of retrying"
    assert sim.book_calls >= 3, "should have retried through the contested attempts"
    assert len(sim.booked) == 1


async def test_a_drop_that_never_lands_fails_cleanly_and_says_why(rig):
    sim = SimResy()
    with sim.mock():
        target = build_target(
            weekdays=[],
            drop={
                "days_ahead": 30,
                "at": (now_nyc() + timedelta(seconds=1)).strftime("%H:%M:%S"),
                "lead_ms": 100,
                "burst_seconds": 2,
                "burst_concurrency": 2,
                "burst_interval_ms": 100,
                "max_requests": 10,
                "clock_probes": 2,
            },
        )
        hunter, notifier = await rig(target)
        await hunter.login()

        booking = await hunter.snipe(target)   # no inventory ever released

    assert booking is None
    assert sim.booked == []
    assert sim.find_calls > 0
    # Nothing was ever seen, so this is a config problem, not a lost race --
    # the bot must not claim it was outraced, but it MUST still report the
    # empty drop to the owner's phone: mystery silence at a drop is a bug.
    assert "Missed the drop" not in notifier.titles()
    assert any("Drop report" in t for t, _ in notifier.sent)
    assert any("sold out before" in m or "days_ahead" in m for _, m in notifier.sent)


# ---------------------------------------------------------------------------
# Scenario 3: things going wrong in the middle of an unattended run.
# ---------------------------------------------------------------------------


# REGRESSION: an AuthError used to propagate out of poll_loop and kill every
# engine, including a snipe scheduled for the next morning.
async def test_recovers_from_an_expired_session_without_dying(rig):
    sim = SimResy()
    with sim.mock():
        hunter, notifier = await rig(build_target())
        await hunter.login()
        first_login_count = sim.login_calls

        # The session goes stale, as it does on a long-running bot.
        sim.expire_session()
        sim.add(slot_at(2, "19:30"))

        generation = hunter._auth_generation
        # The stale session must surface as AuthError specifically. It used to be
        # swallowed by `except ResyError` (AuthError subclasses it), which left
        # the bot polling a dead session forever without ever alerting.
        with pytest.raises(AuthError):
            await hunter.poll_once(hunter.config.targets[0])

        assert await hunter.recover_auth(generation) is True
        assert sim.login_calls == first_login_count + 1

        booking = await hunter.poll_once(hunter.config.targets[0])

    assert booking is not None, "should book normally once the session is refreshed"
    assert len(sim.booked) == 1


async def test_a_rate_limit_backs_off_rather_than_crashing(rig):
    sim = SimResy()
    sim.rate_limit_on_find = 1
    with sim.mock():
        hunter, _ = await rig(build_target(days_ahead_min=0, days_ahead_max=2))
        await hunter.login()
        sim.add(slot_at(1, "19:30"))

        # First find is 429'd; search absorbs it and returns what it has.
        result = await hunter.poll_once(hunter.config.targets[0])
        assert result is None

        booking = await hunter.poll_once(hunter.config.targets[0])

    assert booking is not None
    assert len(sim.booked) == 1


# REGRESSION: search broke out of the party-size loop on *any* returned slot,
# so a useless 2-top hid a bookable 3-top from the fallback size.
async def test_falls_back_to_a_larger_party_when_the_first_size_is_useless(rig):
    sim = SimResy()
    with sim.mock():
        # Party of 2 exists but only at 4pm, outside the target's window.
        sim.add(slot_at(14, "16:00", party=2))
        # Party of 3 has a perfect 7:30pm.
        sim.add(slot_at(14, "19:30", party=3))

        hunter, _ = await rig(build_target(
            party_size=2, party_size_fallback=[3], days_ahead_min=14, days_ahead_max=14,
        ))
        await hunter.login()
        booking = await hunter.poll_once(hunter.config.targets[0])

    assert booking is not None, "fallback party size was never probed"
    assert sim.booked[0].party_size == 3


# ---------------------------------------------------------------------------
# Scenario 4: the safety rails, under simulation rather than with a fake client.
# ---------------------------------------------------------------------------


async def test_dry_run_finds_the_table_and_refuses_to_book_it(rig):
    sim = SimResy()
    with sim.mock():
        sim.add(slot_at(2, "19:30"))
        hunter, notifier = await rig(build_target(), dry_run=True)
        await hunter.login()
        booking = await hunter.poll_once(hunter.config.targets[0])

    assert booking is None
    assert sim.booked == []
    assert sim.book_calls == 0
    assert sim.details_calls == 0
    assert "dry run" in notifier.titles().lower()


async def test_never_books_the_same_target_twice(rig):
    sim = SimResy()
    with sim.mock():
        sim.add(slot_at(2, "19:30"), slot_at(3, "20:00"))
        target = build_target(max_bookings=1)
        hunter, _ = await rig(target)
        await hunter.login()

        assert await hunter.poll_once(target) is not None
        assert await hunter.poll_once(target) is None

    assert len(sim.booked) == 1


async def test_the_global_budget_stops_booking_across_targets(rig):
    sim = SimResy()
    with sim.mock():
        # Days 1 and 2 sit in the always-checked near window; consecutive
        # polls must book them and then hit the global ceiling.
        sim.add(slot_at(1, "19:30"), slot_at(2, "20:00"), slot_at(3, "19:00"))
        target = build_target(max_bookings=5)
        hunter, _ = await rig(target, max_bookings_per_run=2)
        await hunter.login()

        for _ in range(4):
            await hunter.poll_once(target)

    assert len(sim.booked) == 2, "global per-run ceiling was not enforced"


async def test_a_booking_survives_a_restart(rig, tmp_path):
    sim = SimResy()
    with sim.mock():
        sim.add(slot_at(2, "19:30"), slot_at(3, "20:00"))
        target = build_target(max_bookings=1)

        hunter, _ = await rig(target)
        await hunter.login()
        assert await hunter.poll_once(target) is not None

        # A second process starting against the same state directory.
        restarted, _ = await rig(target)
        await restarted.login()
        assert await restarted.poll_once(target) is None

    assert len(sim.booked) == 1, "restart re-booked a target that was already satisfied"


# ---------------------------------------------------------------------------
# Scenario 5: politeness. A burst that sustains 20 req/s for its whole window
# is both useless (the room sold out in the first two seconds) and a good way
# to get an account flagged.
# ---------------------------------------------------------------------------


async def test_a_fruitless_burst_stops_at_its_request_cap(rig):
    sim = SimResy()
    with sim.mock():
        target = build_target(
            weekdays=[],
            drop={
                "days_ahead": 30,
                "at": (now_nyc() + timedelta(seconds=1)).strftime("%H:%M:%S"),
                "lead_ms": 100,
                "burst_seconds": 30,      # would run for 30s...
                "max_requests": 25,       # ...but the cap should end it far sooner
                "burst_concurrency": 3,
                "burst_interval_ms": 50,
                "aggressive_seconds": 30,
                "clock_probes": 2,
            },
        )
        hunter, _ = await rig(target)
        await hunter.login()

        started = now_nyc()
        assert await hunter.snipe(target) is None
        elapsed = (now_nyc() - started).total_seconds()

    assert sim.find_calls <= 25 + target.drop.burst_concurrency, (
        f"burst cap not enforced: {sim.find_calls} requests"
    )
    assert elapsed < 20, "should have stood down early rather than burning the full window"


async def test_the_burst_cadence_decays_after_the_release_window(rig):
    sim = SimResy()
    with sim.mock():
        target = build_target(
            weekdays=[],
            drop={
                "days_ahead": 30,
                "at": (now_nyc() + timedelta(seconds=1)).strftime("%H:%M:%S"),
                "lead_ms": 100,
                "burst_seconds": 6,
                "burst_concurrency": 1,
                "burst_interval_ms": 100,
                "aggressive_seconds": 1.5,   # 1.5s hard, then 10x slower
                "decay_factor": 10.0,
                "max_requests": 500,
                "clock_probes": 2,
            },
        )
        hunter, _ = await rig(target)
        await hunter.login()
        await hunter.snipe(target)

    # Undecayed, 6s at 100ms would be ~60 requests. With decay after 1.5s the
    # tail runs at 1/s, so the total lands far below that.
    assert sim.find_calls < 35, f"cadence did not decay: {sim.find_calls} requests"
    assert sim.find_calls > 8, "decayed so hard it stopped hunting"


async def test_dry_run_stops_the_burst_instead_of_faking_lost_races(rig):
    """REGRESSION: dry-run made try_book return None, which the burst read as
    being outraced -- so a rehearsal burned the whole window and logged a
    stream of alarming 'lost every candidate' lines."""
    sim = SimResy()
    with sim.mock():
        target = build_target(
            weekdays=[],
            drop={
                "days_ahead": 30,
                "at": (now_nyc() + timedelta(seconds=1)).strftime("%H:%M:%S"),
                "lead_ms": 100,
                "burst_seconds": 20,
                "burst_concurrency": 2,
                "burst_interval_ms": 100,
                "max_requests": 300,
                "clock_probes": 2,
            },
        )
        hunter, notifier = await rig(target, dry_run=True)
        await hunter.login()
        sim.release_in(1.0, slot_at(30, "19:00"))

        started = now_nyc()
        result = await hunter.snipe(target)
        elapsed = (now_nyc() - started).total_seconds()

    assert result is None
    assert sim.booked == []
    assert sim.book_calls == 0
    assert elapsed < 10, f"burst ran {elapsed:.0f}s instead of standing down on dry run"
    assert "dry run" in notifier.titles().lower()


# ---------------------------------------------------------------------------
# Scenario 6: drop-policy auto-discovery. The user configures the wrong drop
# time; the venue's own page states the right one; auto mode must correct it
# before the snipe arms.
# ---------------------------------------------------------------------------


async def test_auto_discovery_corrects_a_wrong_configured_drop(rig):
    sim = SimResy()
    sim.venue_extra = {
        "config": {"lead_time_in_days": 21},
        "content": [
            {
                "name": "need_to_know",
                "body": "Reservations open 21 days in advance at 10:00AM daily.",
            }
        ],
    }
    with sim.mock():
        target = build_target(
            drop={
                "auto": True,
                "at": "09:00:00",     # wrong: the page says 10am
                "days_ahead": 30,     # wrong: the page says 21
            },
        )
        hunter, _ = await rig(target)
        await hunter.login()
        await hunter.apply_drop_policy(target)

    from datetime import time as dtime

    assert target.drop.at == dtime(10, 0), "release time not corrected from the page"
    assert target.drop.days_ahead == 21, "day count not corrected from the page"


async def test_auto_discovery_keeps_config_when_the_page_is_silent(rig):
    sim = SimResy()
    sim.venue_extra = {
        "content": [{"body": "A love letter to the classic New York chophouse."}]
    }
    with sim.mock():
        target = build_target(drop={"auto": True, "at": "09:00:00", "days_ahead": 30})
        hunter, _ = await rig(target)
        await hunter.login()
        await hunter.apply_drop_policy(target)

    from datetime import time as dtime

    assert target.drop.at == dtime(9, 0), "configured fallback was lost"
    assert target.drop.days_ahead == 30


async def test_a_monthly_release_is_flagged_and_never_silently_applied(rig):
    sim = SimResy()
    sim.venue_extra = {
        "content": [
            {"body": "Reservations open on the 1st of the month for the month ahead."}
        ]
    }
    with sim.mock():
        target = build_target(drop={"auto": True, "at": "09:00:00", "days_ahead": 30})
        hunter, notifier = await rig(target)
        await hunter.login()
        await hunter.apply_drop_policy(target)

    from datetime import time as dtime

    assert target.drop.at == dtime(9, 0), "monthly cadence must not rewrite drop timing"
    assert "monthly" in notifier.titles().lower(), "the user was never told"


# ---------------------------------------------------------------------------
# Scenario 7: Resy's edge throttles /4/find. Observed against production --
# persistent empty 500s from a disfavored IP. A blocked bot must say so, not
# quietly report "nothing available" forever.
# ---------------------------------------------------------------------------


async def test_a_fully_blocked_search_raises_instead_of_reporting_no_tables(rig):
    sim = SimResy()
    sim.find_status_override = 500
    with sim.mock():
        hunter, _ = await rig(build_target(days_ahead_min=0, days_ahead_max=2))
        await hunter.login()
        from jamesiv.models import ResyError

        with pytest.raises(ResyError):
            await hunter.poll_once(hunter.config.targets[0])


async def test_the_blindness_alarm_fires_once_at_the_threshold(rig):
    sim = SimResy()
    sim.find_status_override = 500
    with sim.mock():
        target = build_target(days_ahead_min=0, days_ahead_max=1)
        hunter, notifier = await rig(target, blind_poll_alert_after=3)
        await hunter.login()
        from jamesiv.models import ResyError

        for _ in range(5):
            exc = None
            try:
                await hunter.poll_once(target)
            except ResyError as e:
                exc = e
            assert exc is not None
            await hunter._note_blind_poll(target, exc)

    alarms = [t for t, _ in notifier.sent if "cannot see availability" in t]
    assert len(alarms) == 1, "alarm should fire exactly once, at the threshold"


async def test_one_flaky_500_among_working_days_does_not_raise(rig):
    sim = SimResy()
    sim.rate_limit_on_find = None
    with sim.mock():
        # Day 1 works and has a table; intermittent flake is retried inside the
        # client, and partial failure must not mask found inventory.
        sim.add(slot_at(1, "19:30"))
        hunter, _ = await rig(build_target(days_ahead_min=0, days_ahead_max=2))
        await hunter.login()
        booking = await hunter.poll_once(hunter.config.targets[0])

    assert booking is not None


async def test_default_drop_profile_fires_at_most_five_requests(rig):
    """The owner's explicit requirement: a drop is 1-5 precisely timed shots,
    never a barrage. This pins the shipped defaults to that promise."""
    sim = SimResy()   # nothing ever released: worst case, maximum temptation
    with sim.mock():
        target = build_target(
            weekdays=[],
            drop={
                "days_ahead": 30,
                "at": (now_nyc() + timedelta(seconds=1)).strftime("%H:%M:%S"),
                "lead_ms": 100,
                "clock_probes": 2,
                # everything else: shipped defaults
            },
        )
        assert target.drop.max_requests == 5
        assert target.drop.burst_concurrency == 1
        hunter, _ = await rig(target)
        await hunter.login()
        await hunter.snipe(target)

    assert sim.find_calls <= 5, f"default profile fired {sim.find_calls} requests"
    assert sim.book_calls == 0


async def test_party_size_fallback_cannot_double_the_request_budget(rig):
    """REGRESSION: the budget counted loop iterations, but each iteration fired
    one request per party size -- cap 5 with a fallback meant up to 10 requests,
    violating the owner's hard limit."""
    sim = SimResy()   # nothing released: every attempt probes every size
    with sim.mock():
        target = build_target(
            weekdays=[],
            party_size=2,
            party_size_fallback=[3, 4],
            drop={
                "days_ahead": 30,
                "at": (now_nyc() + timedelta(seconds=1)).strftime("%H:%M:%S"),
                "lead_ms": 100,
                "burst_interval_ms": 100,
                "clock_probes": 2,
            },
        )
        assert target.drop.max_requests == 5
        hunter, _ = await rig(target)
        await hunter.login()
        await hunter.snipe(target)

    assert sim.find_calls <= 5, f"fallback sizes leaked past the budget: {sim.find_calls}"


async def test_an_imminent_drop_is_sniped_not_skipped_to_tomorrow(rig):
    """REGRESSION: the scheduler treated any drop closer than 90s as 'missed'
    and armed for tomorrow -- so starting the bot at 9:59 for a 10:00 release
    deliberately sat out a drop it could have caught."""
    import asyncio

    sim = SimResy()
    with sim.mock():
        target = build_target(
            weekdays=[],
            drop={
                "days_ahead": 30,
                "at": "00:00:00",   # placeholder; stamped for real below
                "lead_ms": 200,
                "burst_interval_ms": 400,
                "clock_probes": 2,
            },
        )
        hunter, _ = await rig(target)
        await hunter.login()
        # Stamp the drop time and the release clock at the SAME instant --
        # login on a cold interpreter can take seconds, and stamping the drop
        # at build time made the release land after every shot.
        target.drop = target.drop.model_copy(
            update={"at": (now_nyc() + timedelta(seconds=4)).time()}
        )
        sim.release_in(4.0, slot_at(30, "19:00"))

        task = asyncio.create_task(hunter.snipe_scheduler(target))
        try:
            await asyncio.wait_for(
                _wait_for(lambda: len(sim.booked) > 0, timeout=15.0), timeout=20.0
            )
        finally:
            task.cancel()

    assert len(sim.booked) == 1, "scheduler skipped an imminent drop"


async def _wait_for(predicate, timeout: float):
    import asyncio

    deadline = timeout
    step = 0.2
    waited = 0.0
    while waited < deadline:
        if predicate():
            return
        await asyncio.sleep(step)
        waited += step
    raise AssertionError("condition never became true")


async def test_a_wide_range_is_swept_in_rotating_chunks_not_all_at_once(rig):
    """Politeness: a 0-30 day target must not fire 31 requests per poll. The
    sweep checks the nearest days every cycle and rotates the rest, covering
    the full range over a few cycles."""
    sim = SimResy()
    with sim.mock():
        target = build_target(dates=[], days_ahead_min=0, days_ahead_max=30,
                              action="notify")
        hunter, notifier = await rig(target)
        await hunter.login()

        await hunter.poll_once(target)
        first_cycle = sim.find_calls
        assert first_cycle <= target.poll_days_per_sweep, (
            f"one poll fired {first_cycle} requests"
        )

        # A table far out in the range is still found within a few cycles.
        far_day = 25
        sim.add(slot_at(far_day, "19:30"))
        for _ in range(6):
            await hunter.poll_once(target)
            if notifier.sent:
                break

    assert notifier.sent, "rotation never reached the far end of the range"


async def test_a_throttled_drop_reports_rejection_not_emptiness(rig):
    """If every burst shot is rejected by the edge, the report must say THAT --
    'nothing was released' and 'we were blind' demand different fixes."""
    sim = SimResy()
    sim.find_status_override = 500
    with sim.mock():
        target = build_target(
            weekdays=[],
            drop={
                "days_ahead": 30,
                "at": (now_nyc() + timedelta(seconds=1)).strftime("%H:%M:%S"),
                "lead_ms": 100,
                "burst_interval_ms": 100,
                "clock_probes": 2,
            },
        )
        hunter, notifier = await rig(target)
        await hunter.login()
        await hunter.snipe(target)

    reports = [m for t, m in notifier.sent if "Drop report" in t]
    assert reports, "a fully rejected drop must still reach the phone"
    assert "REJECTED" in reports[0] and "throttled" in reports[0]
