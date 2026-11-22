import json
from decimal import Decimal

import pytest

from billing import rating
from billing.aggregate import Usage

V1 = rating.price_books()["v1"]


def book_data(**over):
    data = json.loads((rating.PRICE_BOOK_DIR / "v1.json").read_text())
    data.update(over)
    return data


def flat(price: str, free: int = 0, tiers=None) -> rating.Rate:
    tiers = tiers or [(None, price)]
    return rating.Rate(free, tuple(rating.Tier(u, Decimal(p)) for u, p in tiers))


def test_v1_loads_and_prices_every_model():
    assert V1.version == "v1"
    assert V1.rate("large-1", "output").tiers[0].per_million == Decimal("48.00")


def test_float_price_rejected():
    data = book_data()
    data["models"]["tiny-1"]["input"]["tiers"][0]["per_million"] = 0.2
    with pytest.raises(rating.PriceBookError, match="string"):
        rating.parse_price_book(data)


@pytest.mark.parametrize("bad", ["-1", "NaN", "Infinity", "abc"])
def test_bad_price_rejected(bad):
    data = book_data()
    data["models"]["tiny-1"]["input"]["tiers"][0]["per_million"] = bad
    with pytest.raises(rating.PriceBookError):
        rating.parse_price_book(data)


def test_missing_model_rejected():
    data = book_data()
    del data["models"]["tiny-1"]
    with pytest.raises(rating.PriceBookError):
        rating.parse_price_book(data)


def test_tiers_must_increase_and_end_open():
    data = book_data()
    data["models"]["tiny-1"]["input"]["tiers"] = [
        {"up_to": 100, "per_million": "1"}, {"up_to": 50, "per_million": "1"},
        {"up_to": None, "per_million": "1"}]
    with pytest.raises(rating.PriceBookError, match="increase"):
        rating.parse_price_book(data)
    data["models"]["tiny-1"]["input"]["tiers"] = [{"up_to": 100, "per_million": "1"}]
    with pytest.raises(rating.PriceBookError, match="last tier"):
        rating.parse_price_book(data)


def test_free_allowance_comes_off_first():
    amount, free = rating.exact_amount(1_050_000, flat("2.00", free=50_000))
    assert (amount, free) == (Decimal("2.00"), 50_000)
    assert rating.exact_amount(10_000, flat("2.00", free=50_000)) == (Decimal(0), 10_000)


def test_graduated_tiers():
    rate = flat("", tiers=[(1_000_000, "10.00"), (None, "5.00")])
    amount, _ = rating.exact_amount(3_000_000, rate)
    assert amount == Decimal("20.00")  # 1M at $10 + 2M at $5


def test_tier_limits_count_tokens_after_allowance():
    rate = flat("", free=500_000, tiers=[(1_000_000, "10.00"), (None, "5.00")])
    amount, _ = rating.exact_amount(1_500_000, rate)
    assert amount == Decimal("10.00")  # 1M billable, all in tier 1


def test_line_rounds_half_up_once():
    # 5,000 tokens at $1/MTok = $0.005 exactly: half-up gives 0.01
    # (banker's rounding would give 0.00).
    exact, _ = rating.exact_amount(5_000, flat("1.00"))
    assert exact == Decimal("0.005")
    assert rating.round_line(exact) == Decimal("0.01")


def test_no_rounding_between_tiers():
    # Each tier alone is $0.004 (would round to 0.00); together $0.008 -> 0.01.
    rate = flat("", tiers=[(4_000, "1.00"), (None, "1.00")])
    exact, _ = rating.exact_amount(8_000, rate)
    assert rating.round_line(exact) == Decimal("0.01")


def test_rate_emits_input_and_output_lines_with_version():
    lines = rating.rate([Usage("2026-10", "cust_0001", "large-1", 1_000_000, 0, 3, 0)], V1)
    assert [(ln.category, ln.tokens, ln.amount, ln.price_book_version) for ln in lines] == [
        ("input", 1_000_000, Decimal("12.00"), "v1"), ("output", 0, Decimal("0.00"), "v1")]


def test_book_for_period_picks_latest_effective():
    v2 = rating.PriceBook("v2", "2026-12", V1.rates)
    books = {"v1": V1, "v2": v2}
    assert rating.book_for_period("2026-11", books).version == "v1"
    assert rating.book_for_period("2026-12", books).version == "v2"
    with pytest.raises(rating.PriceBookError):
        rating.book_for_period("2025-12", books)
