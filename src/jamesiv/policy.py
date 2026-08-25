"""Discovering a venue's release policy from its own Resy page.

The `/3/venue` payload carries what a human reads on the website: the
"Need to Know" prose ("Reservations open 30 days in advance at 9:00AM"),
sometimes an announcement banner, and in many cases a structured lead-time
field. That is enough to recover `drop.days_ahead` and `drop.at` without the
user transcribing anything.

Two honest caveats, reflected in the design:

1. The prose is marketing copy, not a contract. Venues phrase it a dozen ways,
   change it without notice, or omit it entirely. So this module returns what
   it found *and where it found it*, and everything downstream treats the
   result as a strong suggestion to be confirmed -- `doctor` cross-checks it
   against your config, and `auto` mode logs exactly what it inferred.
2. Not every venue drops on a rolling daily window. "On the 1st of the month"
   cadences exist, and pretending those are daily drops would arm a snipe 30
   mornings in a row for nothing. We detect that shape and say so instead of
   guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time as dtime
from typing import Any

# Days-count words venues actually use in booking-policy prose.
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fourteen": 14, "twenty": 20, "twenty-one": 21, "twenty-eight": 28,
    "thirty": 30, "forty-five": 45, "sixty": 60, "ninety": 90,
}

_DAYS_RE = re.compile(
    r"(\d{1,3}|[a-z][a-z\-]{2,})\s*(?:calendar\s+)?days?\s+"
    r"(?:in\s+advance|out|ahead|prior|before)",
    re.IGNORECASE,
)
_WEEKS_RE = re.compile(
    r"(\d{1,2}|[a-z][a-z\-]{2,})\s*weeks?\s+(?:in\s+advance|out|ahead)",
    re.IGNORECASE,
)
_TIME_RE = re.compile(
    r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?m\.?",
    re.IGNORECASE,
)
_MIDNIGHT_RE = re.compile(r"\bat\s+midnight\b", re.IGNORECASE)
_NOON_RE = re.compile(r"\bat\s+noon\b", re.IGNORECASE)
_MONTHLY_RE = re.compile(
    r"(?:1st|first)(?:\s+day)?\s+of\s+(?:the|each|every)\s+month",
    re.IGNORECASE,
)
# Words that make a sentence worth parsing at all.
_RELEVANT_RE = re.compile(
    r"reservation|book|table|release|open|available|drop", re.IGNORECASE
)
# Sentences about reminders, cancellation policies, and deposits also contain
# "N days" phrases -- "a courtesy reminder text 2 days prior to your
# reservation" is not a release policy, and treating it as one would arm a
# snipe for a nonsense window. Such sentences are excluded before parsing.
_DISQUALIFY_RE = re.compile(
    r"reminder|no[- ]show|cancel|charge|deposit|credit card|grace period|"
    r"running late|confirm|prior to your reservation|before your reservation",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True, slots=True)
class DropPolicy:
    """What we learned about when a venue releases inventory."""

    days_ahead: int | None
    at: dtime | None
    cadence: str          # "daily" | "monthly" | "unknown"
    source: str           # "structured" | "text" | "structured+text"
    snippet: str          # the sentence we parsed, for the human to double-check

    @property
    def complete(self) -> bool:
        """Enough to arm a snipe without any manual config."""
        return self.cadence == "daily" and self.days_ahead is not None and self.at is not None

    def describe(self) -> str:
        bits = []
        if self.days_ahead is not None:
            bits.append(f"{self.days_ahead} days ahead")
        if self.at is not None:
            bits.append(f"at {self.at:%H:%M} ET")
        if self.cadence == "monthly":
            bits.append("(monthly release)")
        return ", ".join(bits) if bits else "nothing conclusive"


def _count(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        value = int(token)
        return value if 1 <= value <= 365 else None
    return _WORD_NUMBERS.get(token)


def parse_policy_text(text: str) -> DropPolicy | None:
    """Pull a release policy out of one blob of prose. None if nothing found."""
    if not text or not _RELEVANT_RE.search(text):
        return None

    # Drop reminder/cancellation/deposit sentences before parsing, keeping the
    # rest joined so a policy split across two sentences still merges.
    kept = [
        sent for sent in _SENTENCE_SPLIT_RE.split(text)
        if not _DISQUALIFY_RE.search(sent)
    ]
    text = " ".join(kept)
    if not text or not _RELEVANT_RE.search(text):
        return None

    days: int | None = None
    match = _DAYS_RE.search(text)
    if match:
        days = _count(match.group(1))
    if days is None:
        match = _WEEKS_RE.search(text)
        if match:
            weeks = _count(match.group(1))
            days = weeks * 7 if weeks else None

    at: dtime | None = None
    time_match = _TIME_RE.search(text)
    if time_match:
        hour = int(time_match.group(1)) % 12
        if time_match.group(3).lower() == "p":
            hour += 12
        minute = int(time_match.group(2) or 0)
        if hour < 24 and minute < 60:
            at = dtime(hour, minute)
    elif _MIDNIGHT_RE.search(text):
        at = dtime(0, 0)
    elif _NOON_RE.search(text):
        at = dtime(12, 0)

    monthly = bool(_MONTHLY_RE.search(text))

    if days is None and at is None and not monthly:
        return None

    return DropPolicy(
        days_ahead=days,
        at=at,
        cadence="monthly" if monthly else ("daily" if days is not None else "unknown"),
        source="text",
        snippet=_snippet_around(text, match or time_match),
    )


def _snippet_around(text: str, match: re.Match | None) -> str:
    """The sentence containing the match, trimmed to something quotable."""
    text = re.sub(r"\s+", " ", text).strip()
    if match is None:
        return text[:160]
    start = text.rfind(".", 0, min(match.start(), len(text))) + 1
    end = text.find(".", min(match.end(), len(text)))
    if end == -1:
        end = len(text)
    return text[start:end].strip()[:200]


def _iter_strings(node: Any):
    """Every string in a nested JSON structure. The venue payload's shape has
    shifted before and will again; walking everything beats chasing paths."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _iter_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_strings(value)


def _find_int_key(node: Any, keys: frozenset[str]) -> int | None:
    """First int found under any of the given keys, anywhere in the tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in keys and isinstance(value, (int, float)) and 0 < int(value) <= 365:
                return int(value)
        for value in node.values():
            found = _find_int_key(value, keys)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_int_key(value, keys)
            if found is not None:
                return found
    return None


_LEAD_TIME_KEYS = frozenset({"lead_time_in_days", "lead_time_days", "advance_days"})


def extract_policy(venue_json: dict[str, Any]) -> DropPolicy | None:
    """Best policy recoverable from a full `/3/venue` payload.

    The structured lead-time field, where present, is authoritative for
    `days_ahead` -- it drives the site's own calendar. The prose supplies the
    release *time*, which has no structured home. When both exist they merge.
    """
    structured_days = _find_int_key(venue_json, _LEAD_TIME_KEYS)

    best_text: DropPolicy | None = None
    for blob in _iter_strings(venue_json):
        if len(blob) < 20:
            continue
        parsed = parse_policy_text(blob)
        if parsed is None:
            continue
        if best_text is None or _richness(parsed) > _richness(best_text):
            best_text = parsed

    if structured_days is None and best_text is None:
        return None

    if best_text is None:
        return DropPolicy(
            days_ahead=structured_days, at=None, cadence="daily",
            source="structured", snippet="",
        )
    if structured_days is None:
        return best_text

    return DropPolicy(
        # Trust the machine-readable field over prose for the day count; keep
        # the prose's time and cadence, which is all the prose is needed for.
        days_ahead=structured_days,
        at=best_text.at,
        cadence=best_text.cadence if best_text.cadence != "unknown" else "daily",
        source="structured+text",
        snippet=best_text.snippet,
    )


def _richness(policy: DropPolicy) -> int:
    return (policy.days_ahead is not None) + (policy.at is not None) * 2
