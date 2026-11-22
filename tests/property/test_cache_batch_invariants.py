"""Invariants 6 to 9 with cache and batch token categories (price book v2).

Same rules as test_invariants.py: each test builds its own in-memory ledger.
October is rated on v1, which has no cache prices, so cache and batch usage
is only ever received after October has closed (late October events then
bill in November on v2).
"""
from __future__ import annotations

import random
from datetime import datetime, timezone
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from billing import aggregate, emit, invoice, ledger, rating
from billing.aggregate import Usage
from billing.events import KNOWN_MODELS
from billing.ingest import ingest
from billing.reconcile import reconcile

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
BEFORE_OCT_CLOSE = datetime(2026, 11, 1, 1, tzinfo=timezone.utc)
OCT_CLOSE = datetime(2026, 11, 2, tzinfo=timezone.utc)
V2 = rating.price_books()["v2"]
SETTINGS = settings(max_examples=60, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow])
tokens = st.integers(0, 50_000_000)

usage_rows = st.lists(
    st.builds(Usage,
              period=st.just("2026-11"),
              customer_id=st.sampled_from(["cust_0001", "cust_0002", "cust_0003"]),
              model=st.sampled_from(sorted(KNOWN_MODELS)),
              input_tokens=tokens, output_tokens=tokens,
              event_count=st.just(1), late_count=st.just(0),
              cache_write_tokens=tokens, cache_read_tokens=tokens,
              batch_input_tokens=tokens, batch_output_tokens=tokens,
              batch_cache_write_tokens=tokens, batch_cache_read_tokens=tokens),
    max_size=20,
    unique_by=lambda u: (u.period, u.customer_id, u.model),
)

rates = st.fixed_dictionaries({
    "dup_rate": st.sampled_from([0.0, 0.05, 0.3]),
    "conflict_rate": st.sampled_from([0.0, 0.02]),
    "invalid_rate": st.sampled_from([0.0, 0.03]),
    "late_rate": st.sampled_from([0.0, 0.1, 0.5]),
    "cache_rate": st.sampled_from([0.1, 0.5, 1.0]),
    "batch_rate": st.sampled_from([0.0, 0.2, 0.6]),
})


def loaded(seed, r, n=150):
    """October closed on plain usage, then cache/batch usage received after."""
    conn = ledger.connect(":memory:")
    plain = {k: v for k, v in r.items() if k not in ("cache_rate", "batch_rate")}
    ingest(conn, emit.generate(n // 2, seed=seed, now=NOW, **plain), received_at=BEFORE_OCT_CLOSE)
    invoice.close_period(conn, "2026-10", closed_at=OCT_CLOSE)
    ingest(conn, emit.generate(n, seed=seed + 1, now=NOW, **r), received_at=NOW)
    return conn


# --- Invariant 6 ------------------------------------------------------------

@SETTINGS
@given(usage_rows)
def test_invoice_total_equals_sum_of_rated_usage_in_every_category(rows):
    lines = rating.rate(rows, V2)
    invoices = invoice.build(lines)
    assert sum((i.total for i in invoices), Decimal(0)) == sum((ln.amount for ln in lines), Decimal(0))
    for inv in invoices:
        assert inv.total == sum((ln.amount for ln in inv.lines), Decimal(0))
        assert inv.total.as_tuple().exponent == -2
    for ln in lines:
        exact, _ = rating.exact_amount(ln.tokens, V2.rate(ln.model, ln.category))
        assert ln.amount == rating.round_line(exact)
    assert sum(i.tokens for i in invoices) == sum(u.total_tokens for u in rows)


def multiplier(model: str, category: str) -> Decimal:
    m = V2.modifiers
    out = Decimal(1)
    base = category.removeprefix("batch_")
    if base == "cache_write":
        out *= m.cache_write[model]
    if base == "cache_read":
        out *= m.cache_read[model]
    if category.startswith("batch_"):
        out *= m.batch_multiplier
    return out


@settings(max_examples=300, deadline=None)
@given(st.sampled_from(sorted(KNOWN_MODELS)), st.sampled_from(rating.DERIVED_CATEGORIES),
       st.integers(0, 200_000_000))
def test_derived_amount_is_multiplier_times_base_amount(model, category, n):
    """Differential check of the derived rate, computed the other way round:
    rate on the base (input or output) tiers with no allowance, then scale."""
    source = "output" if category.removeprefix("batch_") == "output" else "input"
    base = rating.Rate(0, V2.rate(model, source).tiers)
    expected = rating.exact_amount(n, base)[0] * multiplier(model, category)
    assert rating.exact_amount(n, V2.rate(model, category))[0] == expected


@SETTINGS
@given(st.sampled_from(sorted(KNOWN_MODELS)), st.sampled_from(rating.DERIVED_CATEGORIES),
       st.integers(0, 100_000_000), st.integers(0, 100_000_000))
def test_more_derived_tokens_never_cost_less(model, category, a, b):
    rate = V2.rate(model, category)
    lo, hi = sorted((a, b))
    assert rating.exact_amount(lo, rate)[0] <= rating.exact_amount(hi, rate)[0]


@SETTINGS
@given(st.sampled_from(sorted(KNOWN_MODELS)), st.sampled_from(aggregate.BASE_CATEGORIES),
       st.integers(1, 100_000_000))
def test_batch_never_costs_more_than_standard(model, base, n):
    standard = rating.Rate(0, V2.rate(model, base).tiers)
    assert rating.exact_amount(n, V2.rate(model, f"batch_{base}"))[0] <= rating.exact_amount(n, standard)[0]


# --- Invariant 7 ------------------------------------------------------------

@SETTINGS
@given(st.integers(0, 2**32), rates, st.integers(0, 2**32), st.integers(1, 4))
def test_replay_with_cache_and_batch_never_changes_totals(seed, r, shuffle_seed, batches):
    # No late events: with October still open they would be rated on v1,
    # which can't price cache tokens (reconcile rightly flags that).
    events = emit.generate(120, seed=seed, now=NOW, **dict(r, late_rate=0.0))
    once = ledger.connect(":memory:")
    ingest(once, events, received_at=NOW)

    replayed = ledger.connect(":memory:")
    rng = random.Random(shuffle_seed)
    stream = events + rng.choices(events, k=len(events) // 2)
    rng.shuffle(stream)
    size = -(-len(stream) // batches)
    for i in range(0, len(stream), size):
        ingest(replayed, stream[i:i + size], received_at=NOW)

    if r["conflict_rate"] == 0:
        assert aggregate.aggregate(replayed) == aggregate.aggregate(once)
        assert invoice.draft(replayed, "2026-11") == invoice.draft(once, "2026-11")
    assert reconcile(replayed).ok


# --- Invariant 8 ------------------------------------------------------------

@SETTINGS
@given(st.integers(0, 2**32), rates)
def test_every_cache_and_batch_token_lands_in_exactly_one_period(seed, r):
    conn = loaded(seed, r)
    usage = aggregate.aggregate(conn)
    assert sum(u.event_count for u in usage) == ledger.count(conn)
    assert sum(u.total_tokens for u in usage) == ledger.total_tokens(conn)
    by_cat = {c: sum(u.tokens(c) for u in usage) for c in aggregate.CATEGORIES}
    for base in aggregate.BASE_CATEGORIES:
        col = f"{base}_tokens"
        standard, batched = conn.execute(
            f"SELECT COALESCE(SUM(CASE WHEN batch = 0 THEN {col} END), 0),"
            f" COALESCE(SUM(CASE WHEN batch = 1 THEN {col} END), 0) FROM usage_events"
        ).fetchone()
        assert (by_cat[base], by_cat[f"batch_{base}"]) == (standard, batched)


# --- Invariant 9 ------------------------------------------------------------

@SETTINGS
@given(st.integers(0, 2**32), rates, st.integers(20, 300), st.booleans())
def test_reconciliation_has_zero_gaps_with_cache_and_batch(seed, r, n, close_november):
    conn = loaded(seed, r, n)
    if close_november:
        invoice.close_period(conn, "2026-11", closed_at=datetime(2026, 12, 1, 1, tzinfo=timezone.utc))
    report = reconcile(conn)
    assert report.ok, report.gaps
