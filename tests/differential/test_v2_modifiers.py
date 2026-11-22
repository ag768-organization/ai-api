"""Differential test for price book v2 against v1.

v2 only adds modifiers (cache and batch prices). On usage with no cache or
batch tokens it must bill exactly what v1 bills, customer by customer.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from billing import aggregate, emit, ledger, rating
from billing.ingest import ingest

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
BOOKS = rating.price_books()


def usage(**rates):
    conn = ledger.connect(":memory:")
    ingest(conn, emit.generate(2_000, seed=5, now=NOW, late_rate=0.1, **rates), received_at=NOW)
    return aggregate.aggregate(conn)


def test_v2_bills_plain_usage_exactly_like_v1():
    diffs = rating.diff_books(usage(), BOOKS["v1"], BOOKS["v2"])
    assert diffs and all(d.delta == 0 for d in diffs)


def test_v1_cannot_rate_cache_usage_so_diff_fails_loudly():
    with pytest.raises(rating.PriceBookError):
        rating.diff_books(usage(cache_rate=0.2), BOOKS["v1"], BOOKS["v2"])


def test_cache_and_batch_usage_rates_on_v2():
    rows = usage(cache_rate=0.3, batch_rate=0.3)
    lines = rating.rate(rows, BOOKS["v2"])
    assert {ln.category for ln in lines} == set(aggregate.CATEGORIES)
    assert sum(ln.tokens for ln in lines) == sum(u.total_tokens for u in rows)
