"""Revenue recognition: when billed and prepaid amounts become revenue.

Reads billing results (frozen for closed periods, drafts for open ones) and
never writes. Four kinds of revenue line, per customer and period:

- `usage`          usage billed at API rates (the overage left on the
                   invoice after credits): recognized as consumed, in the
                   period the usage is billed in.
- `prepaid_usage`  usage paid for with prepaid credit: the credit was cash
                   received in advance (deferred revenue) and becomes
                   revenue as it is drawn.
- `subscription`   the term fee, billed in advance and recognized evenly over
                   the term's periods. The even share is rounded down to
                   cents and the last period takes the remainder, so the
                   pieces add up to the fee exactly. **[GUESS]**
- `breakage`       prepaid credit left when a grant expires is recognized in
                   its last period, once that period has closed. Until
                   then it stays deferred. **[GUESS]** (recognize at expiry,
                   not in proportion to usage.)

Usage covered by a plan's included credit earns no revenue of its own: the
included credit is part of what the subscription fee pays for, and the fee
is recognized ratably. **[GUESS]** (no separate allocation to included
usage.)

Rollforward, as of any period: everything billed plus all prepaid cash
received = revenue recognized + deferred revenue. `reconcile` checks it.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from billing import aggregate, credits, rating, subscriptions

KINDS = ("usage", "prepaid_usage", "subscription", "breakage")
CENT = Decimal("0.01")
ZERO = Decimal("0.00")


@dataclass(frozen=True)
class RevenueLine:
    period: str
    customer_id: str
    kind: str
    ref: str          # '' for usage, else subscription_id or grant_id
    amount: Decimal


def ratable(fee: Decimal, start: str, months: int) -> list[tuple[str, Decimal]]:
    """Split `fee` evenly over `months` periods, exact to the cent."""
    share = (fee / months).quantize(CENT, rounding=ROUND_DOWN)
    pieces = [share] * (months - 1) + [fee - share * (months - 1)]
    return [(subscriptions.add_periods(start, i), amt) for i, amt in enumerate(pieces)]


@dataclass
class Schedule:
    lines: list[RevenueLine]
    alloc: credits.Allocation

    def recognized(self, through: str | None = None, kind: str | None = None) -> Decimal:
        return sum((ln.amount for ln in self.lines
                    if (through is None or ln.period <= through)
                    and (kind is None or ln.kind == kind)), ZERO)


def schedule(conn: sqlite3.Connection,
             books: dict[str, rating.PriceBook] | None = None) -> Schedule:
    alloc = credits.allocate(conn, books)
    closed = aggregate.closes(conn)
    lines: list[RevenueLine] = []

    for key in sorted(set(alloc.usage) | set(alloc.charges)):
        period, customer = key
        if period in alloc.errors:
            continue
        charges = alloc.charges.get(key, [])
        usage = alloc.usage.get(key, ZERO)
        credit = -sum((c.amount for c in charges if c.kind != "subscription_fee"), ZERO)
        if usage - credit:
            lines.append(RevenueLine(period, customer, "usage", "", usage - credit))
        for c in charges:
            if c.kind == "prepaid_credit":
                lines.append(RevenueLine(period, customer, "prepaid_usage", c.ref, -c.amount))

    for s in subscriptions.subscriptions(conn):
        for period, amount in ratable(s.fee, s.start_period, s.term_months):
            if amount:
                lines.append(RevenueLine(period, s.customer_id, "subscription",
                                         s.subscription_id, amount))

    for g in subscriptions.grants(conn):
        left = alloc.balances[g.grant_id]
        if g.last_period in closed and left > 0:
            lines.append(RevenueLine(g.last_period, g.customer_id, "breakage", g.grant_id, left))

    order = {k: i for i, k in enumerate(KINDS)}
    lines.sort(key=lambda ln: (ln.period, ln.customer_id, order[ln.kind], ln.ref))
    return Schedule(lines, alloc)


@dataclass(frozen=True)
class Rollforward:
    """Totals through one period (inclusive)."""

    through: str
    billed: Decimal            # invoice totals: overage + subscription fees
    prepaid_cash: Decimal      # credit grants bought
    recognized: Decimal
    deferred_subscription: Decimal
    deferred_prepaid: Decimal

    @property
    def balances(self) -> bool:
        return (self.billed + self.prepaid_cash
                == self.recognized + self.deferred_subscription + self.deferred_prepaid)


def rollforward(conn: sqlite3.Connection, through: str,
                sched: Schedule | None = None) -> Rollforward:
    sched = sched or schedule(conn)
    alloc = sched.alloc
    billed = ZERO
    for key in set(alloc.usage) | set(alloc.charges):
        if key[0] <= through and key[0] not in alloc.errors:
            billed += alloc.usage.get(key, ZERO) + sum(
                (c.amount for c in alloc.charges.get(key, [])), ZERO)
    fees = sum((s.fee for s in subscriptions.subscriptions(conn)
                if s.start_period <= through), ZERO)
    grants = [g for g in subscriptions.grants(conn) if g.first_period <= through]
    cash = sum((g.amount for g in grants), ZERO)
    used = sched.recognized(through, "prepaid_usage") + sched.recognized(through, "breakage")
    return Rollforward(
        through, billed, cash, sched.recognized(through),
        deferred_subscription=fees - sched.recognized(through, "subscription"),
        deferred_prepaid=cash - used,
    )


def by_period(sched: Schedule) -> dict[str, dict[str, Decimal]]:
    """Recognized revenue per period and kind, for reports."""
    out: dict[str, dict[str, Decimal]] = defaultdict(lambda: dict.fromkeys(KINDS, ZERO))
    for ln in sched.lines:
        out[ln.period][ln.kind] += ln.amount
    return dict(out)
