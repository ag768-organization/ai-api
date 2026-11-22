"""Credits: turn subscriptions and prepaid grants into invoice charges.

For each customer and period, after usage has been rated:

1. **Subscription fee.** A term's whole fee is charged in its first period.
2. **Included credit.** If a subscription is active in the period, its
   included credit comes off the usage total first. It never carries over.
3. **Prepaid credit.** Then prepaid grants usable in the period, soonest to
   expire first, draw down until the usage is covered or the grants run out.
4. **Overage.** Whatever usage is left stays on the invoice at API rates.

Credits only ever offset usage, never the subscription fee, so an invoice
can't go negative. **[GUESS]**

Draws are computed from rated usage, which is already in whole cents, so
nothing here rounds. Closed periods' charges are frozen in `invoice_charges`
and are never recomputed. For open periods, every grant's balance starts
from its amount minus all frozen draws, then open periods draw in date
order. So the total drawn from a grant can never exceed its amount, whatever
order periods close in.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from billing import aggregate, rating
from billing.subscriptions import CreditGrant, Subscription, grants, subscriptions

KINDS = ("subscription_fee", "included_credit", "prepaid_credit")
ZERO = Decimal("0.00")


@dataclass(frozen=True)
class Charge:
    period: str
    customer_id: str
    kind: str
    ref: str          # subscription_id or grant_id
    amount: Decimal   # fee positive, credits negative; whole cents


@dataclass
class Allocation:
    charges: dict[tuple[str, str], list[Charge]] = field(default_factory=dict)
    balances: dict[str, Decimal] = field(default_factory=dict)   # grant_id -> unspent
    usage: dict[tuple[str, str], Decimal] = field(default_factory=dict)  # rated usage totals
    # Open periods whose usage can't be rated (e.g. cache tokens on v1). They
    # get no charges; rating them raises where it matters (draft, close).
    errors: dict[str, str] = field(default_factory=dict)

    def for_period(self, period: str) -> list[Charge]:
        return [c for (p, _), cs in sorted(self.charges.items()) if p == period for c in cs]


def frozen_charges(conn: sqlite3.Connection, period: str | None = None) -> list[Charge]:
    sql = "SELECT * FROM invoice_charges"
    args: tuple = ()
    if period is not None:
        sql, args = sql + " WHERE period = ?", (period,)
    order = {k: i for i, k in enumerate(KINDS)}
    rows = [Charge(r["period"], r["customer_id"], r["kind"], r["ref"], Decimal(r["amount"]))
            for r in conn.execute(sql, args)]
    return sorted(rows, key=lambda c: (c.period, c.customer_id, order.get(c.kind, 99), c.ref))


def frozen_usage_totals(conn: sqlite3.Connection) -> dict[tuple[str, str], Decimal]:
    out: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    for r in conn.execute("SELECT period, customer_id, amount FROM invoice_lines"):
        out[(r["period"], r["customer_id"])] += Decimal(r["amount"])
    return dict(out)


def charges_for(period: str, customer_id: str, usage_total: Decimal,
                subs: list[Subscription], live_grants: list[CreditGrant],
                balances: dict[str, Decimal]) -> list[Charge]:
    """Charges for one invoice. Spends from `balances` in place."""
    out = []
    for s in subs:
        if s.start_period == period:
            out.append(Charge(period, customer_id, "subscription_fee", s.subscription_id, s.fee))
    left = usage_total
    active = [s for s in subs if s.active_in(period)]
    if active and left > 0 and active[0].included_credit > 0:
        take = min(active[0].included_credit, left)
        out.append(Charge(period, customer_id, "included_credit", active[0].subscription_id, -take))
        left -= take
    usable = sorted((g for g in live_grants if g.usable_in(period)),
                    key=lambda g: (g.last_period, g.granted_at, g.grant_id))
    for g in usable:
        if left <= 0:
            break
        take = min(balances[g.grant_id], left)
        if take > 0:
            out.append(Charge(period, customer_id, "prepaid_credit", g.grant_id, -take))
            balances[g.grant_id] -= take
            left -= take
    return out


def allocate(conn: sqlite3.Connection, books: dict[str, rating.PriceBook] | None = None,
             overrides: dict[str, rating.PriceBook] | None = None) -> Allocation:
    """Charges for every invoice, frozen ones as stored and open ones computed.

    `overrides` rates a given open period on a specific book (used by
    `close_period` and `draft` when the caller passes one)."""
    books = rating.price_books() if books is None else books
    overrides = overrides or {}
    closed = aggregate.closes(conn)
    subs_by_customer: dict[str, list[Subscription]] = defaultdict(list)
    for s in subscriptions(conn):
        subs_by_customer[s.customer_id].append(s)
    grants_by_customer: dict[str, list[CreditGrant]] = defaultdict(list)
    all_grants = grants(conn)
    for g in all_grants:
        grants_by_customer[g.customer_id].append(g)

    result = Allocation()
    result.usage.update(frozen_usage_totals(conn))
    balances = {g.grant_id: g.amount for g in all_grants}
    for c in frozen_charges(conn):
        result.charges.setdefault((c.period, c.customer_id), []).append(c)
        if c.kind == "prepaid_credit" and c.ref in balances:
            balances[c.ref] += c.amount   # amount is negative

    usage = aggregate.aggregate(conn)
    open_periods = sorted({u.period for u in usage if u.period not in closed}
                          | {s.start_period for s in subscriptions(conn)
                             if s.start_period not in closed})
    for period in open_periods:
        rows = [u for u in usage if u.period == period]
        totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
        try:
            if rows:
                book = overrides.get(period) or rating.book_for_period(period, books)
                for line in rating.rate(rows, book):
                    totals[line.customer_id] += line.amount
        except rating.PriceBookError as exc:
            result.errors[period] = str(exc)
            continue
        customers = set(totals) | {s.customer_id for s in subscriptions(conn)
                                   if s.start_period == period}
        for customer in sorted(customers):
            result.usage[(period, customer)] = totals.get(customer, ZERO)
            charges = charges_for(period, customer, totals.get(customer, ZERO),
                                  subs_by_customer[customer], grants_by_customer[customer],
                                  balances)
            if charges:
                result.charges[(period, customer)] = charges
    result.balances = balances
    return result


def remaining_credit(conn: sqlite3.Connection, customer_id: str, period: str,
                     books: dict[str, rating.PriceBook] | None = None) -> Decimal:
    """Credit the customer can still spend in `period`: unused included credit
    plus unspent prepaid grants usable then, after all usage so far."""
    alloc = allocate(conn, books)
    left = ZERO
    for s in subscriptions(conn, customer_id):
        if s.active_in(period):
            used = -sum((c.amount for c in alloc.charges.get((period, customer_id), [])
                         if c.kind == "included_credit"), ZERO)
            left += s.included_credit - used
    for g in grants(conn, customer_id):
        if g.usable_in(period):
            left += alloc.balances[g.grant_id]
    return left
