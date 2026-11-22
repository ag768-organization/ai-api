"""Aggregate: total usage per customer, per model, per billing period.

Reads the ledger and never writes to it. Two rules decide which period an
event is billed in:

- Billing period is the UTC calendar month of `occurred_at`. **[GUESS]**
- Late events: if that period was already closed when the event was
  *received*, the event is billed in the next period that was still open at
  that moment, and flagged late. Closed periods never reopen.

Because the rule depends only on ledger columns (`occurred_at`,
`received_at`) and the append-only `period_closes` table, the answer for an
event never changes once it is in the ledger: every event lands in exactly
one period, and replaying events can't move them.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping


def period_of(ts: datetime) -> str:
    """'YYYY-MM' for an aware timestamp, in UTC."""
    if ts.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    ts = ts.astimezone(timezone.utc)
    return f"{ts.year:04d}-{ts.month:02d}"


def next_period(period: str) -> str:
    year, month = (int(p) for p in period.split("-"))
    return f"{year + 1:04d}-01" if month == 12 else f"{year:04d}-{month + 1:02d}"


def period_end(period: str) -> datetime:
    """First instant after the period (exclusive end), UTC."""
    year, month = (int(p) for p in next_period(period).split("-"))
    return datetime(year, month, 1, tzinfo=timezone.utc)


def closes(conn: sqlite3.Connection) -> dict[str, datetime]:
    """Closed periods and when each was closed."""
    return {
        r["period"]: datetime.fromisoformat(r["closed_at"])
        for r in conn.execute("SELECT period, closed_at FROM period_closes")
    }


def billing_period(occurred_at: datetime, received_at: datetime,
                   closed: Mapping[str, datetime]) -> str:
    """The one period an event is billed in.

    Start at the event's own period and move forward past every period that
    was already closed when the event arrived. An event received at the exact
    instant of a close counts as after it."""
    period = period_of(occurred_at)
    while period in closed and closed[period] <= received_at:
        period = next_period(period)
    return period


# Token categories. The four base categories come straight from an event's
# token fields; a batch event's tokens go to the `batch_` version of each.
BASE_CATEGORIES = ("input", "output", "cache_write", "cache_read")
CATEGORIES = BASE_CATEGORIES + tuple(f"batch_{c}" for c in BASE_CATEGORIES)
# Categories frozen in period_aggregates' own columns; the rest go to
# period_aggregate_units.
COLUMN_CATEGORIES = ("input", "output")


def category_for(base: str, batch: bool) -> str:
    return f"batch_{base}" if batch else base


@dataclass(frozen=True)
class Usage:
    """Aggregated units for one customer, model and billing period.

    `input_tokens` and `output_tokens` are standard (non-batch) calls only;
    every other category has its own field and defaults to zero."""

    period: str
    customer_id: str
    model: str
    input_tokens: int
    output_tokens: int
    event_count: int
    late_count: int
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    batch_input_tokens: int = 0
    batch_output_tokens: int = 0
    batch_cache_write_tokens: int = 0
    batch_cache_read_tokens: int = 0

    def tokens(self, category: str) -> int:
        if category not in CATEGORIES:
            raise KeyError(category)
        return getattr(self, f"{category}_tokens")

    @property
    def total_tokens(self) -> int:
        return sum(self.tokens(c) for c in CATEGORIES)


@dataclass(frozen=True)
class AssignedEvent:
    event_id: str
    customer_id: str
    model: str
    input_tokens: int
    output_tokens: int
    period: str
    late: bool
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    batch: bool = False

    def units(self) -> dict[str, int]:
        """Tokens by category for this one event."""
        return {category_for(base, self.batch): getattr(self, f"{base}_tokens")
                for base in BASE_CATEGORIES}


def assign(conn: sqlite3.Connection) -> list[AssignedEvent]:
    """Every ledger event with the period it is billed in."""
    closed = closes(conn)
    out = []
    for r in conn.execute(
        "SELECT event_id, customer_id, model, input_tokens, output_tokens,"
        " cache_write_tokens, cache_read_tokens, batch,"
        " occurred_at, received_at FROM usage_events ORDER BY seq"
    ):
        occurred = datetime.fromisoformat(r["occurred_at"])
        period = billing_period(occurred, datetime.fromisoformat(r["received_at"]), closed)
        out.append(AssignedEvent(r["event_id"], r["customer_id"], r["model"],
                                 r["input_tokens"], r["output_tokens"],
                                 period, period != period_of(occurred),
                                 r["cache_write_tokens"], r["cache_read_tokens"],
                                 bool(r["batch"])))
    return out


def summarise(events: Iterable[AssignedEvent]) -> list[Usage]:
    tokens: dict[tuple[str, str, str], dict[str, int]] = {}
    counts: dict[tuple[str, str, str], list[int]] = {}
    for e in events:
        key = (e.period, e.customer_id, e.model)
        t = tokens.setdefault(key, dict.fromkeys(CATEGORIES, 0))
        for cat, n in e.units().items():
            t[cat] += n
        c = counts.setdefault(key, [0, 0])
        c[0] += 1
        c[1] += int(e.late)
    return [Usage(p, cu, m, input_tokens=tokens[(p, cu, m)]["input"],
                  output_tokens=tokens[(p, cu, m)]["output"],
                  event_count=counts[(p, cu, m)][0], late_count=counts[(p, cu, m)][1],
                  **{f"{cat}_tokens": n for cat, n in tokens[(p, cu, m)].items()
                     if cat not in COLUMN_CATEGORIES})
            for (p, cu, m) in sorted(tokens)]


def aggregate(conn: sqlite3.Connection, period: str | None = None) -> list[Usage]:
    """Usage per (period, customer, model), sorted. Optionally one period."""
    events = assign(conn)
    if period is not None:
        events = [e for e in events if e.period == period]
    return summarise(events)
