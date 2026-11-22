import sqlite3
from decimal import Decimal

import pytest

from billing import aggregate, invoice
from billing.ingest import ingest
from tests.billing_helpers import at, event

OCT = "2026-10-15T00:00:00+00:00"


def seed(conn):
    ingest(conn, [
        event(1, occurred_at=OCT, model="large-1", input_tokens=1_000_000, output_tokens=500_000),
        event(2, occurred_at=OCT, model="medium-1", input_tokens=5_000, output_tokens=0),
        event(3, occurred_at=OCT, customer_id="cust_0002", model="tiny-1"),
    ], received_at=at("2026-11-01T01:00"))


def test_invoice_total_is_sum_of_rounded_lines(conn):
    seed(conn)
    invs = {i.customer_id: i for i in invoice.draft(conn, "2026-10")}
    a = invs["cust_0001"]
    # large-1: 1M in at $12 + 0.5M out at $48 = 12 + 24; medium-1: 5k in at $3 = 0.015 -> 0.02
    assert [str(ln.amount) for ln in a.lines] == ["12.00", "24.00", "0.02", "0.00"]
    assert a.total == Decimal("36.02")
    assert invs["cust_0002"].total == Decimal("0.00")  # inside tiny-1 free allowance


def test_close_freezes_invoice(conn):
    seed(conn)
    closed = invoice.close_period(conn, "2026-10", closed_at=at("2026-11-02"))
    assert invoice.stored(conn, "2026-10") == closed
    row = conn.execute("SELECT total FROM invoices WHERE customer_id = 'cust_0001'").fetchone()
    assert row["total"] == "36.02"


def test_late_event_does_not_change_closed_invoice(conn):
    seed(conn)
    before = invoice.close_period(conn, "2026-10", closed_at=at("2026-11-02"))
    ingest(conn, [event(9, occurred_at=OCT, model="large-1")], received_at=at("2026-11-03"))
    assert invoice.stored(conn, "2026-10") == before
    nov = invoice.draft(conn, "2026-11")
    assert [(i.customer_id, i.tokens) for i in nov] == [("cust_0001", 150)]


def test_cannot_close_twice(conn):
    seed(conn)
    invoice.close_period(conn, "2026-10", closed_at=at("2026-11-02"))
    with pytest.raises(invoice.PeriodCloseError, match="already closed"):
        invoice.close_period(conn, "2026-10", closed_at=at("2026-11-03"))


def test_cannot_close_before_period_ends(conn):
    with pytest.raises(invoice.PeriodCloseError, match="not ended"):
        invoice.close_period(conn, "2026-10", closed_at=at("2026-10-31T23:59"))


def test_cannot_backdate_close_past_received_events(conn):
    seed(conn)  # received 2026-11-01T01:00
    with pytest.raises(invoice.PeriodCloseError, match="after every event"):
        invoice.close_period(conn, "2026-10", closed_at=at("2026-11-01"))
    assert aggregate.closes(conn) == {}


def test_failed_close_writes_nothing(conn):
    seed(conn)
    with pytest.raises(invoice.PeriodCloseError):
        invoice.close_period(conn, "2026-10", closed_at=at("2026-11-01"))
    for table in ("period_closes", "period_aggregates", "invoice_lines", "invoices"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("table", ["period_closes", "period_aggregates", "invoice_lines",
                                   "invoices", "ingest_batches"])
def test_billing_tables_are_append_only(conn, table):
    seed(conn)
    invoice.close_period(conn, "2026-10", closed_at=at("2026-11-02"))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"DELETE FROM {table}")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"UPDATE {table} SET rowid = rowid")
