from datetime import timezone, datetime
from decimal import Decimal

from billing import emit, invoice, ledger, rating
from billing.ingest import ingest
from billing.reconcile import reconcile
from tests.billing_helpers import at, parsed

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


def loaded(conn):
    events = emit.generate(500, seed=11, dup_rate=0.05, conflict_rate=0.02,
                           invalid_rate=0.02, late_rate=0.1, now=NOW)
    ingest(conn, events[:300], received_at=at("2026-11-01T01:00"))
    invoice.close_period(conn, "2026-10", closed_at=at("2026-11-02"))
    ingest(conn, events[300:], received_at=at("2026-11-20T01:00"))
    return conn


def checks(report):
    return {g.check for g in report.gaps}


def test_generator_output_reconciles_with_zero_gaps(conn):
    r = reconcile(loaded(conn))
    assert r.ok, r.gaps
    assert r.checked["closed_periods"] == 1
    assert r.checked["events"] == ledger.count(conn)


def test_empty_ledger_reconciles(conn):
    assert reconcile(conn).ok


def test_event_bypassing_ingest_is_a_gap(conn):
    loaded(conn)
    ledger.append(conn, parsed(1, occurred_at="2026-11-10T00:00:00+00:00"), at("2026-11-20T02:00"))
    assert "ingest_vs_ledger" in checks(reconcile(conn))


def test_unbalanced_ingest_batch_is_a_gap(conn):
    loaded(conn)
    conn.execute("INSERT INTO ingest_batches (received_at, received, accepted, duplicate,"
                 " conflict, invalid) VALUES ('2026-11-20', 5, 0, 0, 0, 0)")
    assert "ingest_balance" in checks(reconcile(conn))


def test_extra_invoice_line_is_a_gap(conn):
    loaded(conn)
    conn.execute("INSERT INTO invoice_lines VALUES ('2026-10', 'cust_9999', 'tiny-1', 'input',"
                 " 100, 0, '0.00', 'v1')")
    assert {"aggregated_vs_invoiced", "invoice_missing"} <= checks(reconcile(conn))


def test_missing_frozen_aggregate_is_a_gap(conn):
    # An invoice line with no matching frozen aggregate (and vice versa) fails.
    loaded(conn)
    conn.execute("INSERT INTO period_aggregates VALUES ('2026-10', 'cust_9999', 'tiny-1',"
                 " 7, 0, 1, 0)")
    assert {"raw_vs_aggregated", "aggregated_vs_invoiced"} <= checks(reconcile(conn))


def test_line_that_does_not_re_rate_is_a_gap(conn):
    loaded(conn)
    v1 = rating.price_books()["v1"]
    tampered = dict(v1.rates)
    tampered["small-1"] = {
        cat: rating.Rate(0, (rating.Tier(None, Decimal("999")),)) for cat in rating.CATEGORIES}
    books = {"v1": rating.PriceBook("v1", v1.effective_from, tampered)}
    assert "line_amount" in checks(reconcile(conn, books))
