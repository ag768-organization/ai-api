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
    payload_hash  TEXT    NOT NULL,
    -- Added in the cache/batch phase. Older ledgers get them from
    -- `_migrate` with the same defaults, see USAGE_EVENT_COLUMNS_ADDED.
    cache_write_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0 CHECK (cache_read_tokens  >= 0),
    batch              INTEGER NOT NULL DEFAULT 0 CHECK (batch IN (0, 1))
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

-- Frozen aggregates for the token categories beyond standard input and
-- output (cache writes, cache reads and every batch category). Long format,
-- one row per non-zero category, so adding a category never needs a new
-- column. Together with period_aggregates this is the full frozen usage.
CREATE TABLE IF NOT EXISTS period_aggregate_units (
    period      TEXT    NOT NULL,
    customer_id TEXT    NOT NULL,
    model       TEXT    NOT NULL,
    category    TEXT    NOT NULL,
    tokens      INTEGER NOT NULL CHECK (tokens >= 0),
    PRIMARY KEY (period, customer_id, model, category)
);

-- Customer accounts. billing_mode 'postpaid' (the default for a customer
-- with no row) bills usage in arrears; 'prepaid' stops serving requests once
-- included and prepaid credits run out.
CREATE TABLE IF NOT EXISTS accounts (
    customer_id  TEXT PRIMARY KEY,
    billing_mode TEXT NOT NULL CHECK (billing_mode IN ('postpaid', 'prepaid')),
    opened_at    TEXT NOT NULL
);

-- A subscription is one fixed term of a plan. Renewal is a new row, so a
-- row never changes. The plan's terms are copied in at signup, so a later
-- plan change can't alter a running subscription.
CREATE TABLE IF NOT EXISTS subscriptions (
    subscription_id TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL,
    plan_id         TEXT NOT NULL,
    fee             TEXT NOT NULL,      -- whole-term fee, Decimal dollars, billed in advance
    included_credit TEXT NOT NULL,      -- usage credit per period, Decimal dollars
    term_months     INTEGER NOT NULL CHECK (term_months > 0),
    start_period    TEXT NOT NULL,      -- first period of the term ('YYYY-MM')
    created_at      TEXT NOT NULL
);

-- Prepaid credit purchases. Usable from first_period through last_period
-- inclusive; whatever is left after last_period has expired.
CREATE TABLE IF NOT EXISTS credit_grants (
    grant_id     TEXT PRIMARY KEY,
    customer_id  TEXT NOT NULL,
    amount       TEXT NOT NULL,         -- Decimal dollars, cents
    granted_at   TEXT NOT NULL,
    first_period TEXT NOT NULL,
    last_period  TEXT NOT NULL
);

-- Non-usage invoice lines frozen at close: subscription fees (positive) and
-- credits applied against usage (negative). ref is the subscription_id or
-- grant_id the charge comes from.
CREATE TABLE IF NOT EXISTS invoice_charges (
    period      TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN
                    ('subscription_fee', 'included_credit', 'prepaid_credit')),
    ref         TEXT NOT NULL,
    amount      TEXT NOT NULL,
    PRIMARY KEY (period, customer_id, kind, ref)
);

CREATE TABLE IF NOT EXISTS invoices (
    period      TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    total       TEXT NOT NULL,             -- usage lines + charges, never re-rounded
    line_count  INTEGER NOT NULL,
    PRIMARY KEY (period, customer_id)
);
"""

# Every billing table is append-only, enforced the same way as usage_events.
APPEND_ONLY_TABLES = ("ingest_batches", "period_closes", "period_aggregates",
                      "period_aggregate_units", "invoice_lines", "invoices",
                      "accounts", "subscriptions", "credit_grants", "invoice_charges")
SCHEMA += "".join(
    f"""
CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only: UPDATE not allowed'); END;
CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only: DELETE not allowed'); END;
"""
    for t in APPEND_ONLY_TABLES
)


# Columns added to usage_events after Phase 2. ADD COLUMN with a constant
# default changes no existing row (and fires no UPDATE trigger): old events
# read back as uncached, non-batch calls, which is what they were.
USAGE_EVENT_COLUMNS_ADDED = {
    "cache_write_tokens": "INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0)",
    "cache_read_tokens": "INTEGER NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0)",
    "batch": "INTEGER NOT NULL DEFAULT 0 CHECK (batch IN (0, 1))",
}


def _migrate(conn: sqlite3.Connection) -> None:
    have = {r["name"] for r in conn.execute("PRAGMA table_info(usage_events)")}
    for name, ddl in USAGE_EVENT_COLUMNS_ADDED.items():
        if name not in have:
            conn.execute(f"ALTER TABLE usage_events ADD COLUMN {name} {ddl}")
    conn.commit()


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
    _migrate(conn)
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
             cache_write_tokens, cache_read_tokens, batch,
             occurred_at, received_at, payload_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id) DO NOTHING
        """,
        (
            event.event_id,
            event.customer_id,
            event.model,
            event.input_tokens,
            event.output_tokens,
            event.cache_write_tokens,
            event.cache_read_tokens,
            int(event.batch),
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
        "SELECT COALESCE(SUM(input_tokens + output_tokens + cache_write_tokens"
        " + cache_read_tokens), 0) FROM usage_events"
    ).fetchone()[0]

