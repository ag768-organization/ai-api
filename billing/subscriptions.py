"""Accounts, subscription plans and prepaid credit grants.

Everything here is an append-only record of what a customer signed up for
or paid for. Nothing in this module bills anything: `credits.py` turns these
records plus rated usage into invoice charges.

- An **account** sets the billing mode. `postpaid` (also what a customer with
  no account row gets) is billed in arrears for any usage past its credits.
  `prepaid` is stopped at the API once its credits run out.
- A **subscription** is one fixed term of a plan: a fee for the whole term,
  billed in advance in its first period, plus included usage credit for each
  period of the term. The plan's terms are copied onto the row at signup.
- A **credit grant** is a prepaid credit purchase. It can be spent from the
  period it was bought in through `last_period`, then expires.
  **[GUESS]** credits expire at the end of the 12th period after purchase,
  i.e. a grant bought any time in 2026-11 is usable through 2027-10.

Money is Decimal dollars stored as strings, always whole cents.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from billing import aggregate

PLANS_PATH = Path(__file__).parent / "plans.json"
CREDIT_LIFETIME_PERIODS = 12  # GUESS
BILLING_MODES = ("postpaid", "prepaid")
CENT = Decimal("0.01")


class SubscriptionError(ValueError):
    pass


def add_periods(period: str, n: int) -> str:
    year, month = (int(p) for p in period.split("-"))
    index = year * 12 + (month - 1) + n
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def money(value: Any, what: str, *, positive: bool = False) -> Decimal:
    """A Decimal amount in whole cents, from a string or Decimal (never a float)."""
    if not isinstance(value, (str, Decimal)):
        raise SubscriptionError(f"{what} must be a string or Decimal, got {value!r}")
    try:
        dec = Decimal(value)
    except ArithmeticError as exc:
        raise SubscriptionError(f"{what}: bad amount {value!r}") from exc
    if not dec.is_finite() or dec < 0 or (positive and dec == 0):
        raise SubscriptionError(f"{what}: bad amount {value!r}")
    if dec != dec.quantize(CENT):
        raise SubscriptionError(f"{what} must be whole cents, got {value!r}")
    return dec.quantize(CENT)


# --- Plans --------------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    plan_id: str
    fee: Decimal
    term_months: int
    included_credit: Decimal


def load_plans(path: Path = PLANS_PATH) -> dict[str, Plan]:
    data = json.loads(path.read_text())
    plans = {}
    for plan_id, spec in data["plans"].items():
        term = spec["term_months"]
        if not isinstance(term, int) or isinstance(term, bool) or term <= 0:
            raise SubscriptionError(f"{plan_id}: term_months must be a positive integer")
        plans[plan_id] = Plan(plan_id, money(spec["fee"], f"{plan_id}: fee"), term,
                              money(spec["included_credit"], f"{plan_id}: included_credit"))
    return plans


# --- Records -------------------------------------------------------------------

@dataclass(frozen=True)
class Subscription:
    subscription_id: str
    customer_id: str
    plan_id: str
    fee: Decimal
    included_credit: Decimal
    term_months: int
    start_period: str

    @property
    def end_period(self) -> str:
        """Last period of the term (inclusive)."""
        return add_periods(self.start_period, self.term_months - 1)

    def active_in(self, period: str) -> bool:
        return self.start_period <= period <= self.end_period


@dataclass(frozen=True)
class CreditGrant:
    grant_id: str
    customer_id: str
    amount: Decimal
    granted_at: datetime
    first_period: str
    last_period: str

    def usable_in(self, period: str) -> bool:
        return self.first_period <= period <= self.last_period


def _now(ts: datetime | None) -> datetime:
    ts = ts or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        raise SubscriptionError("timestamps must be timezone-aware")
    return ts.astimezone(timezone.utc)


def _require_open(conn: sqlite3.Connection, period: str, what: str) -> None:
    if period in aggregate.closes(conn):
        raise SubscriptionError(f"{what}: period {period} is already closed")


def open_account(conn: sqlite3.Connection, customer_id: str, billing_mode: str,
                 *, opened_at: datetime | None = None) -> None:
    if billing_mode not in BILLING_MODES:
        raise SubscriptionError(f"billing_mode must be one of {BILLING_MODES}")
    with conn:
        conn.execute("INSERT INTO accounts (customer_id, billing_mode, opened_at) VALUES (?, ?, ?)",
                     (customer_id, billing_mode, _now(opened_at).isoformat()))


def billing_mode(conn: sqlite3.Connection, customer_id: str) -> str:
    row = conn.execute("SELECT billing_mode FROM accounts WHERE customer_id = ?",
                       (customer_id,)).fetchone()
    return row["billing_mode"] if row else "postpaid"


def subscribe(conn: sqlite3.Connection, customer_id: str, plan_id: str, start_period: str,
              *, created_at: datetime | None = None, subscription_id: str | None = None,
              plans: dict[str, Plan] | None = None) -> Subscription:
    """Start one term of `plan_id` at `start_period`. Refuses a closed start
    period (its invoice is frozen) and any overlap with another term."""
    plans = load_plans() if plans is None else plans
    if plan_id not in plans:
        raise SubscriptionError(f"unknown plan {plan_id!r}")
    plan = plans[plan_id]
    sub = Subscription(subscription_id or f"sub_{uuid.uuid4().hex}", customer_id, plan_id,
                       plan.fee, plan.included_credit, plan.term_months, start_period)
    with conn:
        _require_open(conn, start_period, "subscription")
        for other in subscriptions(conn, customer_id):
            if other.start_period <= sub.end_period and sub.start_period <= other.end_period:
                raise SubscriptionError(
                    f"{customer_id} already has {other.subscription_id} for "
                    f"{other.start_period}..{other.end_period}")
        conn.execute(
            "INSERT INTO subscriptions (subscription_id, customer_id, plan_id, fee,"
            " included_credit, term_months, start_period, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sub.subscription_id, customer_id, plan_id, str(sub.fee), str(sub.included_credit),
             sub.term_months, start_period, _now(created_at).isoformat()))
    return sub


def grant_credit(conn: sqlite3.Connection, customer_id: str, amount: str | Decimal,
                 *, granted_at: datetime | None = None, grant_id: str | None = None) -> CreditGrant:
    """Record a prepaid credit purchase. Refuses if the purchase period is
    already closed, since the credit would apply to a frozen invoice."""
    granted_at = _now(granted_at)
    first = aggregate.period_of(granted_at)
    grant = CreditGrant(grant_id or f"grant_{uuid.uuid4().hex}", customer_id,
                        money(amount, "amount", positive=True), granted_at, first,
                        add_periods(first, CREDIT_LIFETIME_PERIODS - 1))
    with conn:
        _require_open(conn, first, "credit grant")
        conn.execute(
            "INSERT INTO credit_grants (grant_id, customer_id, amount, granted_at,"
            " first_period, last_period) VALUES (?, ?, ?, ?, ?, ?)",
            (grant.grant_id, customer_id, str(grant.amount), granted_at.isoformat(),
             grant.first_period, grant.last_period))
    return grant


def subscriptions(conn: sqlite3.Connection, customer_id: str | None = None) -> list[Subscription]:
    sql = "SELECT * FROM subscriptions"
    args: tuple = ()
    if customer_id is not None:
        sql, args = sql + " WHERE customer_id = ?", (customer_id,)
    return [Subscription(r["subscription_id"], r["customer_id"], r["plan_id"], Decimal(r["fee"]),
                         Decimal(r["included_credit"]), r["term_months"], r["start_period"])
            for r in conn.execute(sql + " ORDER BY start_period, subscription_id", args)]


def grants(conn: sqlite3.Connection, customer_id: str | None = None) -> list[CreditGrant]:
    sql = "SELECT * FROM credit_grants"
    args: tuple = ()
    if customer_id is not None:
        sql, args = sql + " WHERE customer_id = ?", (customer_id,)
    return [CreditGrant(r["grant_id"], r["customer_id"], Decimal(r["amount"]),
                        datetime.fromisoformat(r["granted_at"]), r["first_period"],
                        r["last_period"])
            for r in conn.execute(sql + " ORDER BY granted_at, grant_id", args)]
