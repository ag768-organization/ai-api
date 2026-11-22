"""Property tests for billing invariants 6 to 9 (CLAUDE.md).

Each test builds its own in-memory ledger inside the test body, because
Hypothesis runs the body many times and a pytest fixture would be shared.
Failures print the seed and parameters, so `emit.generate` can replay them.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from billing import aggregate, emit, invoice, ledger, rating
from billing.aggregate import Usage
from billing.events import KNOWN_MODELS
from billing.ingest import ingest
from billing.reconcile import reconcile

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
V1 = rating.price_books()["v1"]
SETTINGS = settings(max_examples=60, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow])

usage_rows = st.lists(
    st.builds(Usage,
              period=st.sampled_from(["2026-09", "2026-10", "2026-11"]),
              customer_id=st.sampled_from(["cust_0001", "cust_0002", "cust_0003"]),
              model=st.sampled_from(sorted(KNOWN_MODELS)),
              input_tokens=st.integers(0, 50_000_000),
              output_tokens=st.integers(0, 50_000_000),
              event_count=st.just(1), late_count=st.just(0)),
    max_size=30,
    unique_by=lambda u: (u.period, u.customer_id, u.model),
)

fault_rates = st.fixed_dictionaries({
    "dup_rate": st.sampled_from([0.0, 0.05, 0.3]),
    "conflict_rate": st.sampled_from([0.0, 0.02]),
    "invalid_rate": st.sampled_from([0.0, 0.03]),
    "late_rate": st.sampled_from([0.0, 0.1, 0.5]),
})


def fresh():
    return ledger.connect(":memory:")


def snapshot(conn):
    """Everything a customer is billed for: aggregates and draft invoices of
    every period, plus frozen invoices of closed periods."""
    periods = sorted({u.period for u in aggregate.aggregate(conn)} | set(aggregate.closes(conn)))
    return (
        aggregate.aggregate(conn),
        {p: invoice.stored(conn, p) if p in aggregate.closes(conn) else invoice.draft(conn, p)
         for p in periods},
    )


# --- Invariant 6: invoice total == sum of rated usage ---------------------

@SETTINGS
@given(usage_rows)
def test_invoice_total_equals_sum_of_rated_usage(rows):
    lines = rating.rate(rows, V1)
    invoices = invoice.build(lines)
    assert sum((i.total for i in invoices), Decimal(0)) == sum((ln.amount for ln in lines), Decimal(0))
    for inv in invoices:
        assert inv.total == sum((ln.amount for ln in inv.lines), Decimal(0))
        assert inv.total.as_tuple().exponent == -2  # cents, never re-rounded
    for ln in lines:
        exact, _ = rating.exact_amount(ln.tokens, V1.rate(ln.model, ln.category))
        assert ln.amount == rating.round_line(exact)
        assert abs(ln.amount - exact) <= Decimal("0.005")
    # units are carried through to the invoice exactly
    assert sum(i.tokens for i in invoices) == sum(u.total_tokens for u in rows)


@SETTINGS
@given(st.sampled_from(sorted(KNOWN_MODELS)), st.sampled_from(rating.CATEGORIES),
       st.integers(0, 100_000_000), st.integers(0, 100_000_000))
def test_more_tokens_never_cost_less(model, category, a, b):
    rate = V1.rate(model, category)
    lo, hi = sorted((a, b))
    assert rating.exact_amount(lo, rate)[0] <= rating.exact_amount(hi, rate)[0]


# --- Invariant 7: replay never changes totals -----------------------------

@SETTINGS
@given(st.integers(0, 2**32), fault_rates, st.integers(0, 2**32), st.integers(1, 4))
def test_replay_in_any_order_with_duplicates_never_changes_totals(seed, rates, shuffle_seed, batches):
    events = emit.generate(120, seed=seed, now=NOW, **rates)

    once = fresh()
    ingest(once, events, received_at=NOW)

    replayed = fresh()
    rng = random.Random(shuffle_seed)
    stream = events + rng.choices(events, k=len(events) // 2)  # extra duplicates
    rng.shuffle(stream)
    size = -(-len(stream) // batches)
    for i in range(0, len(stream), size):
        ingest(replayed, stream[i:i + size], received_at=NOW)

    # Conflicts depend on which copy arrives first, so compare only when the
    # generator injected none (exactly-once on identical payloads).
    if rates["conflict_rate"] == 0:
        assert snapshot(replayed) == snapshot(once)
    assert reconcile(replayed).ok


@SETTINGS
@given(st.integers(0, 2**32), fault_rates)
def test_replay_after_close_changes_nothing(seed, rates):
    events = emit.generate(150, seed=seed, now=NOW, **rates)
    conn = fresh()
    ingest(conn, events, received_at=datetime(2026, 11, 1, 1, tzinfo=timezone.utc))
    invoice.close_period(conn, "2026-10", closed_at=datetime(2026, 11, 2, tzinfo=timezone.utc))
    before = snapshot(conn)
    r = ingest(conn, events, received_at=NOW)
    assert r.accepted == 0
    assert snapshot(conn) == before


# --- Invariant 8: every ledger event lands in exactly one billing period --

months = st.sampled_from([f"2026-{m:02d}" for m in range(1, 12)])


@SETTINGS
@given(st.sets(months), st.datetimes(min_value=datetime(2026, 1, 1), max_value=datetime(2026, 11, 1)),
       st.integers(0, 120 * 24))
def test_billing_period_is_first_period_open_at_receipt(closed_months, occurred, delay_hours):
    occurred = occurred.replace(tzinfo=timezone.utc)
    received = occurred + timedelta(hours=delay_hours)
    # each closed month closes one day after it ends
    closed = {m: aggregate.period_end(m) + timedelta(days=1) for m in closed_months}
    period = aggregate.billing_period(occurred, received, closed)

    assert period >= aggregate.period_of(occurred)
    assert not (period in closed and closed[period] <= received)  # open at receipt
    p = aggregate.period_of(occurred)
    while p != period:  # everything skipped was closed at receipt
        assert closed[p] <= received
        p = aggregate.next_period(p)


@SETTINGS
@given(st.integers(0, 2**32), fault_rates)
def test_each_event_counted_in_exactly_one_period(seed, rates):
    events = emit.generate(150, seed=seed, now=NOW, **rates)
    conn = fresh()
    ingest(conn, events[:80], received_at=datetime(2026, 11, 1, 1, tzinfo=timezone.utc))
    invoice.close_period(conn, "2026-10", closed_at=datetime(2026, 11, 2, tzinfo=timezone.utc))
    ingest(conn, events[80:], received_at=NOW)

    assigned = aggregate.assign(conn)
    assert sorted(e.event_id for e in assigned) == sorted(
        r["event_id"] for r in conn.execute("SELECT event_id FROM usage_events"))
    usage = aggregate.aggregate(conn)
    assert sum(u.event_count for u in usage) == ledger.count(conn)
    assert sum(u.total_tokens for u in usage) == ledger.total_tokens(conn)


# --- Invariant 9: reconciliation reports zero gaps on generator output ----

@SETTINGS
@given(st.integers(0, 2**32), fault_rates, st.integers(20, 300))
def test_reconciliation_has_zero_gaps_on_generator_output(seed, rates, n):
    events = emit.generate(n, seed=seed, now=NOW, **rates)
    conn = fresh()
    cut = n // 2
    ingest(conn, events[:cut], received_at=datetime(2026, 11, 1, 1, tzinfo=timezone.utc))
    invoice.close_period(conn, "2026-10", closed_at=datetime(2026, 11, 2, tzinfo=timezone.utc))
    ingest(conn, events[cut:], received_at=NOW)
    report = reconcile(conn)
    assert report.ok, report.gaps
