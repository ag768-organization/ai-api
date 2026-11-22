"""Subscriptions, included credits, prepaid credits and expiry."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from billing import credits, invoice, ledger, subscriptions
from billing.ingest import ingest
from billing.reconcile import reconcile
from tests.billing_helpers import at, event

D = Decimal
OCT = "2026-10-15T00:00:00+00:00"
NOV = "2026-11-10T00:00:00+00:00"
NOV_CLOSE = at("2026-12-01T00:00:01")


# Six $12.00 slices of usage that stay inside the first volume tier when
# combined in one period: (model, input_tokens, output_tokens).
TWELVE_DOLLARS = [("large-1", 1_000_000, 0)] + [("large-1", 0, 250_000)] * 4 + [
    ("medium-1", 0, 1_000_000)]


def usage(conn, n, when, dollars_12: int = 1, customer="cust_0001", received="2026-11-20T01:00",
          start: int = 0):
    """`dollars_12` slices of $12.00 of usage (slices `start`.. of TWELVE_DOLLARS)."""
    ingest(conn, [event(n + i, occurred_at=when, customer_id=customer, model=m,
                        input_tokens=i_t, output_tokens=o_t)
                  for i, (m, i_t, o_t) in enumerate(TWELVE_DOLLARS[start:start + dollars_12])],
           received_at=at(received))


def charges(invs, customer="cust_0001"):
    [inv] = [i for i in invs if i.customer_id == customer]
    return [(c.kind, c.amount) for c in inv.charges], inv


# --- Records ----------------------------------------------------------------

def test_plans_load_and_are_whole_cents():
    plans = subscriptions.load_plans()
    assert plans["plus"].included_credit == D("40.00")
    assert plans["plus_annual"].term_months == 12


@pytest.mark.parametrize("bad", [5.0, "5.001", "-1", "NaN", "0"])
def test_grant_amount_must_be_positive_whole_cents(conn, bad):
    with pytest.raises(subscriptions.SubscriptionError):
        subscriptions.grant_credit(conn, "cust_0001", bad, granted_at=at(NOV[:10]))


def test_grant_expires_after_twelve_periods(conn):
    g = subscriptions.grant_credit(conn, "cust_0001", "10.00", granted_at=at("2026-11-30T23:00"))
    assert (g.first_period, g.last_period) == ("2026-11", "2027-10")


def test_subscription_copies_plan_terms(conn):
    sub = subscriptions.subscribe(conn, "cust_0001", "plus_annual", "2026-11")
    [stored] = subscriptions.subscriptions(conn)
    assert stored == sub
    assert (sub.fee, sub.end_period) == (D("600.00"), "2027-10")


def test_overlapping_terms_refused(conn):
    subscriptions.subscribe(conn, "cust_0001", "plus_annual", "2026-11")
    with pytest.raises(subscriptions.SubscriptionError, match="already has"):
        subscriptions.subscribe(conn, "cust_0001", "plus", "2027-03")
    subscriptions.subscribe(conn, "cust_0001", "plus", "2027-11")   # after the term: fine
    subscriptions.subscribe(conn, "cust_0002", "plus", "2027-03")   # other customer: fine


def test_unknown_plan_and_bad_mode_refused(conn):
    with pytest.raises(subscriptions.SubscriptionError):
        subscriptions.subscribe(conn, "cust_0001", "gold", "2026-11")
    with pytest.raises(subscriptions.SubscriptionError):
        subscriptions.open_account(conn, "cust_0001", "credit_card")


def test_cannot_subscribe_or_grant_into_a_closed_period(conn):
    usage(conn, 1, OCT, received="2026-11-01T01:00")
    invoice.close_period(conn, "2026-10", closed_at=at("2026-11-02"))
    with pytest.raises(subscriptions.SubscriptionError, match="closed"):
        subscriptions.subscribe(conn, "cust_0001", "plus", "2026-10")
    with pytest.raises(subscriptions.SubscriptionError, match="closed"):
        subscriptions.grant_credit(conn, "cust_0001", "5.00", granted_at=at("2026-10-20"))


@pytest.mark.parametrize("table", ["accounts", "subscriptions", "credit_grants",
                                   "invoice_charges"])
def test_new_tables_are_append_only(conn, table):
    subscriptions.open_account(conn, "cust_0001", "prepaid")
    subscriptions.subscribe(conn, "cust_0001", "plus", "2026-11")
    subscriptions.grant_credit(conn, "cust_0001", "5.00", granted_at=at(NOV[:10]))
    usage(conn, 1, NOV)
    invoice.close_period(conn, "2026-11", closed_at=NOV_CLOSE)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"DELETE FROM {table}")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"UPDATE {table} SET rowid = rowid")


# --- Billing ------------------------------------------------------------------

def test_pay_as_you_go_invoice_is_unchanged(conn):
    usage(conn, 1, NOV)
    kinds, inv = charges(invoice.draft(conn, "2026-11"))
    assert kinds == [] and inv.total == D("12.00")


def test_usage_inside_included_credit_costs_only_the_fee(conn):
    subscriptions.subscribe(conn, "cust_0001", "plus", "2026-11")
    usage(conn, 1, NOV)                                    # $12 of usage, $40 included
    kinds, inv = charges(invoice.draft(conn, "2026-11"))
    assert kinds == [("subscription_fee", D("60.00")), ("included_credit", D("-12.00"))]
    assert inv.total == D("60.00") == inv.usage_total + D("48.00")


def test_overage_past_included_credit_is_billed_at_api_rates(conn):
    subscriptions.subscribe(conn, "cust_0001", "plus", "2026-11")
    usage(conn, 1, NOV, dollars_12=5)                      # $60 of usage
    kinds, inv = charges(invoice.draft(conn, "2026-11"))
    assert kinds == [("subscription_fee", D("60.00")), ("included_credit", D("-40.00"))]
    assert inv.total == D("80.00")                         # fee + $20 overage


def test_included_credit_does_not_carry_over(conn):
    subscriptions.subscribe(conn, "cust_0001", "plus_annual", "2026-10")
    usage(conn, 1, OCT, received="2026-11-01T01:00")       # $12 of $40 used in Oct
    invoice.close_period(conn, "2026-10", closed_at=at("2026-11-02"))
    usage(conn, 10, NOV, dollars_12=5)                     # $60 in Nov
    kinds, inv = charges(invoice.draft(conn, "2026-11"))
    assert kinds == [("included_credit", D("-40.00"))]     # no fee again, no $28 carried
    assert inv.total == D("20.00")


def test_prepaid_credit_draws_down_across_periods(conn):
    subscriptions.grant_credit(conn, "cust_0001", "30.00", granted_at=at("2026-10-01"))
    usage(conn, 1, OCT, received="2026-11-01T01:00", dollars_12=2)   # $24
    oct_inv = invoice.close_period(conn, "2026-10", closed_at=at("2026-11-02"))
    assert charges(oct_inv)[0] == [("prepaid_credit", D("-24.00"))]
    usage(conn, 10, NOV)                                               # $12, $6 left
    kinds, inv = charges(invoice.draft(conn, "2026-11"))
    assert kinds == [("prepaid_credit", D("-6.00"))]
    assert inv.total == D("6.00")


def test_included_credit_is_spent_before_prepaid(conn):
    subscriptions.subscribe(conn, "cust_0001", "plus", "2026-11")
    g = subscriptions.grant_credit(conn, "cust_0001", "100.00", granted_at=at("2026-11-01"))
    usage(conn, 1, NOV, dollars_12=5)                      # $60
    kinds, inv = charges(invoice.draft(conn, "2026-11"))
    assert kinds == [("subscription_fee", D("60.00")), ("included_credit", D("-40.00")),
                     ("prepaid_credit", D("-20.00"))]
    assert inv.total == D("60.00")                         # credits never offset the fee
    assert credits.allocate(conn).balances[g.grant_id] == D("80.00")


def test_soonest_expiring_grant_is_spent_first(conn):
    old = subscriptions.grant_credit(conn, "cust_0001", "5.00", granted_at=at("2025-12-15"),
                                     grant_id="grant_old")
    new = subscriptions.grant_credit(conn, "cust_0001", "50.00", granted_at=at("2026-11-01"),
                                     grant_id="grant_new")
    usage(conn, 1, NOV)                                     # $12
    [inv] = invoice.draft(conn, "2026-11")
    assert [(c.ref, c.amount) for c in inv.charges] == [
        (old.grant_id, D("-5.00")), (new.grant_id, D("-7.00"))]


def test_expired_credit_is_not_spent(conn):
    g = subscriptions.grant_credit(conn, "cust_0001", "50.00", granted_at=at("2025-10-20"))
    assert g.last_period == "2026-09"
    usage(conn, 1, NOV)
    kinds, inv = charges(invoice.draft(conn, "2026-11"))
    assert kinds == [] and inv.total == D("12.00")
    assert credits.allocate(conn).balances[g.grant_id] == D("50.00")   # left to expire


def test_annual_fee_is_charged_once_with_included_credit_every_month(conn):
    subscriptions.subscribe(conn, "cust_0001", "plus_annual", "2026-11")
    usage(conn, 1, NOV)
    kinds, _ = charges(invoice.draft(conn, "2026-11"))
    assert kinds == [("subscription_fee", D("600.00")), ("included_credit", D("-12.00"))]


def test_subscription_with_no_usage_still_gets_an_invoice(conn):
    subscriptions.subscribe(conn, "cust_0002", "basic", "2026-11")
    [inv] = invoice.draft(conn, "2026-11")
    assert (inv.customer_id, inv.lines, inv.total) == ("cust_0002", (), D("12.00"))


# --- Close and reconcile --------------------------------------------------------

def loaded(conn):
    subscriptions.subscribe(conn, "cust_0001", "plus", "2026-11")
    subscriptions.grant_credit(conn, "cust_0001", "30.00", granted_at=at("2026-10-01"))
    subscriptions.grant_credit(conn, "cust_0002", "10.00", granted_at=at("2026-11-01"))
    subscriptions.subscribe(conn, "cust_0003", "basic", "2026-11")
    usage(conn, 1, OCT, received="2026-11-01T01:00")
    invoice.close_period(conn, "2026-10", closed_at=at("2026-11-02"))
    usage(conn, 10, NOV, dollars_12=6)
    usage(conn, 20, NOV, customer="cust_0002")
    return conn


def test_close_freezes_charges_and_reconciles(conn):
    loaded(conn)
    closed = invoice.close_period(conn, "2026-11", closed_at=NOV_CLOSE)
    assert invoice.stored(conn, "2026-11") == closed
    totals = {r["customer_id"]: r["total"] for r in conn.execute(
        "SELECT * FROM invoices WHERE period = '2026-11'")}
    # cust_0001: fee 60 + usage 72 - included 40 - prepaid 18 (30 less 12 used in Oct)
    assert totals == {"cust_0001": "74.00", "cust_0002": "2.00", "cust_0003": "12.00"}
    report = reconcile(conn)
    assert report.ok, report.gaps


def test_open_periods_reconcile(conn):
    report = reconcile(loaded(conn))
    assert report.ok, report.gaps


def test_closing_out_of_order_never_overdraws_a_grant(conn):
    g = subscriptions.grant_credit(conn, "cust_0001", "20.00", granted_at=at("2026-10-01"))
    usage(conn, 1, OCT, received="2026-11-20T01:00")        # Oct still open
    usage(conn, 10, NOV, dollars_12=2, start=1)
    invoice.close_period(conn, "2026-11", closed_at=NOV_CLOSE)  # Nov first: Oct took $12 first
    invoice.close_period(conn, "2026-10", closed_at=NOV_CLOSE)
    drawn = -sum((c.amount for c in credits.frozen_charges(conn) if c.ref == g.grant_id), D(0))
    assert drawn == D("20.00")
    report = reconcile(conn)
    assert report.ok, report.gaps


def test_extra_credit_row_is_a_gap(conn):
    loaded(conn)
    invoice.close_period(conn, "2026-11", closed_at=NOV_CLOSE)
    grant = "(SELECT grant_id FROM credit_grants WHERE customer_id = 'cust_0001')"
    # cust_0001's $30 grant is fully spent; a further draw overdraws it
    conn.execute("INSERT INTO invoice_charges VALUES ('2026-12', 'cust_0001', 'prepaid_credit',"
                 f" {grant}, '-9.00')")
    # and a draw on someone else's grant is never allowed
    conn.execute("INSERT INTO invoice_charges VALUES ('2026-11', 'cust_0003', 'prepaid_credit',"
                 f" {grant}, '-1.00')")
    gaps = {g.check for g in reconcile(conn).gaps}
    assert {"grant_overdrawn", "credit_exceeds_usage", "prepaid_credit", "invoice_total"} <= gaps


def test_wrong_fee_is_a_gap(conn):
    subscriptions.subscribe(conn, "cust_0003", "basic", "2026-10")
    usage(conn, 1, OCT, received="2026-11-01T01:00")
    invoice.close_period(conn, "2026-10", closed_at=at("2026-11-02"))
    conn.execute("INSERT INTO invoice_charges VALUES ('2026-10', 'cust_0003', 'subscription_fee',"
                 " 'sub_fake', '1.00')")
    assert "subscription_fee" in {g.check for g in reconcile(conn).gaps}


def test_wrong_included_credit_is_a_gap(conn):
    subscriptions.subscribe(conn, "cust_0001", "plus", "2026-10")
    usage(conn, 1, OCT, received="2026-11-01T01:00")
    invoice.close_period(conn, "2026-10", closed_at=at("2026-11-02"))
    conn.execute("INSERT INTO invoice_charges VALUES ('2026-10', 'cust_0001', 'included_credit',"
                 " 'other', '-1.00')")
    assert "included_credit" in {g.check for g in reconcile(conn).gaps}


# --- Stopping prepaid usage -----------------------------------------------------

def test_remaining_credit(conn):
    subscriptions.subscribe(conn, "cust_0001", "plus", "2026-11")
    subscriptions.grant_credit(conn, "cust_0001", "10.00", granted_at=at("2026-11-01"))
    assert credits.remaining_credit(conn, "cust_0001", "2026-11") == D("50.00")
    usage(conn, 1, NOV, dollars_12=4)                      # $48
    assert credits.remaining_credit(conn, "cust_0001", "2026-11") == D("2.00")
    usage(conn, 10, NOV, start=4)
    assert credits.remaining_credit(conn, "cust_0001", "2026-11") == D("0.00")


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "api.db"
    monkeypatch.setenv("AI_API_DB", str(db))
    return TestClient(app), db


def test_prepaid_customer_is_stopped_when_credit_runs_out(client):
    api, db = client
    conn = ledger.connect(db)
    subscriptions.open_account(conn, "cust_0007", "prepaid")
    body = {"customer_id": "cust_0007", "model": "large-1", "prompt": "hello there"}
    r = api.post("/v1/complete", json=body)
    assert r.status_code == 402                            # no credit bought yet
    assert ledger.count(conn) == 0                         # refused before any usage

    subscriptions.grant_credit(conn, "cust_0007", "12.00")
    assert api.post("/v1/complete", json=body).status_code == 200
    ingest(conn, [{"event_id": "00000000-0000-4000-8000-000000000999",
                   "customer_id": "cust_0007", "model": "large-1",
                   "input_tokens": 1_000_000, "output_tokens": 0,
                   "occurred_at": datetime.now(timezone.utc).isoformat()}])  # $12 used
    assert api.post("/v1/complete", json=body).status_code == 402


def test_postpaid_customer_is_never_stopped(client):
    api, _ = client
    body = {"customer_id": "cust_0008", "model": "large-1", "prompt": "hello"}
    assert api.post("/v1/complete", json=body).status_code == 200
