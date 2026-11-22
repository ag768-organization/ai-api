"""Property tests for revenue recognition, on random plans, grants and
generator usage (see test_credit_invariants.setup)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from billing import emit, invoice, revrec, subscriptions
from billing.ingest import ingest
from billing.reconcile import reconcile
from tests.property.test_credit_invariants import (CUSTOMERS, NOW, SETTINGS, ZERO, accounts,
                                                   every_invoice, setup)


@SETTINGS
@given(accounts, st.integers(0, 2**32), st.integers(20, 200),
       st.sampled_from(["oct_first", "nov_first", "none"]))
def test_revenue_invariants(spec, seed, n, close_order):
    conn = setup(spec, seed, n, close_order)
    sched = revrec.schedule(conn)
    assert all(ln.amount > 0 for ln in sched.lines)

    for s in subscriptions.subscriptions(conn):
        assert sum((ln.amount for ln in sched.lines if ln.ref == s.subscription_id), ZERO) == s.fee

    for g in subscriptions.grants(conn):
        used = sum((ln.amount for ln in sched.lines if ln.ref == g.grant_id), ZERO)
        assert ZERO <= used <= g.amount     # never recognize more than was paid

    # everything billed or prepaid is either revenue or deferred, every period
    invs = every_invoice(conn)
    for period in sorted({inv.period for inv in invs} | {ln.period for ln in sched.lines}):
        rf = revrec.rollforward(conn, period, sched)
        assert rf.balances
        assert rf.billed == sum((inv.total for inv in invs if inv.period <= period), ZERO)
        assert rf.deferred_subscription >= 0 and rf.deferred_prepaid >= 0

    report = reconcile(conn)
    assert report.ok, report.gaps


@SETTINGS
@given(accounts, st.integers(0, 2**32), st.integers(0, 2**32))
def test_closed_period_revenue_is_final(spec, seed, later_seed):
    conn = setup(spec, seed, 120, "oct_first")
    before = [ln for ln in revrec.schedule(conn).lines if ln.period == "2026-10"]
    more = emit.generate(80, seed=later_seed, now=NOW, late_rate=0.5, customers=CUSTOMERS)
    ingest(conn, more, received_at=NOW)
    invoice.close_period(conn, "2026-11", closed_at=datetime(2026, 12, 1, 1, tzinfo=timezone.utc))
    after = [ln for ln in revrec.schedule(conn).lines if ln.period == "2026-10"]
    assert after == before
    assert sum((ln.amount for ln in after), Decimal(0)) >= 0
