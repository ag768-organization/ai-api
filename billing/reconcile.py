"""Reconcile: the daily completeness and accuracy check.

Every check below must come out exact. Any difference is a gap, and any gap
fails the run; there are no warnings.

Units are compared per customer, model and token category: input, output,
cache writes, cache reads and the batch version of each.

Completeness
  1. Each ingest batch balances: received = accepted + duplicate + conflict
     + invalid.
  2. Accepted events across all batches = events in the ledger.
  3. Raw ledger units = aggregated units across all periods (every event
     lands in exactly one period).
  4. For each closed period, aggregates recomputed from the ledger = the
     aggregates frozen at close = units on the frozen invoice lines.
  5. For each open period, raw units = aggregated units = draft invoiced units.

Accuracy
  6. Each frozen invoice line re-rates to the same amount with the price book
     version it records, and that version is the one the close used.
  7. Each invoice total = the sum of its lines, and its line count matches.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from billing import aggregate, invoice, ledger, rating


@dataclass(frozen=True)
class Gap:
    check: str
    period: str | None
    customer_id: str | None
    detail: str


@dataclass
class Report:
    gaps: list[Gap] = field(default_factory=list)
    checked: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.gaps

    def gap(self, check: str, detail: str, period: str | None = None,
            customer_id: str | None = None) -> None:
        self.gaps.append(Gap(check, period, customer_id, detail))


# (customer, model, category) -> tokens. Every check compares all token
# categories (input, output, cache and batch), with a missing key read as 0.
Units = dict[tuple[str, str, str], int]


def _usage_units(usage: list[aggregate.Usage]) -> Units:
    out: Units = defaultdict(int)
    for u in usage:
        for cat in aggregate.CATEGORIES:
            out[(u.customer_id, u.model, cat)] += u.tokens(cat)
    return dict(out)


def _line_units(invoices: list[invoice.Invoice]) -> Units:
    out: Units = defaultdict(int)
    for inv in invoices:
        for ln in inv.lines:
            out[(ln.customer_id, ln.model, ln.category)] += ln.tokens
    return dict(out)


def _raw_units(events: list[aggregate.AssignedEvent]) -> Units:
    out: Units = defaultdict(int)
    for e in events:
        for cat, n in e.units().items():
            out[(e.customer_id, e.model, cat)] += n
    return dict(out)


def _compare(report: Report, check: str, period: str, a_name: str, a: Units,
             b_name: str, b: Units) -> None:
    for key in sorted(set(a) | set(b)):
        if a.get(key, 0) != b.get(key, 0):
            customer, model, cat = key
            report.gap(check, f"{model}/{cat}: {a_name}={a.get(key, 0)} "
                              f"{b_name}={b.get(key, 0)}", period, customer)


def reconcile(conn: sqlite3.Connection,
              books: dict[str, rating.PriceBook] | None = None) -> Report:
    books = rating.price_books() if books is None else books
    r = Report()

    # 1-2. Ingest counts
    batches = conn.execute("SELECT * FROM ingest_batches ORDER BY batch_seq").fetchall()
    for b in batches:
        if b["received"] != b["accepted"] + b["duplicate"] + b["conflict"] + b["invalid"]:
            r.gap("ingest_balance", f"batch {b['batch_seq']} does not balance")
    accepted = sum(b["accepted"] for b in batches)
    if accepted != ledger.count(conn):
        r.gap("ingest_vs_ledger", f"accepted={accepted} ledger_events={ledger.count(conn)}")
    r.checked["ingest_batches"] = len(batches)

    # 3. Every ledger event in exactly one period
    events = aggregate.assign(conn)
    usage = aggregate.summarise(events)
    agg_tokens = sum(u.total_tokens for u in usage)
    agg_events = sum(u.event_count for u in usage)
    if (agg_tokens, agg_events) != (ledger.total_tokens(conn), ledger.count(conn)):
        r.gap("ledger_vs_aggregate",
              f"ledger={ledger.count(conn)} events/{ledger.total_tokens(conn)} tokens "
              f"aggregated={agg_events} events/{agg_tokens} tokens")

    by_period: dict[str, list[aggregate.AssignedEvent]] = defaultdict(list)
    for e in events:
        by_period[e.period].append(e)
    closes = {row["period"]: row["price_book_version"]
              for row in conn.execute("SELECT period, price_book_version FROM period_closes")}

    for period in sorted(set(by_period) | set(closes)):
        raw = _raw_units(by_period.get(period, []))
        recomputed = aggregate.summarise(by_period.get(period, []))
        if period in closes:
            # 4. Closed period: ledger vs frozen aggregates vs frozen lines
            frozen = invoice.frozen_usage(conn, period)
            invoices = invoice.stored(conn, period)
            _compare(r, "raw_vs_aggregated", period, "raw", raw,
                     "aggregated", _usage_units(frozen))
            _compare(r, "aggregated_vs_invoiced", period, "aggregated",
                     _usage_units(frozen), "invoiced", _line_units(invoices))
            if recomputed != frozen:
                r.gap("aggregate_event_counts", "recomputed aggregates differ from frozen", period)
            _check_accuracy(r, conn, period, closes[period], invoices, books)
        else:
            # 5. Open period: raw vs aggregated vs draft invoice
            try:
                book = rating.book_for_period(period, books)
                drafts = invoice.build(rating.rate(recomputed, book))
            except rating.PriceBookError as exc:
                # e.g. cache tokens in a period whose book has no cache price
                r.gap("unrated_usage", str(exc), period)
                drafts = []
            _compare(r, "raw_vs_aggregated", period, "raw", raw,
                     "aggregated", _usage_units(recomputed))
            _compare(r, "aggregated_vs_invoiced", period, "aggregated",
                     _usage_units(recomputed), "invoiced", _line_units(drafts))
    r.checked["periods"] = len(set(by_period) | set(closes))
    r.checked["closed_periods"] = len(closes)
    r.checked["events"] = len(events)
    return r


def _check_accuracy(r: Report, conn: sqlite3.Connection, period: str, close_version: str,
                    invoices: list[invoice.Invoice],
                    books: dict[str, rating.PriceBook]) -> None:
    # 6. Re-rate every frozen line with the book it records
    for inv in invoices:
        for ln in inv.lines:
            if ln.price_book_version != close_version:
                r.gap("price_book_version", f"{ln.model}/{ln.category} rated with "
                      f"{ln.price_book_version}, close used {close_version}", period, ln.customer_id)
                continue
            book = books.get(ln.price_book_version)
            if book is None:
                r.gap("price_book_missing", f"no price book {ln.price_book_version}", period)
                continue
            try:
                exact, free = rating.exact_amount(ln.tokens, book.rate(ln.model, ln.category))
            except (rating.PriceBookError, KeyError) as exc:
                r.gap("line_amount", f"{ln.model}/{ln.category}: cannot re-rate ({exc})",
                      period, ln.customer_id)
                continue
            if (rating.round_line(exact), free) != (ln.amount, ln.free_tokens):
                r.gap("line_amount", f"{ln.model}/{ln.category}: stored {ln.amount} "
                      f"re-rated {rating.round_line(exact)}", period, ln.customer_id)

    # 7. Header totals = sum of lines
    built = {(inv.customer_id): inv for inv in invoices}
    headers = conn.execute(
        "SELECT customer_id, total, line_count FROM invoices WHERE period = ?", (period,)
    ).fetchall()
    seen = set()
    for h in headers:
        seen.add(h["customer_id"])
        inv = built.get(h["customer_id"])
        lines_total = inv.total if inv else Decimal("0.00")
        line_count = len(inv.lines) if inv else 0
        if Decimal(h["total"]) != lines_total or h["line_count"] != line_count:
            r.gap("invoice_total", f"header {h['total']} ({h['line_count']} lines), "
                  f"lines sum {lines_total} ({line_count} lines)", period, h["customer_id"])
    for customer in set(built) - seen:
        r.gap("invoice_missing", "lines with no invoice header", period, customer)
