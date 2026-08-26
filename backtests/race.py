"""Strategy back-test: which burst profile wins the most reservations?

This races the REAL Hunter._burst -- the production code path, unmodified --
against a modeled field of rivals, over hundreds of independent drop trials
per strategy. Only the network is fake; every timing decision under test is
the shipped one.

The world model (all timings real seconds on the event loop):

- The venue releases N in-window tables at the boundary. Resy is usually
  punctual but sometimes late: 70% on time, 30% late by 0.2-2.0s.
- Our clock sync is good but not perfect: the burst's start is offset by a
  gaussian clock error (sigma 60ms).
- Every request we make costs a round trip: gaussian ~85ms +/- 15ms.
- Each table's fastest rival claims it at some delay after it becomes
  visible: 70% of tables are contested by other bots (lognormal, median
  ~450ms), the rest only by humans (lognormal, median ~8s).
- 15% of claimed tables bounce back: the rival's hold lapses 3-8s later and
  the table is free again. This is the window patience exploits.

Absolute take rates depend on these assumptions and should not be quoted as
predictions. The *ordering* of strategies is the finding: it is driven by
mechanics (shot timing vs rival timing) that hold under any plausible
parameterisation.

Run:  python backtests/race.py [--trials 240] [--only NAME]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from jamesiv.config import Config, Secrets, Settings, Target
from jamesiv.hunter import Hunter
from jamesiv.models import ResyError, Slot, SlotTaken
from jamesiv.state import Store
from jamesiv.timeutil import NYC

# ---------------------------------------------------------------- world model

RTT_MEAN, RTT_SIGMA, RTT_FLOOR = 0.085, 0.015, 0.050
CLOCK_ERR_SIGMA = float(os.environ.get("RACE_CLOCK_SIGMA", "0.060"))
P_LATE_RELEASE = float(os.environ.get("RACE_P_LATE", "0.30"))
LATE_MIN = 0.2
LATE_MAX = float(os.environ.get("RACE_LATE_MAX", "2.0"))
P_BOT_CONTESTED = 0.70
BOT_MEDIAN, BOT_SIGMA = 0.45, 0.6          # lognormal, seconds after visibility
HUMAN_MEDIAN, HUMAN_SIGMA = 8.0, 0.8
P_BOUNCE, BOUNCE_MIN, BOUNCE_MAX = 0.15, 3.0, 8.0
SLOTS_MIN, SLOTS_MAX = 3, 6


@dataclass
class RaceSlot:
    slot: Slot
    visible_at: float          # monotonic, when the provider shows it
    claimed_at: float          # when the fastest rival takes it
    lapses_at: float | None    # hold bounce-back, if any
    held_by_us: bool = False   # sevenrooms: our hold locks the table

    def state(self, t: float) -> str:
        if t < self.visible_at:
            return "hidden"
        if t < self.claimed_at:
            return "free"
        if self.lapses_at is not None and t >= self.lapses_at:
            return "free"
        return "claimed"


class RaceClient:
    """Duck-types the three client methods the burst path uses, with real
    per-request latency and a time-indexed rival model.

    Provider semantics differ at the claim step and the difference is modeled:

    - resy: `details` then `book` are separate races -- a rival can take the
      table between them (evaluated independently at each server arrival).
    - sevenrooms: the HOLD locks the table; win the hold and completion cannot
      be stolen. Rivals likewise lock at their claim instant.
    """

    def __init__(self, race_slots: list[RaceSlot], rng: random.Random,
                 provider: str = "resy"):
        self.provider = provider
        self.race_slots = race_slots
        self.rng = rng
        self.find_calls = 0
        self.booked: list[RaceSlot] = []
        self.t0 = time.monotonic()

    def _rtt(self) -> float:
        return max(RTT_FLOOR, self.rng.gauss(RTT_MEAN, RTT_SIGMA))

    def _t(self) -> float:
        return time.monotonic() - self.t0

    async def find(self, *, venue_id, day, party_size, venue_slug=None, throttle=True,
                   retries=2):
        self.find_calls += 1
        rtt = self._rtt()
        t_server = self._t() + rtt / 2      # state evaluated at server arrival
        await asyncio.sleep(rtt)
        return [
            rs.slot for rs in self.race_slots
            if rs.slot.party_size == party_size and rs.state(t_server) == "free"
        ]

    async def book_token_for(self, slot: Slot) -> str:
        rtt = self._rtt()
        t_server = self._t() + rtt / 2
        await asyncio.sleep(rtt)
        rs = self._lookup(slot)
        if rs.state(t_server) != "free":
            raise SlotTaken("gone at details/hold")
        if self.provider == "sevenrooms":
            # The hold locks the table for us; the race is over here.
            rs.claimed_at = -1.0
            rs.lapses_at = None
            rs.held_by_us = True
        return f"bt::{slot.config_id}"

    async def book(self, slot: Slot, book_token: str):
        rtt = self._rtt()
        t_server = self._t() + rtt / 2
        await asyncio.sleep(rtt)
        rs = self._lookup(slot)
        if self.provider == "sevenrooms":
            if not getattr(rs, "held_by_us", False):
                raise SlotTaken("hold vanished")
        elif rs.state(t_server) != "free":
            raise SlotTaken("beaten at book")
        rs.claimed_at = -1.0
        rs.lapses_at = None
        self.booked.append(rs)
        return f"rt::{slot.config_id}", "res-race"

    def _lookup(self, slot: Slot) -> RaceSlot:
        for rs in self.race_slots:
            if rs.slot.config_id == slot.config_id:
                return rs
        raise ResyError("unknown slot")


class SilentNotifier:
    enabled = False
    async def send(self, *a, **k): pass


# ------------------------------------------------------------------ one trial

def _make_slots(rng: random.Random, lead_s: float) -> list[RaceSlot]:
    """The tables released at this drop, with their rival timeline."""
    boundary = lead_s + rng.gauss(0.0, CLOCK_ERR_SIGMA)
    jitter = rng.uniform(LATE_MIN, LATE_MAX) if rng.random() < P_LATE_RELEASE else 0.0
    visible = max(0.0, boundary + jitter)

    day = date.today() + timedelta(days=30)
    out = []
    for i in range(rng.randint(SLOTS_MIN, SLOTS_MAX)):
        start = datetime(day.year, day.month, day.day, 18, 0, tzinfo=NYC) + timedelta(
            minutes=30 * i
        )
        slot = Slot(
            config_id=f"cfg-{i}", start=start, seating_type="Dining Room",
            venue_id=1, day=day, party_size=2,
        )
        if rng.random() < P_BOT_CONTESTED:
            delay = rng.lognormvariate(math.log(BOT_MEDIAN), BOT_SIGMA)
        else:
            delay = rng.lognormvariate(math.log(HUMAN_MEDIAN), HUMAN_SIGMA)
        claimed = visible + delay
        lapses = claimed + rng.uniform(BOUNCE_MIN, BOUNCE_MAX) if rng.random() < P_BOUNCE else None
        out.append(RaceSlot(slot=slot, visible_at=visible, claimed_at=claimed, lapses_at=lapses))
    return out


async def run_trial(profile: dict, seed: int, provider: str = "resy") -> dict:
    rng = random.Random(seed)
    drop_overrides = {k: v for k, v in profile.items() if k not in ("name", "lead_ms")}
    lead_ms = profile.get("lead_ms", 250)

    target = Target(
        name="Race", slug="race", venue_id=1, action="book",
        earliest="17:00", latest="22:00",
        drop={"days_ahead": 30, "at": "10:00", "lead_ms": lead_ms, **drop_overrides},
    )
    client = RaceClient(_make_slots(rng, lead_ms / 1000.0), rng, provider=provider)
    store = Store(":memory:")
    hunter = Hunter(
        config=Config(settings=Settings(), targets=[target]),
        secrets=Secrets(), client=client, store=store, notifier=SilentNotifier(),
    )
    t_start = time.monotonic()
    try:
        booking = await hunter._burst(target, 1, target.dates[0] if target.dates else
                                      date.today() + timedelta(days=30), target.drop)
    finally:
        store.close()
    return {
        "won": booking is not None,
        "requests": client.find_calls,
        "t_book": time.monotonic() - t_start if booking else None,
    }


# -------------------------------------------------------------------- runner

STRATEGIES = [
    # name                 shots  shape
    {"name": "1-shot",     "max_requests": 1,  "burst_concurrency": 1, "burst_interval_ms": 400,
     "burst_seconds": 4},
    {"name": "3-tight",    "max_requests": 3,  "burst_concurrency": 1, "burst_interval_ms": 150,
     "burst_seconds": 6},
    {"name": "5-quick (shipped)", "max_requests": 5, "burst_concurrency": 1,
     "burst_interval_ms": 400, "aggressive_seconds": 3, "burst_seconds": 10},
    {"name": "5-tight",    "max_requests": 5,  "burst_concurrency": 1, "burst_interval_ms": 150,
     "burst_seconds": 6},
    {"name": "5-spread",   "max_requests": 5,  "burst_concurrency": 1, "burst_interval_ms": 250,
     "aggressive_seconds": 0.6, "decay_factor": 12, "burst_seconds": 9},
    {"name": "10-mixed",   "max_requests": 10, "burst_concurrency": 2, "burst_interval_ms": 200,
     "aggressive_seconds": 1.5, "decay_factor": 8, "burst_seconds": 12},
    {"name": "25-volley",  "max_requests": 25, "burst_concurrency": 3, "burst_interval_ms": 120,
     "aggressive_seconds": 2, "decay_factor": 5, "burst_seconds": 12},
    {"name": "100-barrage", "max_requests": 100, "burst_concurrency": 5, "burst_interval_ms": 60,
     "aggressive_seconds": 3, "decay_factor": 4, "burst_seconds": 12},
    # stretch: same 5 shots, 3 dense then 2 spread -- covers releases that
    # land seconds late (shots ~0, .4, .8, 2.8, 4.8 relative to boundary)
    {"name": "5-stretch lead=100", "max_requests": 5, "burst_concurrency": 1,
     "burst_interval_ms": 400, "aggressive_seconds": 1.0, "decay_factor": 5,
     "burst_seconds": 8, "lead_ms": 100},
    # the candidate new default: spread shape + minimal lead
    {"name": "5-spread lead=100", "max_requests": 5, "burst_concurrency": 1,
     "burst_interval_ms": 250, "aggressive_seconds": 0.6, "decay_factor": 12,
     "burst_seconds": 9, "lead_ms": 100},
    {"name": "5-spread lead=0", "max_requests": 5, "burst_concurrency": 1,
     "burst_interval_ms": 250, "aggressive_seconds": 0.6, "decay_factor": 12,
     "burst_seconds": 9, "lead_ms": 0},
    {"name": "5-quick lead=100", "max_requests": 5, "burst_interval_ms": 400,
     "lead_ms": 100, "burst_seconds": 10},
    # lead-time sensitivity, all on the shipped 5-quick shape
    {"name": "5-quick lead=0",    "max_requests": 5, "burst_interval_ms": 400, "lead_ms": 0,
     "burst_seconds": 10},
    {"name": "5-quick lead=600",  "max_requests": 5, "burst_interval_ms": 400, "lead_ms": 600,
     "burst_seconds": 10},
    {"name": "5-quick lead=1200", "max_requests": 5, "burst_interval_ms": 400, "lead_ms": 1200,
     "burst_seconds": 10},
]


async def run_strategy(profile: dict, trials: int, concurrency: int = 60,
                       provider: str = "resy") -> dict:
    results = []
    for chunk_start in range(0, trials, concurrency):
        chunk = range(chunk_start, min(chunk_start + concurrency, trials))
        results.extend(await asyncio.gather(
            *(run_trial(profile, 1000 + i, provider) for i in chunk)
        ))
    wins = [r for r in results if r["won"]]
    times = sorted(r["t_book"] for r in wins)
    return {
        "name": profile["name"],
        "trials": trials,
        "take_rate": len(wins) / trials,
        "avg_requests": sum(r["requests"] for r in results) / trials,
        "p50_time_to_book": times[len(times) // 2] if times else None,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=240)
    ap.add_argument("--only", type=str, default=None)
    ap.add_argument("--provider", type=str, default="resy",
                    choices=["resy", "sevenrooms"])
    args = ap.parse_args()

    strategies = [s for s in STRATEGIES if args.only is None or args.only in s["name"]]
    out = []
    for profile in strategies:
        t0 = time.monotonic()
        summary = await run_strategy(profile, args.trials, provider=args.provider)
        summary["provider"] = args.provider
        summary["wall_s"] = round(time.monotonic() - t0, 1)
        out.append(summary)
        print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)   # thousands of trials; silence the hunt log
    asyncio.run(main())
