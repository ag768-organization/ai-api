"""Revenue recognition: usage as consumed, subscriptions ratably, prepaid
credit deferred until drawn or expired."""
from __future__ import annotations

from decimal import Decimal

import pytest

from billing import credits, invoice, revrec, subscriptions
from billing.reconcile import reconcile
from tests.billing_helpers import at
from tests.test_subscriptions import NOV, NOV_CLOSE, OCT, usage

D = Decimal
SEP = "2026-09-15T00:00:00+00:00"


def lines(conn, kind=None, period=None):
    return [(ln.period, ln.kind, ln.amount) for ln in revrec.schedule(conn).lines
            if (kind is None or ln.kind == kind) and (period is None or ln.period == period)]


@pytest.mark.parametrize("fee,months,expected", [
    ("600.00", 12, ["50.00"] * 12),
    ("100.00", 3, ["33.33", "33.33", "33.34"]),
    ("0.06", 12, ["0.00"] * 11 + ["0.06"]),
    ("60.00", 1, ["60.00"]),
])
def test_ratable_split_is_exact(fee, months, expected):
    pieces = revrec.ratable(D(fee), "2026-11", months)
    assert [str(a) for _, a in pieces] == expected
    assert sum(a for _, a in pieces) == D(fee)
    assert [p for p, _ in pieces][-1] == subscriptions.add_periods("2026-11", months - 1)


def test_postpaid_usage_is_recognized_when_billed(conn):
    usage(conn, 1, NOV)
    usage(conn, 5, OCT, start=1)              # late: Oct still open, so billed in Oct
    assert lines(conn) == [("2026-10", "usage", D("12.00")), ("2026-11", "usage", D("12.00"))]


def test_annual_fee_is_recognized_evenly_and_the_rest_is_deferred(conn):
    subscriptions.subscribe(conn, "cust_0001", "plus_annual", "2026-11")
    usage(conn, 1, NOV)                       # $12, all inside included credit
    assert lines(conn, period="2026-11") == [("2026-11", "subscription", D("50.00"))]
    assert len(lines(conn, "subscription")) == 12
    rf = revrec.rollforward(conn, "2026-11")
    assert (rf.billed, rf.recognized, rf.deferred_subscription) == (
        D("600.00"), D("50.00"), D("550.00"))
    assert rf.balances
    assert revrec.rollforward(conn, "2027-10").deferred_subscription == 0


def test_included_usage_earns_no_extra_revenue_but_overage_does(conn):
    subscriptions.subscribe(conn, "cust_0001", "plus", "2026-11")
    usage(conn, 1, NOV, dollars_12=5)         # $60 usage, $40 included
    assert lines(conn) == [("2026-11", "usage", D("20.00")),
                           ("2026-11", "subscription", D("60.00"))]


def test_prepaid_credit_is_deferred_until_drawn(conn):
    subscriptions.grant_credit(conn, "cust_0001", "30.00", granted_at=at("2026-10-01"))
    rf = revrec.rollforward(conn, "2026-10")
    assert (rf.prepaid_cash, rf.recognized, rf.deferred_prepaid) == (D("30.00"), 0, D("30.00"))
    usage(conn, 1, OCT, dollars_12=2, received="2026-11-01T01:00")
    assert lines(conn) == [("2026-10", "prepaid_usage", D("24.00"))]
    assert revrec.rollforward(conn, "2026-10").deferred_prepaid == D("6.00")


def test_breakage_is_recognized_once_the_last_period_closes(conn):
    g = subscriptions.grant_credit(conn, "cust_0001", "50.00", granted_at=at("2025-11-03"))
    assert g.last_period == "2026-10"
    usage(conn, 1, OCT, received="2026-11-01T01:00")
    assert lines(conn, "breakage") == []      # Oct open: still deferred
    assert revrec.rollforward(conn, "2026-10").deferred_prepaid == D("38.00")
    invoice.close_period(conn, "2026-10", closed_at=at("2026-11-02"))
    assert lines(conn) == [("2026-10", "prepaid_usage", D("12.00")),
                           ("2026-10", "breakage", D("38.00"))]
    rf = revrec.rollforward(conn, "2026-10")
    assert (rf.recognized, rf.deferred_prepaid) == (D("50.00"), 0)
    assert reconcile(conn).ok


def test_expired_grant_is_not_drawn_by_an_earlier_open_period(conn):
    g = subscriptions.grant_credit(conn, "cust_0001", "50.00", granted_at=at("2025-11-03"))
    usage(conn, 1, OCT, received="2026-11-01T01:00")
    invoice.close_period(conn, "2026-10", closed_at=at("2026-11-02"))
    usage(conn, 5, SEP, start=1, received="2026-11-20T01:00")   # Sep never closed: billed in Sep
    [sep] = invoice.draft(conn, "2026-09")
    assert sep.charges == () and sep.total == D("12.00")
    assert credits.allocate(conn).balances[g.grant_id] == D("38.00")  # all broken already
    report = reconcile(conn)
    assert report.ok, report.gaps


def test_recognized_revenue_of_a_closed_period_never_changes(conn):
    subscriptions.subscribe(conn, "cust_0001", "plus_annual", "2026-10")
    subscriptions.grant_credit(conn, "cust_0002", "5.00", granted_at=at("2026-10-01"))
    usage(conn, 1, OCT, dollars_12=6, received="2026-11-01T01:00")
    usage(conn, 10, OCT, customer="cust_0002", received="2026-11-01T01:00")
    invoice.close_period(conn, "2026-10", closed_at=at("2026-11-02"))
    before = lines(conn, period="2026-10")
    usage(conn, 20, OCT, dollars_12=3, received="2026-11-20T01:00")      # late: billed in Nov
    usage(conn, 30, NOV, customer="cust_0002", start=1)
    invoice.close_period(conn, "2026-11", closed_at=NOV_CLOSE)
    assert lines(conn, period="2026-10") == before
    report = reconcile(conn)
    assert report.ok, report.gaps


def test_tampered_fee_breaks_the_rollforward(conn):
    subscriptions.subscribe(conn, "cust_0001", "plus", "2026-10")
    usage(conn, 1, OCT, received="2026-11-01T01:00")
    invoice.close_period(conn, "2026-10", closed_at=at("2026-11-02"))
    conn.execute("INSERT INTO invoice_charges VALUES ('2026-10', 'cust_0001', 'subscription_fee',"
                 " 'sub_fake', '5.00')")
    assert "revenue_rollforward" in {g.check for g in reconcile(conn).gaps}


def test_overdrawn_grant_is_a_revenue_gap(conn):
    g = subscriptions.grant_credit(conn, "cust_0001", "5.00", granted_at=at("2026-10-01"))
    usage(conn, 1, OCT, received="2026-11-01T01:00")
    invoice.close_period(conn, "2026-10", closed_at=at("2026-11-02"))
    conn.execute("INSERT INTO invoice_charges VALUES ('2026-11', 'cust_0001', 'prepaid_credit',"
                 f" '{g.grant_id}', '-3.00')")
    assert "revenue_prepaid" in {gap.check for gap in reconcile(conn).gaps}
