"""Invoice: line items and a total per customer per billing period.

The invoice total is the sum of its already-rounded line items, never
rounded again (CLAUDE.md invariant 3).

`close_period` is the one-way step that freezes a period: it writes the
period's aggregates, rated lines and invoice totals to append-only tables in
one transaction, then records the close. After that, late events go to the
next open period and the frozen invoice cannot change.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from billing import aggregate, rating


@dataclass(frozen=True)
class Invoice:
    period: str
    customer_id: str
    lines: tuple[rating.RatedLine, ...]

    @property
    def total(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal("0.00"))

    @property
    def tokens(self) -> int:
        return sum(line.tokens for line in self.lines)


def build(lines: list[rating.RatedLine]) -> list[Invoice]:
    """Group rated lines into invoices, one per (period, customer)."""
    grouped: dict[tuple[str, str], list[rating.RatedLine]] = {}
    for line in lines:
        grouped.setdefault((line.period, line.customer_id), []).append(line)
    return [Invoice(p, c, tuple(ls)) for (p, c), ls in sorted(grouped.items())]


def draft(conn: sqlite3.Connection, period: str,
          book: rating.PriceBook | None = None) -> list[Invoice]:
    """What the period's invoices would be if it closed now. Writes nothing."""
    book = book or rating.book_for_period(period)
    return build(rating.rate(aggregate.aggregate(conn, period), book))


class PeriodCloseError(Exception):
    pass


def close_period(conn: sqlite3.Connection, period: str, *,
                 closed_at: datetime | None = None,
                 book: rating.PriceBook | None = None) -> list[Invoice]:
    """Freeze `period` and return its final invoices.

    Refuses to close a period that hasn't ended, one that is already closed,
    or with a `closed_at` earlier than an event already in the ledger (which
    would move that event to another period after the fact)."""
    book = book or rating.book_for_period(period)
    if conn.in_transaction:
        raise PeriodCloseError("commit or roll back the open transaction first")
    # Take the write lock first so no ingest batch can commit between reading
    # the ledger and recording the close.
    conn.execute("BEGIN IMMEDIATE")
    try:
        closed_at = (closed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if closed_at < aggregate.period_end(period):
            raise PeriodCloseError(f"{period} has not ended yet")
        if period in aggregate.closes(conn):
            raise PeriodCloseError(f"{period} is already closed")
        latest = conn.execute("SELECT MAX(received_at) FROM usage_events").fetchone()[0]
        if latest is not None and datetime.fromisoformat(latest) >= closed_at:
            raise PeriodCloseError("closed_at must be after every event already received")

        usage = aggregate.aggregate(conn, period)
        invoices = build(rating.rate(usage, book))
        conn.executemany(
            "INSERT INTO period_aggregates (period, customer_id, model, input_tokens,"
            " output_tokens, event_count, late_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(u.period, u.customer_id, u.model, u.input_tokens, u.output_tokens,
              u.event_count, u.late_count) for u in usage],
        )
        conn.executemany(
            "INSERT INTO period_aggregate_units (period, customer_id, model, category, tokens)"
            " VALUES (?, ?, ?, ?, ?)",
            [(u.period, u.customer_id, u.model, cat, u.tokens(cat)) for u in usage
             for cat in aggregate.CATEGORIES
             if cat not in aggregate.COLUMN_CATEGORIES and u.tokens(cat)],
        )
        conn.executemany(
            "INSERT INTO invoice_lines (period, customer_id, model, category, tokens,"
            " free_tokens, amount, price_book_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(ln.period, ln.customer_id, ln.model, ln.category, ln.tokens, ln.free_tokens,
              str(ln.amount), ln.price_book_version) for inv in invoices for ln in inv.lines],
        )
        conn.executemany(
            "INSERT INTO invoices (period, customer_id, total, line_count) VALUES (?, ?, ?, ?)",
            [(inv.period, inv.customer_id, str(inv.total), len(inv.lines)) for inv in invoices],
        )
        conn.execute(
            "INSERT INTO period_closes (period, closed_at, price_book_version) VALUES (?, ?, ?)",
            (period, closed_at.isoformat(), book.version),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return invoices


def frozen_usage(conn: sqlite3.Connection, period: str) -> list[aggregate.Usage]:
    """The aggregates of a closed period, as frozen at close."""
    extra: dict[tuple[str, str], dict[str, int]] = {}
    for r in conn.execute(
            "SELECT customer_id, model, category, tokens FROM period_aggregate_units"
            " WHERE period = ?", (period,)):
        extra.setdefault((r["customer_id"], r["model"]), {})[f"{r['category']}_tokens"] = r["tokens"]
    rows = [aggregate.Usage(**dict(row), **extra.pop((row["customer_id"], row["model"]), {}))
            for row in conn.execute(
                "SELECT period, customer_id, model, input_tokens, output_tokens,"
                " event_count, late_count FROM period_aggregates WHERE period = ?"
                " ORDER BY customer_id, model", (period,))]
    # Units with no period_aggregates row still count, so reconcile sees them.
    rows += [aggregate.Usage(period, c, m, 0, 0, 0, 0, **units)
             for (c, m), units in sorted(extra.items())]
    return rows


def stored(conn: sqlite3.Connection, period: str) -> list[Invoice]:
    """The frozen invoices of a closed period, as written at close."""
    lines = [
        rating.RatedLine(r["period"], r["customer_id"], r["model"], r["category"],
                         r["tokens"], r["free_tokens"], Decimal(r["amount"]),
                         r["price_book_version"])
        for r in conn.execute(
            "SELECT * FROM invoice_lines WHERE period = ?"
            " ORDER BY customer_id, model, category", (period,))
    ]
    # Same line order as rating.rate, so a stored invoice equals the one built
    # at close. Categories the code doesn't know sort last (reconcile flags them).
    order = {c: i for i, c in enumerate(aggregate.CATEGORIES)}
    lines.sort(key=lambda ln: (ln.customer_id, ln.model, order.get(ln.category, len(order)),
                               ln.category))
    return build(lines)
