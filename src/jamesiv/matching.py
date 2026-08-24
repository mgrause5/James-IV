"""Deciding which dates to check and which table is the best one on offer.

Deliberately pure: no network, no clock beyond what is passed in. This is the
part of the bot most worth being confident about, because the cost of a ranking
bug is a booked table you did not want at a restaurant you cannot cancel.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .config import Target
from .models import Slot


def candidate_days(target: Target, today: date) -> list[date]:
    """Every date this target wants checked, soonest first.

    Explicit `dates` win outright and are only filtered for being in the past.
    Otherwise we sweep the days_ahead range and filter by weekday.
    """
    if target.dates:
        return sorted(d for d in target.dates if d >= today)

    days: list[date] = []
    for offset in range(target.days_ahead_min, target.days_ahead_max + 1):
        day = today + timedelta(days=offset)
        if target.weekdays and day.weekday() not in target.weekdays:
            continue
        days.append(day)
    return days


def drop_target_day(target: Target, today: date) -> date | None:
    """The date whose inventory is released at the next drop.

    A venue booking 30 days out releases `today + 30` at 9am. If the target also
    constrains weekdays and that date does not qualify, there is nothing to
    snipe today.
    """
    if target.drop is None:
        return None
    day = today + timedelta(days=target.drop.days_ahead)
    if target.weekdays and day.weekday() not in target.weekdays:
        return None
    if target.dates and day not in target.dates:
        return None
    return day


def slot_matches(target: Target, slot: Slot, *, now: datetime | None = None) -> bool:
    """Would we accept this table at all?

    `now` enables the lead-time check, and callers hunting same-day
    cancellations must pass it. Resy happily returns tonight's 7pm slot to a
    query made at 8pm, and without this the bot would try to book a table that
    has already been sat -- or one 20 minutes out that you cannot possibly get
    to. Left as None the check is skipped, which is what the ranking tests want.
    """
    t = slot.start.time()
    if t < target.earliest or t > target.latest:
        return False
    if now is not None:
        lead_minutes = (slot.start - now).total_seconds() / 60.0
        if lead_minutes < target.min_lead_minutes:
            return False
    if not target.seating_allowed(slot.seating_type):
        return False
    if target.dates and slot.day not in target.dates:
        return False
    if target.weekdays and slot.day.weekday() not in target.weekdays:
        return False
    return True


def slot_sort_key(target: Target, slot: Slot) -> tuple:
    """Ranking. Lower is better.

    Ordering rationale, in priority order:
      1. Preferred time window -- the whole point of the windows list.
      2. Preferred seating type -- a dining room seat beats a bar stool if you
         said so, but never beats being in your window at all.
      3. Party size -- your requested size before any fallback size.
      4. Date, then time -- soonest wins ties, deterministically.
    """
    return (
        target.window_rank(slot.start.time()),
        target.seating_rank(slot.seating_type),
        target.party_sizes.index(slot.party_size) if slot.party_size in target.party_sizes else 99,
        slot.day,
        slot.start.time(),
    )


def best_slots(target: Target, slots: list[Slot], *, now: datetime | None = None) -> list[Slot]:
    """Filter to acceptable slots and rank them best-first."""
    matched = [s for s in slots if slot_matches(target, s, now=now)]
    matched.sort(key=lambda s: slot_sort_key(target, s))
    return matched


def describe_target(target: Target) -> str:
    bits = [f"party of {target.party_size}"]
    if target.party_size_fallback:
        bits.append(f"(or {', '.join(str(s) for s in target.party_size_fallback)})")
    bits.append(f"{target.earliest:%H:%M}-{target.latest:%H:%M}")
    if target.weekdays:
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        bits.append("/".join(names[d] for d in sorted(target.weekdays)))
    if target.seating_types:
        bits.append(f"seating: {', '.join(target.seating_types)}")
    bits.append(f"action: {target.action}")
    return " · ".join(bits)
