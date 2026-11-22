"""Append-only ledger of raw usage events (SQLite).

The ledger is the system of record for billable usage. It is insert-only:
SQLite triggers abort any UPDATE or DELETE, so a bug (or a person) can't
quietly change history. Corrections will be new events in a later phase.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from billing.events import UsageEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      TEXT    NOT NULL UNIQUE,
    customer_id   TEXT    NOT NULL,
    model         TEXT    NOT NULL,
    input_tokens  INTEGER NOT NULL CHECK (input_tokens  >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    occurred_at   TEXT    NOT NULL,   -- when the call happened (UTC ISO-8601)
    received_at   TEXT    NOT NULL,   -- when we ingested it (for late-arrival rules)
    payload_hash  TEXT    NOT NULL
);

CREATE TRIGGER IF NOT EXISTS usage_events_no_update
BEFORE UPDATE ON usage_events
BEGIN
    SELECT RAISE(ABORT, 'usage_events is append-only: UPDATE not allowed');
END;

CREATE TRIGGER IF NOT EXISTS usage_events_no_delete
BEFORE DELETE ON usage_events
BEGIN
    SELECT RAISE(ABORT, 'usage_events is append-only: DELETE not allowed');
END;
"""


class LedgerImmutableError(Exception):
    """Raised when something tries to change or remove a ledger row."""


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # Without this, INSERT OR REPLACE deletes the old row *without* firing the
    # no-delete trigger, silently overwriting history. Must be set on every
    # connection (it is not stored in the database file).
    conn.execute("PRAGMA recursive_triggers = ON")
    conn.executescript(SCHEMA)
    return conn


def append(conn: sqlite3.Connection, event: UsageEvent, received_at: datetime | None = None) -> bool:
    """Insert one event. Returns True if inserted, False if the event_id was
    already present (the caller decides whether that is a duplicate or a
    conflict). Uses ON CONFLICT on event_id only, so any *other* constraint
    failure still raises instead of being swallowed."""
    received_at = received_at or datetime.now(timezone.utc)
    cur = conn.execute(
        """
        INSERT INTO usage_events
            (event_id, customer_id, model, input_tokens, output_tokens,
             occurred_at, received_at, payload_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id) DO NOTHING
        """,
        (
            event.event_id,
            event.customer_id,
            event.model,
            event.input_tokens,
            event.output_tokens,
            event.occurred_at.isoformat(),
            received_at.astimezone(timezone.utc).isoformat(),
            event.payload_hash(),
        ),
    )
    return cur.rowcount == 1


def get_payload_hash(conn: sqlite3.Connection, event_id: str) -> str | None:
    row = conn.execute(
        "SELECT payload_hash FROM usage_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    return row["payload_hash"] if row else None


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]


def total_tokens(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM usage_events"
    ).fetchone()[0]
