"""Durable state: what we have booked, and what we have already alerted on.

SQLite because the bot must survive a container restart without re-notifying you
about every slot it has ever seen, and without double-booking a target it
already satisfied.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .models import Booking, Slot
from .timeutil import now_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    resy_token     TEXT PRIMARY KEY,
    reservation_id TEXT,
    target_name    TEXT NOT NULL,
    venue_id       INTEGER NOT NULL,
    day            TEXT NOT NULL,
    start_time     TEXT NOT NULL,
    seating_type   TEXT NOT NULL,
    party_size     INTEGER NOT NULL,
    booked_at      TEXT NOT NULL,
    cancelled_at   TEXT
);

CREATE TABLE IF NOT EXISTS seen_slots (
    slot_key    TEXT NOT NULL,
    target_name TEXT NOT NULL,
    seen_at     TEXT NOT NULL,
    PRIMARY KEY (slot_key, target_name)
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    level      TEXT NOT NULL,
    target     TEXT,
    message    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_seen_at ON seen_slots (seen_at);
CREATE INDEX IF NOT EXISTS idx_events_at ON events (at);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --------------------------------------------------------------- bookings

    def record_booking(self, booking: Booking) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO bookings
               (resy_token, reservation_id, target_name, venue_id, day, start_time,
                seating_type, party_size, booked_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                booking.resy_token,
                booking.reservation_id,
                booking.target_name,
                booking.slot.venue_id,
                booking.slot.day.isoformat(),
                booking.slot.start.isoformat(),
                booking.slot.seating_type,
                booking.slot.party_size,
                booking.booked_at.isoformat(),
            ),
        )

    def booking_count(self, target_name: str) -> int:
        """Live bookings for a target. Cancelled ones free up the slot again."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM bookings WHERE target_name = ? AND cancelled_at IS NULL",
            (target_name,),
        ).fetchone()
        return int(row["n"])

    def active_bookings(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM bookings WHERE cancelled_at IS NULL ORDER BY day, start_time"
        ).fetchall()

    def mark_cancelled(self, resy_token: str) -> None:
        self.conn.execute(
            "UPDATE bookings SET cancelled_at = ? WHERE resy_token = ?",
            (now_utc().isoformat(), resy_token),
        )

    def has_booking_on(self, target_name: str, day: str) -> bool:
        row = self.conn.execute(
            """SELECT 1 FROM bookings
               WHERE target_name = ? AND day = ? AND cancelled_at IS NULL LIMIT 1""",
            (target_name, day),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------ slot dedupe

    def is_new_slot(self, slot: Slot, target_name: str, *, ttl_hours: float = 6.0) -> bool:
        """True if we have not alerted on this slot recently.

        Re-arms after `ttl_hours` so a table that opens, gets taken, and opens
        again next week still reaches you.
        """
        row = self.conn.execute(
            "SELECT seen_at FROM seen_slots WHERE slot_key = ? AND target_name = ?",
            (slot.key, target_name),
        ).fetchone()

        if row is not None:
            try:
                seen_at = datetime.fromisoformat(row["seen_at"])
            except ValueError:
                seen_at = None
            if seen_at is not None and now_utc() - seen_at < timedelta(hours=ttl_hours):
                return False

        self.conn.execute(
            "INSERT OR REPLACE INTO seen_slots (slot_key, target_name, seen_at) VALUES (?,?,?)",
            (slot.key, target_name, now_utc().isoformat()),
        )
        return True

    def prune(self, *, older_than_days: int = 7) -> int:
        cutoff = (now_utc() - timedelta(days=older_than_days)).isoformat()
        cur = self.conn.execute("DELETE FROM seen_slots WHERE seen_at < ?", (cutoff,))
        self.conn.execute("DELETE FROM events WHERE at < ?", (cutoff,))
        return cur.rowcount

    # ----------------------------------------------------------------- events

    def log_event(self, level: str, message: str, target: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO events (at, level, target, message) VALUES (?,?,?,?)",
            (now_utc().isoformat(), level, target, message),
        )

    def recent_events(self, limit: int = 25) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
