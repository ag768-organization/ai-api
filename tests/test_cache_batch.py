"""Cache write / cache read token types and the batch discount.

Covers the schema change end to end: event validation, the payload hash
staying stable for events that predate the new fields, migrating a Phase 2
ledger file, aggregation into categories, v2 rating, close and reconcile.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal

import pytest
from pydantic import ValidationError

from billing import aggregate, emit, invoice, ledger, rating
from billing.events import UsageEvent
from billing.ingest import ingest
from billing.reconcile import reconcile
from tests.billing_helpers import at, event, parsed
from tests.test_events import valid

V1 = rating.price_books()["v1"]
V2 = rating.price_books()["v2"]
NOV = "2026-11-10T00:00:00+00:00"
OCT = "2026-10-15T00:00:00+00:00"

# Hash of tests.test_events.valid() as computed by the Phase 2 code (main at
# 5482094), before the new fields existed. If this changes, every event
# already in a ledger would turn into a conflict when re-sent.
PHASE2_HASH = "6879d8464e5eac9e8e1b7544435ab9c10f58d7d193433c356ca8a932fd47aa78"


# --- Event schema ---------------------------------------------------------

def test_new_fields_default_to_an_uncached_standard_call():
    e = UsageEvent.model_validate(valid())
    assert (e.cache_write_tokens, e.cache_read_tokens, e.batch) == (0, 0, False)


def test_total_tokens_includes_cache_tokens():
    e = UsageEvent.model_validate(valid(cache_write_tokens=7, cache_read_tokens=30))
    assert e.total_tokens == 100 + 50 + 7 + 30


@pytest.mark.parametrize("field,value", [
    ("cache_write_tokens", -1), ("cache_read_tokens", -1),
    ("cache_read_tokens", 1.0), ("cache_write_tokens", "5"),
    ("cache_read_tokens", 2_000_001),
    ("batch", 1), ("batch", "true"), ("batch", None),
])
def test_bad_new_fields_rejected(field, value):
    with pytest.raises(ValidationError):
        UsageEvent.model_validate(valid(**{field: value}))


# --- Duplicate check across the schema change ------------------------------

def test_hash_of_an_old_event_is_unchanged():
    assert UsageEvent.model_validate(valid()).payload_hash() == PHASE2_HASH


def test_explicit_defaults_hash_the_same_as_omitted_fields():
    explicit = valid(cache_write_tokens=0, cache_read_tokens=0, batch=False)
    assert UsageEvent.model_validate(explicit).payload_hash() == PHASE2_HASH


@pytest.mark.parametrize("over", [{"cache_write_tokens": 1}, {"cache_read_tokens": 1},
                                  {"batch": True}])
def test_each_new_field_changes_the_hash(over):
    assert UsageEvent.model_validate(valid(**over)).payload_hash() != PHASE2_HASH


def test_resend_with_explicit_defaults_is_a_duplicate(conn):
    ingest(conn, [event(1)])
    r = ingest(conn, [event(1, cache_write_tokens=0, cache_read_tokens=0, batch=False)])
    assert (r.duplicate, r.conflict) == (1, 0)


@pytest.mark.parametrize("over", [{"cache_read_tokens": 10}, {"batch": True}])
def test_resend_with_different_cache_or_batch_is_a_conflict(conn, over):
    ingest(conn, [event(1)])
    r = ingest(conn, [event(1, **over)])
    assert (r.duplicate, r.conflict) == (0, 1)
    assert ledger.total_tokens(conn) == 150  # never overwritten


# --- Migrating a Phase 2 ledger file ---------------------------------------

PHASE2_USAGE_EVENTS = """
CREATE TABLE usage_events (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      TEXT    NOT NULL UNIQUE,
    customer_id   TEXT    NOT NULL,
    model         TEXT    NOT NULL,
    input_tokens  INTEGER NOT NULL CHECK (input_tokens  >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    occurred_at   TEXT    NOT NULL,
    received_at   TEXT    NOT NULL,
    payload_hash  TEXT    NOT NULL
);
CREATE TRIGGER usage_events_no_update BEFORE UPDATE ON usage_events
BEGIN SELECT RAISE(ABORT, 'usage_events is append-only: UPDATE not allowed'); END;
CREATE TRIGGER usage_events_no_delete BEFORE DELETE ON usage_events
BEGIN SELECT RAISE(ABORT, 'usage_events is append-only: DELETE not allowed'); END;
"""


def phase2_db(path):
    old = sqlite3.connect(str(path))
    old.executescript(PHASE2_USAGE_EVENTS)
    e = UsageEvent.model_validate(valid())
    old.execute(
        "INSERT INTO usage_events (event_id, customer_id, model, input_tokens, output_tokens,"
        " occurred_at, received_at, payload_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (e.event_id, e.customer_id, e.model, 100, 50, e.occurred_at.isoformat(),
         "2026-11-01T13:00:00+00:00", PHASE2_HASH))
    old.commit()
    old.close()


def test_migration_adds_columns_without_changing_old_rows(tmp_path):
    db = tmp_path / "phase2.db"
    phase2_db(db)
    conn = ledger.connect(db)
    row = conn.execute("SELECT * FROM usage_events").fetchone()
    assert (row["cache_write_tokens"], row["cache_read_tokens"], row["batch"]) == (0, 0, 0)
    assert (row["input_tokens"], row["output_tokens"], row["payload_hash"]) == (100, 50, PHASE2_HASH)
    assert ledger.total_tokens(conn) == 150
    [assigned] = aggregate.assign(conn)
    assert assigned.units() == {"input": 100, "output": 50, "cache_write": 0, "cache_read": 0}


def test_old_event_resent_after_migration_is_a_duplicate(tmp_path):
    db = tmp_path / "phase2.db"
    phase2_db(db)
    conn = ledger.connect(db)
    r = ingest(conn, [valid()])
    assert (r.accepted, r.duplicate, r.conflict) == (0, 1, 0)
    r = ingest(conn, [valid(batch=True)])
    assert r.conflict == 1


def test_migration_is_idempotent_and_keeps_append_only(tmp_path):
    db = tmp_path / "phase2.db"
    phase2_db(db)
    ledger.connect(db).close()
    conn = ledger.connect(db)  # second run must not try to add the columns again
    assert ledger.count(conn) == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE usage_events SET cache_read_tokens = 5")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM usage_events")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO usage_events (event_id, customer_id, model, input_tokens,"
                     " output_tokens, cache_read_tokens, occurred_at, received_at, payload_hash)"
                     " VALUES ('x', 'cust_1', 'tiny-1', 0, 0, -1, '', '', '')")


# --- Aggregation ------------------------------------------------------------

def test_batch_event_tokens_go_to_batch_categories(conn):
    ingest(conn, [
        event(1, occurred_at=NOV, input_tokens=10, output_tokens=20,
              cache_write_tokens=3, cache_read_tokens=4),
        event(2, occurred_at=NOV, input_tokens=100, output_tokens=200,
              cache_write_tokens=30, cache_read_tokens=40, batch=True),
    ], received_at=at("2026-11-11"))
    [u] = aggregate.aggregate(conn)
    assert {c: u.tokens(c) for c in aggregate.CATEGORIES} == {
        "input": 10, "output": 20, "cache_write": 3, "cache_read": 4,
        "batch_input": 100, "batch_output": 200, "batch_cache_write": 30,
        "batch_cache_read": 40}
    assert u.total_tokens == ledger.total_tokens(conn) == 407


# --- Rating -----------------------------------------------------------------

def test_v2_keeps_v1_base_rates():
    assert V2.effective_from == "2026-11"
    assert V2.rates == V1.rates
    assert rating.book_for_period("2026-10").version == "v1"
    assert rating.book_for_period("2026-11").version == "v2"


def test_derived_rates_are_multiples_of_the_input_rate():
    base = V2.rate("large-1", "input").tiers[0].per_million          # 12.00
    assert V2.rate("large-1", "cache_write").tiers[0].per_million == base * Decimal("1.30")
    assert V2.rate("large-1", "cache_read").tiers[0].per_million == base * Decimal("0.08")
    assert V2.rate("large-1", "batch_output").tiers[0].per_million == Decimal("48.00") * Decimal("0.60")
    assert V2.rate("large-1", "batch_cache_read").tiers[0].per_million == (
        base * Decimal("0.08") * Decimal("0.60"))
    for cat in rating.DERIVED_CATEGORIES:
        assert V2.rate("tiny-1", cat).free_tokens == 0   # no allowance on derived categories


def test_worked_example_one_call_with_cache_reads():
    """Shape of the PDF's worked example, with this project's made-up rates:
    10k new input, 40k cache reads and 3k output on large-1 under v2."""
    u = aggregate.Usage("2026-11", "cust_0001", "large-1", 10_000, 3_000, 1, 0,
                        cache_read_tokens=40_000)
    lines = {ln.category: ln.amount for ln in rating.rate([u], V2)}
    # 10,000 x $12 / 1M = 0.12; 40,000 x $0.96 / 1M = 0.0384; 3,000 x $48 / 1M = 0.144
    assert lines == {"input": Decimal("0.12"), "output": Decimal("0.14"),
                     "cache_read": Decimal("0.04")}
    batch = aggregate.Usage("2026-11", "cust_0001", "large-1", 0, 0, 1, 0,
                            batch_input_tokens=10_000, batch_output_tokens=3_000,
                            batch_cache_read_tokens=40_000)
    blines = {ln.category: ln.amount for ln in rating.rate([batch], V2)}
    # 0.072, 0.0864, 0.02304: each rounded once, half-up
    assert blines == {"input": Decimal("0.00"), "output": Decimal("0.00"),
                      "batch_input": Decimal("0.07"), "batch_output": Decimal("0.09"),
                      "batch_cache_read": Decimal("0.02")}


def test_unused_derived_categories_get_no_line():
    u = aggregate.Usage("2026-11", "cust_0001", "small-1", 5, 5, 1, 0)
    assert [ln.category for ln in rating.rate([u], V2)] == ["input", "output"]


def test_v1_refuses_to_rate_cache_tokens():
    u = aggregate.Usage("2026-10", "cust_0001", "small-1", 5, 5, 1, 0, cache_read_tokens=1)
    with pytest.raises(rating.PriceBookError, match="no price for cache_read"):
        rating.rate([u], V1)


def test_unknown_category_rejected():
    with pytest.raises(rating.PriceBookError, match="unknown token category"):
        V2.rate("tiny-1", "audio")


def v2_data():
    return json.loads((rating.PRICE_BOOK_DIR / "v2.json").read_text())


@pytest.mark.parametrize("edit", [
    lambda d: d["modifiers"].update(batch_multiplier=0.5),        # float
    lambda d: d["modifiers"].update(batch_multiplier="0"),
    lambda d: d["modifiers"].update(batch_multiplier="1.5"),
    lambda d: d["modifiers"]["cache"].pop("tiny-1"),
    lambda d: d["modifiers"]["cache"]["tiny-1"].update(read_multiplier="-0.1"),
    lambda d: d["modifiers"]["cache"]["tiny-1"].pop("write_multiplier"),
    lambda d: d["modifiers"].pop("cache"),
])
def test_malformed_modifiers_rejected(edit):
    data = v2_data()
    edit(data)
    with pytest.raises(rating.PriceBookError):
        rating.parse_price_book(data)


# --- Close and reconcile ----------------------------------------------------

def mixed(conn):
    """October closed on v1 (no cache), then cache and batch usage in November,
    including a late October batch event billed in November."""
    ingest(conn, [event(1, occurred_at=OCT, model="large-1")], received_at=at("2026-11-01T01:00"))
    invoice.close_period(conn, "2026-10", closed_at=at("2026-11-02"))
    ingest(conn, [
        event(2, occurred_at=NOV, model="large-1", cache_read_tokens=40_000),
        event(3, occurred_at=NOV, model="small-1", cache_write_tokens=900, batch=True),
        event(4, occurred_at=OCT, model="tiny-1", cache_read_tokens=10, batch=True),  # late
    ], received_at=at("2026-11-20T01:00"))
    return conn


def test_close_freezes_derived_units_and_reconciles(conn):
    mixed(conn)
    closed = invoice.close_period(conn, "2026-11", closed_at=at("2026-12-01T00:00:01"))
    assert invoice.stored(conn, "2026-11") == closed
    assert invoice.frozen_usage(conn, "2026-11") == aggregate.aggregate(conn, "2026-11")
    units = {(r["model"], r["category"]): r["tokens"] for r in conn.execute(
        "SELECT * FROM period_aggregate_units WHERE period = '2026-11'")}
    assert units == {("large-1", "cache_read"): 40_000, ("small-1", "batch_input"): 100,
                     ("small-1", "batch_output"): 50, ("small-1", "batch_cache_write"): 900,
                     ("tiny-1", "batch_input"): 100, ("tiny-1", "batch_output"): 50,
                     ("tiny-1", "batch_cache_read"): 10}
    report = reconcile(conn)
    assert report.ok, report.gaps


def test_open_period_with_cache_and_batch_reconciles(conn):
    report = reconcile(mixed(conn))
    assert report.ok, report.gaps


def test_frozen_units_are_append_only(conn):
    mixed(conn)
    invoice.close_period(conn, "2026-11", closed_at=at("2026-12-01T00:00:01"))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE period_aggregate_units SET tokens = 0")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM period_aggregate_units")


def test_extra_frozen_cache_units_are_a_gap(conn):
    mixed(conn)
    invoice.close_period(conn, "2026-11", closed_at=at("2026-12-01T00:00:01"))
    conn.execute("INSERT INTO period_aggregate_units VALUES"
                 " ('2026-11', 'cust_0001', 'medium-1', 'cache_read', 5)")
    gaps = {g.check for g in reconcile(conn).gaps}
    assert {"raw_vs_aggregated", "aggregated_vs_invoiced"} <= gaps


def test_tampered_cache_line_amount_is_a_gap(conn):
    mixed(conn)
    invoice.close_period(conn, "2026-11", closed_at=at("2026-12-01T00:00:01"))
    cheap = rating.Modifiers(V2.modifiers.batch_multiplier, V2.modifiers.cache_write,
                             dict(V2.modifiers.cache_read, **{"large-1": Decimal("0.01")}))
    books = {"v1": V1, "v2": rating.PriceBook("v2", "2026-11", V2.rates, cheap)}
    assert "line_amount" in {g.check for g in reconcile(conn, books).gaps}


def test_cache_tokens_in_a_v1_period_fail_loudly(conn):
    ingest(conn, [event(1, occurred_at=OCT, cache_read_tokens=5)], received_at=at("2026-11-01T01:00"))
    with pytest.raises(rating.PriceBookError):
        invoice.close_period(conn, "2026-10", closed_at=at("2026-11-02"))
    assert "unrated_usage" in {g.check for g in reconcile(conn).gaps}
    assert "2026-10" not in aggregate.closes(conn)  # the failed close left nothing behind


# --- Generator ----------------------------------------------------------------

def test_generator_output_unchanged_when_new_rates_are_zero():
    """Same seed, same events as the Phase 2 generator (hash taken on main at
    5482094), so earlier failures still replay."""
    plain = emit.generate(200, seed=4, dup_rate=0.1, conflict_rate=0.05, invalid_rate=0.05,
                          late_rate=0.1, now=at("2026-11-20"))
    digest = hashlib.sha256(json.dumps(plain, sort_keys=True).encode()).hexdigest()
    assert digest == "3e773d37c58b2ca2873ecd7d3347e4d025a7526c14d27269072df71d1ed08f3c"


def test_generator_injects_cache_and_batch():
    events = emit.generate(200, seed=4, cache_rate=0.3, batch_rate=0.2, now=at("2026-11-20"))
    assert sum("cache_read_tokens" in e for e in events) == 60
    assert sum(e.get("batch", False) for e in events) == 40
    assert all(UsageEvent.model_validate(e) for e in events)
