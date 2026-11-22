"""Append-only ledger of raw usage events (SQLite).

The ledger is the system of record for billable usage. It is insert-only:
SQLite triggers abort any UPDATE or DELETE, so a bug (or a person) can't
quietly change history. Corrections will be new events in a later phase.

The same database also holds the downstream billing tables (ingest batch
counts, period closes, frozen aggregates and invoices). They are append-only
too: once a period is closed, nothing about it can change.
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

-- One row per ingest batch, so reconciliation can prove every input was
-- counted: received = accepted + duplicate + conflict + invalid.
CREATE TABLE IF NOT EXISTS ingest_batches (
    batch_seq   INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT    NOT NULL,
    received    INTEGER NOT NULL,
    accepted    INTEGER NOT NULL,
    duplicate   INTEGER NOT NULL,
    conflict    INTEGER NOT NULL,
    invalid     INTEGER NOT NULL
);

-- A closed billing period. Closing is one-way: no UPDATE or DELETE, so a
-- closed period can never be reopened.
CREATE TABLE IF NOT EXISTS period_closes (
    period             TEXT PRIMARY KEY,   -- 'YYYY-MM' (UTC calendar month)
    closed_at          TEXT NOT NULL,
    price_book_version TEXT NOT NULL
);

-- Aggregates frozen at close: total units per customer, model, period.
CREATE TABLE IF NOT EXISTS period_aggregates (
    period        TEXT    NOT NULL,
    customer_id   TEXT    NOT NULL,
    model         TEXT    NOT NULL,
    input_tokens  INTEGER NOT NULL CHECK (input_tokens  >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    event_count   INTEGER NOT NULL,
    late_count    INTEGER NOT NULL,
    PRIMARY KEY (period, customer_id, model)
);

-- Invoice lines frozen at close. amount is a Decimal string in dollars,
-- already rounded to cents (never a float).
CREATE TABLE IF NOT EXISTS invoice_lines (
    period             TEXT    NOT NULL,
    customer_id        TEXT    NOT NULL,
    model              TEXT    NOT NULL,
    category           TEXT    NOT NULL,   -- token category: input, output
    tokens             INTEGER NOT NULL CHECK (tokens >= 0),
    free_tokens        INTEGER NOT NULL CHECK (free_tokens >= 0),
    amount             TEXT    NOT NULL,
    price_book_version TEXT    NOT NULL,
    PRIMARY KEY (period, customer_id, model, category)
);

CREATE TABLE IF NOT EXISTS invoices (
    period      TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    total       TEXT NOT NULL,             -- sum of line amounts, never re-rounded
    line_count  INTEGER NOT NULL,
    PRIMARY KEY (period, customer_id)
);
"""

# Every billing table is append-only, enforced the same way as usage_events.
APPEND_ONLY_TABLES = ("ingest_batches", "period_closes", "period_aggregates",
                      "invoice_lines", "invoices")
SCHEMA += "".join(
    f"""
CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only: UPDATE not allowed'); END;
CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only: DELETE not allowed'); END;
"""
    for t in APPEND_ONLY_TABLES
)


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

