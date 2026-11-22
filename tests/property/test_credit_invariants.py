"""Property tests for subscriptions and credits (invariants 6, 7 and 9).

Random plans, terms and prepaid grants on top of generator usage. Each test
builds its own in-memory ledger.
"""
from __future__ import annotations

import random
from datetime import datetime, timezone
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from billing import aggregate, credits, emit, invoice, ledger, subscriptions
from billing.ingest import ingest
from billing.reconcile import reconcile

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
BEFORE_OCT_CLOSE = datetime(2026, 11, 1, 1, tzinfo=timezone.utc)
OCT_CLOSE = datetime(2026, 11, 2, tzinfo=timezone.utc)
NOV_CLOSE = datetime(2026, 12, 1, 1, tzinfo=timezone.utc)
CUSTOMERS = ["cust_0001", "cust_0002", "cust_0003", "cust_0004"]
SETTINGS = settings(max_examples=50, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow])
ZERO = Decimal("0.00")

accounts = st.lists(st.tuples(
    st.sampled_from(CUSTOMERS),
    st.sampled_from([None, "basic", "plus", "plus_annual", "team"]),
    st.sampled_from(["2026-10", "2026-11"]),
    st.lists(st.tuples(st.integers(1, 30_000),                      # grant cents
                       st.integers(0, 13)), max_size=3),            # months before Nov
), max_size=4, unique_by=lambda a: a[0])


def setup(spec, seed, n, close_order):
    conn = ledger.connect(":memory:")
    for customer, plan, start, grant_list in spec:
        if plan:
            subscriptions.subscribe(conn, customer, plan, start, subscription_id=f"sub_{customer}")
        for i, (cents, back) in enumerate(grant_list):
            granted = datetime(2026 - (1 if back > 10 else 0), (10 - back) % 12 + 1, 3,
                               tzinfo=timezone.utc)
            subscriptions.grant_credit(conn, customer, Decimal(cents).scaleb(-2),
                                       granted_at=granted, grant_id=f"g_{customer}_{i}")
    events = emit.generate(n, seed=seed, now=NOW, late_rate=0.3, dup_rate=0.05,
                           customers=CUSTOMERS)
    if close_order == "oct_first":
        ingest(conn, events[: n // 2], received_at=BEFORE_OCT_CLOSE)
        invoice.close_period(conn, "2026-10", closed_at=OCT_CLOSE)
        ingest(conn, events[n // 2:], received_at=NOW)
    else:
        ingest(conn, events, received_at=NOW)
        if close_order == "nov_first":
            invoice.close_period(conn, "2026-11", closed_at=NOV_CLOSE)
            invoice.close_period(conn, "2026-10", closed_at=NOV_CLOSE)
    return conn


def every_invoice(conn):
    periods = sorted({u.period for u in aggregate.aggregate(conn)}
                     | {s.start_period for s in subscriptions.subscriptions(conn)}
                     | set(aggregate.closes(conn)))
    closed = aggregate.closes(conn)
    return [inv for p in periods
            for inv in (invoice.stored(conn, p) if p in closed else invoice.draft(conn, p))]


@SETTINGS
@given(accounts, st.integers(0, 2**32), st.integers(20, 200),
       st.sampled_from(["oct_first", "nov_first", "none"]))
def test_credit_invariants(spec, seed, n, close_order):
    conn = setup(spec, seed, n, close_order)
    invs = every_invoice(conn)
    for inv in invs:
        fees = sum((c.amount for c in inv.charges if c.kind == "subscription_fee"), ZERO)
        credit = -sum((c.amount for c in inv.charges if c.kind != "subscription_fee"), ZERO)
        # Invariant 6, extended: total = rated usage + fees - credits, exactly
        assert inv.total == inv.usage_total + fees - credit
        assert ZERO <= credit <= inv.usage_total       # credits only offset usage
        assert inv.total >= fees                       # never below the fee
        assert inv.total.as_tuple().exponent == -2

    # Conservation: every grant's amount = drawn + unspent, never overdrawn
    alloc = credits.allocate(conn)
    for g in subscriptions.grants(conn):
        drawn = -sum((c.amount for inv in invs for c in inv.charges if c.ref == g.grant_id), ZERO)
        assert drawn + alloc.balances[g.grant_id] == g.amount
        assert ZERO <= alloc.balances[g.grant_id] <= g.amount

    # Every subscription's fee appears exactly once
    for s in subscriptions.subscriptions(conn):
        fees = [c for inv in invs for c in inv.charges
                if c.kind == "subscription_fee" and c.ref == s.subscription_id]
        assert [(c.period, c.amount) for c in fees] == [(s.start_period, s.fee)]

    # Invariant 9
    report = reconcile(conn)
    assert report.ok, report.gaps


@SETTINGS
@given(accounts, st.integers(0, 2**32), st.integers(0, 2**32))
def test_replay_never_changes_charges(spec, seed, shuffle_seed):
    once = setup(spec, seed, 100, "none")
    events = emit.generate(100, seed=seed, now=NOW, late_rate=0.3, dup_rate=0.05,
                           customers=CUSTOMERS)
    rng = random.Random(shuffle_seed)
    stream = events + rng.choices(events, k=40)
    rng.shuffle(stream)
    ingest(once, stream, received_at=NOW)   # everything again, in another order
    assert every_invoice(once) == every_invoice(setup(spec, seed, 100, "none"))
