from billing import aggregate
from billing.ingest import ingest
from billing.invoice import close_period
from tests.billing_helpers import at, event


def test_period_helpers():
    assert aggregate.period_of(at("2026-10-31T23:59:59")) == "2026-10"
    assert aggregate.next_period("2026-12") == "2027-01"
    assert aggregate.period_end("2026-12") == at("2027-01-01")


def test_period_uses_utc_not_local_offset():
    from datetime import datetime
    ts = datetime.fromisoformat("2026-10-31T20:00:00-05:00")  # 01:00 UTC on 1 Nov
    assert aggregate.period_of(ts) == "2026-11"


def test_event_billed_in_its_own_period_when_open():
    assert aggregate.billing_period(at("2026-10-05"), at("2026-11-02"), {}) == "2026-10"


def test_event_received_before_close_stays_in_its_period():
    closed = {"2026-10": at("2026-11-03")}
    assert aggregate.billing_period(at("2026-10-05"), at("2026-11-02"), closed) == "2026-10"


def test_late_event_moves_to_next_open_period():
    closed = {"2026-10": at("2026-11-03")}
    assert aggregate.billing_period(at("2026-10-05"), at("2026-11-04"), closed) == "2026-11"


def test_late_event_skips_every_closed_period():
    closed = {"2026-09": at("2026-10-02"), "2026-10": at("2026-11-02")}
    assert aggregate.billing_period(at("2026-09-15"), at("2026-11-10"), closed) == "2026-11"


def test_received_at_exact_close_instant_counts_as_late():
    closed = {"2026-10": at("2026-11-03")}
    assert aggregate.billing_period(at("2026-10-05"), at("2026-11-03"), closed) == "2026-11"


def test_aggregate_totals_per_customer_model_period(conn):
    ingest(conn, [
        event(1, occurred_at="2026-10-01T00:00:00+00:00", input_tokens=10, output_tokens=1),
        event(2, occurred_at="2026-10-20T00:00:00+00:00", input_tokens=20, output_tokens=2),
        event(3, occurred_at="2026-10-20T00:00:00+00:00", model="large-1"),
        event(4, occurred_at="2026-11-01T00:00:00+00:00", customer_id="cust_0002"),
    ], received_at=at("2026-11-02"))
    rows = {(u.period, u.customer_id, u.model): u for u in aggregate.aggregate(conn)}
    small = rows[("2026-10", "cust_0001", "small-1")]
    assert (small.input_tokens, small.output_tokens, small.event_count) == (30, 3, 2)
    assert set(rows) == {("2026-10", "cust_0001", "small-1"), ("2026-10", "cust_0001", "large-1"),
                         ("2026-11", "cust_0002", "small-1")}
    assert [u.period for u in aggregate.aggregate(conn, "2026-11")] == ["2026-11"]


def test_late_event_flagged_and_ledger_keeps_true_occurred_at(conn):
    ingest(conn, [event(1, occurred_at="2026-10-05T00:00:00+00:00")], received_at=at("2026-11-01T01:00"))
    close_period(conn, "2026-10", closed_at=at("2026-11-02"))
    ingest(conn, [event(2, occurred_at="2026-10-06T00:00:00+00:00")], received_at=at("2026-11-05"))

    nov = aggregate.aggregate(conn, "2026-11")
    assert [(u.event_count, u.late_count) for u in nov] == [(1, 1)]
    row = conn.execute("SELECT occurred_at FROM usage_events WHERE event_id = ?",
                       (event(2)["event_id"],)).fetchone()
    assert row["occurred_at"].startswith("2026-10-06")  # the shift is visible, not rewritten
