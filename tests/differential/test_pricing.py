"""Differential pricing tests.

1. Rate the same generated usage with an old and a new price book and check
   that only the customers who use the changed price move, by exactly the
   re-rated difference.
2. Check `rating.exact_amount` against an independent reference
   implementation written a different way (clamp each tier band).
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from billing import aggregate, emit, ledger, rating
from billing.events import KNOWN_MODELS
from billing.ingest import ingest

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
V1 = rating.price_books()["v1"]


def usage(seed=5, n=2_000):
    conn = ledger.connect(":memory:")
    ingest(conn, emit.generate(n, seed=seed, now=NOW, late_rate=0.1), received_at=NOW)
    return aggregate.aggregate(conn)


def candidate(edit) -> rating.PriceBook:
    data = json.loads((rating.PRICE_BOOK_DIR / "v1.json").read_text())
    data = copy.deepcopy(data)
    data["version"] = "candidate"
    edit(data)
    return rating.parse_price_book(data)


def test_same_book_gives_zero_differences():
    assert all(d.delta == 0 for d in rating.diff_books(usage(), V1, V1))


def test_price_change_moves_only_affected_customers():
    def raise_large_output(d):
        d["models"]["large-1"]["output"]["tiers"][0]["per_million"] = "60.00"
    new = candidate(raise_large_output)
    rows = usage()
    diffs = {(d.period, d.customer_id): d for d in rating.diff_books(rows, V1, new)}

    affected = {(u.period, u.customer_id) for u in rows if u.model == "large-1" and u.output_tokens}
    for key, d in diffs.items():
        if key in affected:
            assert d.delta > 0
        else:
            assert d.delta == 0
    # every delta equals re-rating just the changed lines
    for (period, customer), d in diffs.items():
        expected = sum(
            (rating.round_line(rating.exact_amount(u.output_tokens, new.rate("large-1", "output"))[0])
             - rating.round_line(rating.exact_amount(u.output_tokens, V1.rate("large-1", "output"))[0])
             for u in rows if (u.period, u.customer_id, u.model) == (period, customer, "large-1")),
            Decimal(0))
        assert d.delta == expected


def test_diff_summary_totals_add_up():
    def cut_tiny(d):
        d["models"]["tiny-1"]["input"]["free_tokens"] = 0
    diffs = rating.diff_books(usage(), V1, candidate(cut_tiny))
    old = sum((d.old_total for d in diffs), Decimal(0))
    new = sum((d.new_total for d in diffs), Decimal(0))
    assert new - old == sum((d.delta for d in diffs), Decimal(0))
    assert new >= old  # removing an allowance never lowers a bill


def reference_amount(tokens: int, rate: rating.Rate) -> Decimal:
    billable = max(0, tokens - rate.free_tokens)
    total = Decimal(0)
    lower = 0
    for tier in rate.tiers:
        upper = tier.up_to if tier.up_to is not None else billable
        band = max(0, min(billable, upper) - lower)
        total += Decimal(band) * tier.per_million / Decimal(1_000_000)
        lower = upper
    return total


@settings(max_examples=300, deadline=None)
@given(st.sampled_from(sorted(KNOWN_MODELS)), st.sampled_from(rating.CATEGORIES),
       st.integers(0, 200_000_000))
def test_rating_matches_reference_implementation(model, category, tokens):
    rate = V1.rate(model, category)
    assert rating.exact_amount(tokens, rate)[0] == reference_amount(tokens, rate)
