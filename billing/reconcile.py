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
  7. Each invoice total = the sum of its usage lines and charges, and its
     line count matches.

Subscriptions and credits (frozen and draft invoices alike)
  8. Every subscription is charged its recorded fee exactly once, in its
     start period, and no fee is charged for anything else.
  9. Included credit = min(the active term's included credit, rated usage),
     and only while a term is active.
 10. Every prepaid draw comes from the customer's own grant, inside the
     grant's usable periods, and no grant is drawn past its amount.
 11. Credits on an invoice never exceed its rated usage, and prepaid credit
     only covers what included credit left.

Revenue recognition (revrec.py)
 12. Each subscription's recognized pieces add up to its fee exactly and
     fall inside its term.
 13. Each grant: drawn + breakage + still deferred = amount, and nothing is
     left deferred once its last period has closed.
 14. Per invoice: usage + prepaid_usage revenue = rated usage - included
     credit. No revenue line is negative.
 15. Rollforward through every period: billed + prepaid cash = recognized +
     deferred.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from billing import aggregate, credits, invoice, ledger, rating, revrec, subscriptions


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
    _check_credits(r, conn, books)
    _check_revenue(r, conn, books)
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


def _check_credits(r: Report, conn: sqlite3.Connection,
                   books: dict[str, rating.PriceBook]) -> None:
    alloc = credits.allocate(conn, books)
    subs = {s.subscription_id: s for s in subscriptions.subscriptions(conn)}
    grants = {g.grant_id: g for g in subscriptions.grants(conn)}
    ZERO = Decimal("0.00")

    # 8. Fees
    fees: dict[str, list[credits.Charge]] = defaultdict(list)
    for charges in alloc.charges.values():
        for c in charges:
            if c.kind == "subscription_fee":
                fees[c.ref].append(c)
    for sub_id, sub in subs.items():
        charged = fees.pop(sub_id, [])
        if [(c.period, c.customer_id, c.amount) for c in charged] != [
                (sub.start_period, sub.customer_id, sub.fee)]:
            r.gap("subscription_fee", f"{sub_id}: expected {sub.fee} in {sub.start_period}, "
                  f"charged {[(c.period, str(c.amount)) for c in charged]}",
                  sub.start_period, sub.customer_id)
    for sub_id, charged in fees.items():
        for c in charged:
            r.gap("subscription_fee", f"fee for unknown subscription {sub_id}",
                  c.period, c.customer_id)

    # 9-11. Credits, per invoice and per grant
    drawn: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for period, customer in sorted(set(alloc.charges) | set(alloc.usage)):
        if period in alloc.errors:
            continue   # already a gap: unrated_usage
        charges = alloc.charges.get((period, customer), [])
        usage = alloc.usage.get((period, customer), ZERO)
        included = -sum((c.amount for c in charges if c.kind == "included_credit"), ZERO)
        prepaid = -sum((c.amount for c in charges if c.kind == "prepaid_credit"), ZERO)
        active = [s for s in subs.values() if s.customer_id == customer and s.active_in(period)]
        expected = min(active[0].included_credit, usage) if active else ZERO
        refs = {c.ref for c in charges if c.kind == "included_credit"}
        if included != expected or (refs and refs != {active[0].subscription_id}):
            r.gap("included_credit", f"applied {included}, expected {expected}", period, customer)
        if included + prepaid > usage or prepaid > usage - included:
            r.gap("credit_exceeds_usage", f"credits {included + prepaid} on usage {usage}",
                  period, customer)
        for c in charges:
            if c.kind != "prepaid_credit":
                continue
            g = grants.get(c.ref)
            if g is None or g.customer_id != customer or not g.usable_in(period) or c.amount > 0:
                r.gap("prepaid_credit", f"draw {c.amount} from {c.ref} not allowed here",
                      period, customer)
                continue
            drawn[c.ref] -= c.amount
    for grant_id, total in drawn.items():
        g = grants[grant_id]
        if total > g.amount:
            r.gap("grant_overdrawn", f"{grant_id}: drawn {total} of {g.amount}",
                  None, g.customer_id)
    r.checked["subscriptions"] = len(subs)
    r.checked["credit_grants"] = len(grants)


def _check_revenue(r: Report, conn: sqlite3.Connection,
                   books: dict[str, rating.PriceBook]) -> None:
    ZERO = Decimal("0.00")
    sched = revrec.schedule(conn, books)
    closed = aggregate.closes(conn)
    by_ref: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    for ln in sched.lines:
        if ln.amount < 0:
            r.gap("revenue_negative", f"{ln.kind} {ln.ref} {ln.amount}", ln.period, ln.customer_id)
        by_ref[(ln.kind, ln.ref)] += ln.amount

    # 12. Subscriptions
    for s in subscriptions.subscriptions(conn):
        pieces = [ln for ln in sched.lines if ln.kind == "subscription"
                  and ln.ref == s.subscription_id]
        if (sum((ln.amount for ln in pieces), ZERO) != s.fee
                or any(not s.active_in(ln.period) for ln in pieces)):
            r.gap("revenue_subscription", f"{s.subscription_id}: recognized "
                  f"{[(ln.period, str(ln.amount)) for ln in pieces]} of {s.fee}",
                  s.start_period, s.customer_id)

    # 13. Prepaid grants
    for g in subscriptions.grants(conn):
        drawn = by_ref[("prepaid_usage", g.grant_id)]
        broken = by_ref[("breakage", g.grant_id)]
        deferred = g.amount - drawn - broken
        if deferred < 0 or (g.last_period in closed and deferred != 0) or (
                broken and g.last_period not in closed):
            r.gap("revenue_prepaid", f"{g.grant_id}: amount {g.amount}, drawn {drawn}, "
                  f"breakage {broken}, deferred {deferred}", g.last_period, g.customer_id)

    # 14. Usage revenue per invoice
    usage_rev: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    for ln in sched.lines:
        if ln.kind in ("usage", "prepaid_usage"):
            usage_rev[(ln.period, ln.customer_id)] += ln.amount
    alloc = sched.alloc
    for key in sorted(set(alloc.usage) | set(alloc.charges) | set(usage_rev)):
        if key[0] in alloc.errors:
            continue
        included = -sum((c.amount for c in alloc.charges.get(key, [])
                         if c.kind == "included_credit"), ZERO)
        expected = alloc.usage.get(key, ZERO) - included
        if usage_rev[key] != expected:
            r.gap("revenue_usage", f"recognized {usage_rev[key]}, expected {expected}", *key)

    # 15. Rollforward
    periods = sorted({ln.period for ln in sched.lines} | {k[0] for k in alloc.usage})
    for period in periods:
        rf = revrec.rollforward(conn, period, sched)
        if not rf.balances:
            r.gap("revenue_rollforward", f"billed {rf.billed} + cash {rf.prepaid_cash} != "
                  f"recognized {rf.recognized} + deferred "
                  f"{rf.deferred_subscription + rf.deferred_prepaid}", period)
    r.checked["revenue_lines"] = len(sched.lines)
