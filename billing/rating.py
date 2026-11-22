"""Rate: turn aggregated token counts into money, using a versioned price book.

Price books are data (`billing/price_books/*.json`), not code. Each one sets,
per model and per token category (`input`, `output`):

- `free_tokens`: a free allowance per customer, model, category and period
- `tiers`: graduated volume tiers, priced per million tokens. `up_to` counts
  billable tokens (after the allowance); the last tier has `up_to: null`.

Only `input` and `output` are priced directly. From price book v2, a
`modifiers` block derives every other category from them (**[GUESS]** on
all of these rules):

- `cache_write` and `cache_read` are the model's input rate times a per-model
  multiplier, tier for tier.
- `batch_<category>` is that category's rate times one book-wide
  `batch_multiplier` (so batch combines with the cache multipliers).
- Derived categories get no free allowance, and each counts its own tiers.

A book without `modifiers` (v1) cannot rate a derived category: rating
cache or batch tokens on it raises instead of billing them at zero.

Money rules (CLAUDE.md invariant 3): Decimal end to end, prices must be JSON
strings (a float in a price book is rejected), and each line is rounded to
cents exactly once, half-up, from its exact unrounded amount.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Iterable

from billing.aggregate import CATEGORIES as ALL_CATEGORIES
from billing.aggregate import Usage
from billing.events import KNOWN_MODELS

PRICE_BOOK_DIR = Path(__file__).parent / "price_books"
# Categories a price book prices directly. Each gets a line on every invoice,
# even at zero tokens; derived categories get a line only when used.
CATEGORIES = ("input", "output")
DERIVED_CATEGORIES = tuple(c for c in ALL_CATEGORIES if c not in CATEGORIES)
CENT = Decimal("0.01")
PER_MILLION = 1_000_000


class PriceBookError(ValueError):
    """A price book file is malformed."""


@dataclass(frozen=True)
class Tier:
    up_to: int | None          # billable tokens, cumulative; None = no limit
    per_million: Decimal


@dataclass(frozen=True)
class Rate:
    free_tokens: int
    tiers: tuple[Tier, ...]


@dataclass(frozen=True)
class Modifiers:
    batch_multiplier: Decimal
    cache_write: dict[str, Decimal]     # model -> multiple of the input rate
    cache_read: dict[str, Decimal]


@dataclass(frozen=True)
class PriceBook:
    version: str
    effective_from: str        # first period ('YYYY-MM') this book applies to
    rates: dict[str, dict[str, Rate]]   # model -> category -> Rate
    modifiers: Modifiers | None = None

    def rate(self, model: str, category: str) -> Rate:
        if category in CATEGORIES:
            return self.rates[model][category]
        if category not in DERIVED_CATEGORIES:
            raise PriceBookError(f"unknown token category {category!r}")
        if self.modifiers is None:
            raise PriceBookError(f"price book {self.version} has no price for {category}")
        base = category.removeprefix("batch_")
        multiplier = Decimal(1)
        if base == "cache_write":
            base, multiplier = "input", self.modifiers.cache_write[model]
        elif base == "cache_read":
            base, multiplier = "input", self.modifiers.cache_read[model]
        if category.startswith("batch_"):
            multiplier *= self.modifiers.batch_multiplier
        # Exact Decimal products: no rounding until the line total.
        tiers = tuple(Tier(t.up_to, t.per_million * multiplier)
                      for t in self.rates[model][base].tiers)
        return Rate(0, tiers)


def _int(v: Any, what: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise PriceBookError(f"{what} must be a non-negative integer")
    return v


def _decimal(v: Any, what: str) -> Decimal:
    if not isinstance(v, str):
        raise PriceBookError(f"{what} must be a string, got {v!r}")
    try:
        dec = Decimal(v)
    except ArithmeticError as exc:
        raise PriceBookError(f"{what}: bad number {v!r}") from exc
    if not dec.is_finite() or dec < 0:
        raise PriceBookError(f"{what}: bad number {v!r}")
    return dec


def parse_modifiers(data: Any, version: str) -> Modifiers:
    if not isinstance(data, dict) or set(data) != {"batch_multiplier", "cache"}:
        raise PriceBookError(f"{version}: modifiers needs batch_multiplier and cache")
    batch = _decimal(data["batch_multiplier"], f"{version}: batch_multiplier")
    if not 0 < batch <= 1:
        raise PriceBookError(f"{version}: batch_multiplier must be in (0, 1]")
    cache = data["cache"]
    if not isinstance(cache, dict) or set(cache) != set(KNOWN_MODELS):
        raise PriceBookError(f"{version}: cache must cover exactly {sorted(KNOWN_MODELS)}")
    write, read = {}, {}
    for model, spec in cache.items():
        if not isinstance(spec, dict) or set(spec) != {"write_multiplier", "read_multiplier"}:
            raise PriceBookError(f"{version}/{model}: cache needs write_ and read_multiplier")
        write[model] = _decimal(spec["write_multiplier"], f"{version}/{model}: write_multiplier")
        read[model] = _decimal(spec["read_multiplier"], f"{version}/{model}: read_multiplier")
    return Modifiers(batch, write, read)


def parse_price_book(data: dict[str, Any]) -> PriceBook:
    try:
        version = data["version"]
        effective_from = data["effective_from"]
        models = data["models"]
    except (KeyError, TypeError) as exc:
        raise PriceBookError(f"missing field {exc}") from exc
    if set(models) != set(KNOWN_MODELS):
        raise PriceBookError(f"price book {version} must price exactly {sorted(KNOWN_MODELS)}")
    rates: dict[str, dict[str, Rate]] = {}
    for model, cats in models.items():
        if set(cats) != set(CATEGORIES):
            raise PriceBookError(f"{model}: categories must be {CATEGORIES}")
        rates[model] = {}
        for cat, spec in cats.items():
            where = f"{version}/{model}/{cat}"
            tiers = []
            last = 0
            raw_tiers = spec.get("tiers") or []
            if not raw_tiers:
                raise PriceBookError(f"{where}: no tiers")
            for i, t in enumerate(raw_tiers):
                price = t.get("per_million")
                if not isinstance(price, str):
                    raise PriceBookError(f"{where}: per_million must be a string, got {price!r}")
                try:
                    dec = Decimal(price)
                except ArithmeticError as exc:
                    raise PriceBookError(f"{where}: bad price {price!r}") from exc
                if not dec.is_finite() or dec < 0:
                    raise PriceBookError(f"{where}: bad price {price!r}")
                up_to = t.get("up_to")
                is_last = i == len(raw_tiers) - 1
                if is_last != (up_to is None):
                    raise PriceBookError(f"{where}: only the last tier has up_to null")
                if up_to is not None:
                    up_to = _int(up_to, f"{where}: up_to")
                    if up_to <= last:
                        raise PriceBookError(f"{where}: tier limits must increase")
                    last = up_to
                tiers.append(Tier(up_to, dec))
            rates[model][cat] = Rate(_int(spec.get("free_tokens", 0), f"{where}: free_tokens"),
                                     tuple(tiers))
    modifiers = parse_modifiers(data["modifiers"], version) if "modifiers" in data else None
    return PriceBook(version, effective_from, rates, modifiers)


def load_price_book(path: str | Path) -> PriceBook:
    # parse_float=Decimal is belt and braces: prices must be strings anyway.
    return parse_price_book(json.loads(Path(path).read_text(), parse_float=Decimal))


def price_books(directory: Path = PRICE_BOOK_DIR) -> dict[str, PriceBook]:
    books: dict[str, PriceBook] = {}
    for path in sorted(directory.glob("*.json")):
        book = load_price_book(path)
        if book.version in books:
            raise PriceBookError(f"duplicate price book version {book.version}")
        books[book.version] = book
    return books


def book_for_period(period: str, books: dict[str, PriceBook] | None = None) -> PriceBook:
    """The book with the latest `effective_from` on or before the period.
    **[GUESS]** a whole period is rated on one book; no mid-month changes."""
    books = price_books() if books is None else books
    eligible = [b for b in books.values() if b.effective_from <= period]
    if not eligible:
        raise PriceBookError(f"no price book in effect for {period}")
    return max(eligible, key=lambda b: (b.effective_from, b.version))


def exact_amount(tokens: int, rate: Rate) -> tuple[Decimal, int]:
    """Unrounded amount for `tokens`, and how many tokens the allowance covered."""
    free = min(tokens, rate.free_tokens)
    billable = tokens - free
    amount = Decimal(0)
    floor = 0
    for tier in rate.tiers:
        if billable <= floor:
            break
        top = billable if tier.up_to is None else min(billable, tier.up_to)
        # scaleb(-6) divides by a million exactly: no context rounding.
        amount += (Decimal(top - floor) * tier.per_million).scaleb(-6)
        floor = top
    return amount, free


def round_line(amount: Decimal) -> Decimal:
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class RatedLine:
    period: str
    customer_id: str
    model: str
    category: str
    tokens: int
    free_tokens: int
    amount: Decimal            # rounded to cents, once
    price_book_version: str


def rate(usage: Iterable[Usage], book: PriceBook) -> list[RatedLine]:
    """One line per (usage row, token category): always for input and output
    (even at zero, so units reconcile line for line), and for a derived
    category whenever it has tokens."""
    lines = []
    for u in usage:
        for cat in ALL_CATEGORIES:
            tokens = u.tokens(cat)
            if cat not in CATEGORIES and tokens == 0:
                continue
            exact, free = exact_amount(tokens, book.rate(u.model, cat))
            lines.append(RatedLine(u.period, u.customer_id, u.model, cat, tokens, free,
                                   round_line(exact), book.version))
    return lines


@dataclass(frozen=True)
class PriceDiff:
    period: str
    customer_id: str
    old_total: Decimal
    new_total: Decimal

    @property
    def delta(self) -> Decimal:
        return self.new_total - self.old_total


def diff_books(usage: list[Usage], old: PriceBook, new: PriceBook) -> list[PriceDiff]:
    """Differential test helper: rate the same usage with two books and
    compare totals per customer and period."""
    def totals(book: PriceBook) -> dict[tuple[str, str], Decimal]:
        out: dict[tuple[str, str], Decimal] = {}
        for line in rate(usage, book):
            key = (line.period, line.customer_id)
            out[key] = out.get(key, Decimal(0)) + line.amount
        return out

    a, b = totals(old), totals(new)
    return [PriceDiff(p, c, a.get((p, c), Decimal(0)), b.get((p, c), Decimal(0)))
            for p, c in sorted(set(a) | set(b))]
