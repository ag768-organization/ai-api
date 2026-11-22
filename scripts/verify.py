#!/usr/bin/env python
"""End-to-end verify: generator -> ingest -> aggregate -> rate -> invoice ->
close -> reconcile -> revrec, on a fresh database, with a fixed timeline.

It exits 0 only if every check passes, and 1 on any gap. There are no
warnings. This is what the `/verify` skill runs.

The run is reproducible: the seed and every timestamp are fixed, so the same
arguments give the same numbers on any machine (`now` never comes from the
clock). The timeline covers three months:

  2026-09  plans and a prepaid grant set up; batch A ingested on 09-20
  2026-08, 2026-09  closed on 10-01 (August is empty apart from late events)
  2026-10  batch B ingested on 10-20, with late events for closed September
  2026-10  closed on 11-01
  2026-11  a second grant; batch C on 11-20 with cache and batch tokens (v2)
           and late events for closed October. November stays open (draft).
  replay   every batch is sent again on 11-21: nothing new may be accepted
           and no total may move.

Besides `reconcile`, verify checks what reconcile can't see on its own: that
ingest accounted for every event the generator produced, that replay changed
nothing, that late events really were shifted, and that the revenue
rollforward balances.

  python scripts/verify.py                    # default run
  python scripts/verify.py -n 3000 --seed 7   # bigger, another seed
  python scripts/verify.py --db verify.db     # keep the database to inspect
  python scripts/verify.py --inject-gap       # prove a gap fails the run
  python scripts/verify.py --json             # machine-readable report
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from billing import aggregate, emit, invoice, ledger, revrec, subscriptions  # noqa: E402
from billing.ingest import IngestResult, ingest  # noqa: E402
from billing.reconcile import reconcile  # noqa: E402

UTC = timezone.utc
ZERO = Decimal("0.00")


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# Fault rates for each generated batch. Fixed so the expected ingest counts
# are known up front.
DUP_RATE, CONFLICT_RATE, INVALID_RATE, LATE_RATE = 0.05, 0.01, 0.02, 0.05
CACHE_RATE, BATCH_RATE = 0.20, 0.10   # batch C only: v2 prices them, v1 refuses

# (name, generated at, received at, cache/batch on?)
BATCHES = [
    ("A", at("2026-09-20T12:00"), at("2026-09-20T12:30"), False),
    ("B", at("2026-10-20T12:00"), at("2026-10-20T12:30"), False),
    ("C", at("2026-11-20T12:00"), at("2026-11-20T12:30"), True),
]
REPLAY_AT = at("2026-11-21T09:00")
THROUGH = "2026-11"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Result:
    seed: int
    n: int
    batches: list[dict[str, Any]] = field(default_factory=list)
    periods: list[dict[str, Any]] = field(default_factory=list)
    credits: dict[str, str] = field(default_factory=dict)
    revenue: dict[str, Any] = field(default_factory=dict)
    reconcile: dict[str, Any] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def check(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append(Check(name, bool(ok), detail))


def _expected(n: int) -> dict[str, int]:
    """What the generator injects for `n` clean events (see emit.generate)."""
    return {"clean": n, "dup_or_conflict": int(n * DUP_RATE) + int(n * CONFLICT_RATE),
            "invalid": int(n * INVALID_RATE), "late": int(n * LATE_RATE)}


def _setup_accounts(conn) -> None:
    """Plans and credit for a few customers, so fees, included credit,
    prepaid draws, revrec and breakage logic all run."""
    t = at("2026-09-01T00:00")
    subscriptions.subscribe(conn, "cust_0001", "plus_annual", "2026-09",
                            created_at=t, subscription_id="sub_verify_0001")
    subscriptions.subscribe(conn, "cust_0002", "plus", "2026-09",
                            created_at=t, subscription_id="sub_verify_0002a")
    subscriptions.subscribe(conn, "cust_0002", "team", "2026-10",
                            created_at=t, subscription_id="sub_verify_0002b")
    subscriptions.open_account(conn, "cust_0003", "prepaid", opened_at=t)
    subscriptions.grant_credit(conn, "cust_0003", "50.00", granted_at=at("2026-09-02T00:00"),
                               grant_id="grant_verify_0003")


def _totals(conn) -> dict[tuple[str, str], Decimal]:
    """Every invoice total: frozen for closed periods, draft for open ones."""
    closed = aggregate.closes(conn)
    periods = {u.period for u in aggregate.aggregate(conn)} | set(closed)
    out = {}
    for p in sorted(periods):
        invs = invoice.stored(conn, p) if p in closed else invoice.draft(conn, p)
        for inv in invs:
            out[(p, inv.customer_id)] = inv.total
    return out


def _batch_row(name: str, r: IngestResult, received_at: datetime) -> dict[str, Any]:
    return {"batch": name, "received_at": received_at.isoformat(), "received": r.received,
            "accepted": r.accepted, "duplicate": r.duplicate, "conflict": r.conflict,
            "invalid": r.invalid}


def run(n: int = 1000, seed: int = 0, db: str = ":memory:",
        inject_gap: bool = False) -> Result:
    """Run the whole pipeline once. `n` is the clean events per batch."""
    res = Result(seed=seed, n=n)
    conn = ledger.connect(db)
    exp = _expected(n)
    sent: list[list[dict[str, Any]]] = []

    _setup_accounts(conn)
    for i, (name, now, received_at, modern) in enumerate(BATCHES):
        if name == "B":
            invoice.close_period(conn, "2026-08", closed_at=at("2026-10-01T00:10"))
            invoice.close_period(conn, "2026-09", closed_at=at("2026-10-01T00:10"))
        if name == "C":
            invoice.close_period(conn, "2026-10", closed_at=at("2026-11-01T00:10"))
            subscriptions.grant_credit(conn, "cust_0004", "20.00",
                                       granted_at=at("2026-11-02T00:00"),
                                       grant_id="grant_verify_0004")
        events = emit.generate(
            n, seed=seed * 1000 + i, now=now, dup_rate=DUP_RATE, conflict_rate=CONFLICT_RATE,
            invalid_rate=INVALID_RATE, late_rate=LATE_RATE,
            cache_rate=CACHE_RATE if modern else 0.0, batch_rate=BATCH_RATE if modern else 0.0)
        sent.append(events)
        r = ingest(conn, events, received_at=received_at)
        res.batches.append(_batch_row(name, r, received_at))
        # Completeness against the generator, not against ingest's own counts
        res.check(f"batch {name}: every input counted",
                  r.received == len(events) and r.balances(),
                  f"generated {len(events)}, received {r.received} = accepted {r.accepted} "
                  f"+ duplicate {r.duplicate} + conflict {r.conflict} + invalid {r.invalid}")
        res.check(f"batch {name}: expected buckets",
                  (r.accepted, r.duplicate + r.conflict, r.invalid)
                  == (exp["clean"], exp["dup_or_conflict"], exp["invalid"]),
                  f"accepted {r.accepted}/{exp['clean']}, duplicate+conflict "
                  f"{r.duplicate + r.conflict}/{exp['dup_or_conflict']}, "
                  f"invalid {r.invalid}/{exp['invalid']}")

    # Replay every batch (invariant 7)
    before = _totals(conn)
    ledger_before = (ledger.count(conn), ledger.total_tokens(conn))
    replay = IngestResult()
    for events in sent:
        r = ingest(conn, events, received_at=REPLAY_AT)
        for k in ("received", "accepted", "duplicate", "conflict", "invalid"):
            setattr(replay, k, getattr(replay, k) + getattr(r, k))
    res.batches.append(_batch_row("replay", replay, REPLAY_AT))
    after = _totals(conn)
    res.check("replay: nothing new accepted", replay.accepted == 0,
              f"{replay.accepted} of {replay.received} re-sent events accepted")
    res.check("replay: totals unchanged",
              before == after and ledger_before == (ledger.count(conn), ledger.total_tokens(conn)),
              f"{len(before)} invoice totals and ledger "
              f"{ledger_before[0]} events / {ledger_before[1]} tokens compared")

    # Periods
    closed = aggregate.closes(conn)
    usage = aggregate.aggregate(conn)
    by_period: dict[str, list[aggregate.Usage]] = defaultdict(list)
    for u in usage:
        by_period[u.period].append(u)
    for p in sorted(set(by_period) | set(closed)):
        is_closed = p in closed
        invs = invoice.stored(conn, p) if is_closed else invoice.draft(conn, p)
        books = {ln.price_book_version for inv in invs for ln in inv.lines}
        rows = by_period.get(p, [])
        res.periods.append({
            "period": p, "state": "closed" if is_closed else "open (draft)",
            "price_book": ",".join(sorted(books)) or "-",
            "events": sum(u.event_count for u in rows),
            "late": sum(u.late_count for u in rows),
            "tokens": sum(u.total_tokens for u in rows),
            "invoices": len(invs),
            "usage": str(sum((inv.usage_total for inv in invs), ZERO)),
            "charges": str(sum((c.amount for inv in invs for c in inv.charges), ZERO)),
            "total": str(sum((inv.total for inv in invs), ZERO)),
        })

    # Late events: none may be billed in a period that was already closed
    # when it arrived, and the run must actually contain some.
    received = {r["event_id"]: datetime.fromisoformat(r["received_at"])
                for r in conn.execute("SELECT event_id, received_at FROM usage_events")}
    assigned = aggregate.assign(conn)
    into_closed = [e.event_id for e in assigned
                   if e.period in closed and closed[e.period] <= received[e.event_id]]
    late = sum(e.late for e in assigned)
    res.check("late events shifted", late > 0 and not into_closed,
              f"{late} late events billed in a later open period, "
              f"{len(into_closed)} billed into an already-closed period")
    res.check("every ledger event in one period",
              sum(u.event_count for u in usage) == ledger.count(conn),
              f"aggregated {sum(u.event_count for u in usage)}, ledger {ledger.count(conn)}")

    # Credits and revenue
    sched = revrec.schedule(conn)
    kinds = Counter()
    for c in sched.alloc.charges.values():
        for ch in c:
            kinds[ch.kind] += ch.amount
    res.credits = {k: str(kinds.get(k, ZERO)) for k in
                   ("subscription_fee", "included_credit", "prepaid_credit")}
    rf = revrec.rollforward(conn, THROUGH, sched)
    res.revenue = {
        "through": THROUGH,
        "by_kind": {k: str(sched.recognized(THROUGH, k)) for k in revrec.KINDS},
        "billed": str(rf.billed), "prepaid_cash": str(rf.prepaid_cash),
        "recognized": str(rf.recognized),
        "deferred": str(rf.deferred_subscription + rf.deferred_prepaid),
    }
    res.check("revenue rollforward balances", rf.balances,
              f"billed {rf.billed} + prepaid cash {rf.prepaid_cash} = recognized "
              f"{rf.recognized} + deferred {rf.deferred_subscription + rf.deferred_prepaid}")
    res.check("credits exercised", kinds["included_credit"] < 0 and kinds["prepaid_credit"] < 0,
              f"included {kinds['included_credit']}, prepaid {kinds['prepaid_credit']}")

    if inject_gap:
        # A batch whose counts don't add up: reconcile must catch it.
        conn.execute("INSERT INTO ingest_batches (received_at, received, accepted, duplicate,"
                     " conflict, invalid) VALUES (?, 1, 0, 0, 0, 0)", (REPLAY_AT.isoformat(),))
        conn.commit()

    report = reconcile(conn)
    res.reconcile = {"ok": report.ok, "checked": report.checked,
                     "gaps": [asdict(g) for g in report.gaps]}
    res.check("reconcile: zero gaps", report.ok, f"{len(report.gaps)} gaps")
    conn.close()
    return res


# ---------------------------------------------------------------- output

def _table(rows: list[dict[str, Any]], cols: list[str]) -> str:
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    out = ["  " + "  ".join(c.ljust(widths[c]) for c in cols),
           "  " + "  ".join("-" * widths[c] for c in cols)]
    for r in rows:
        out.append("  " + "  ".join(
            str(r[c]).rjust(widths[c]) if isinstance(r[c], int) or str(r[c])[:1].isdigit()
            or str(r[c])[:1] == "-" else str(r[c]).ljust(widths[c]) for c in cols))
    return "\n".join(out)


def render(res: Result) -> str:
    out = [f"VERIFY  seed={res.seed}  n={res.n} clean events per batch", ""]
    out += ["1. Generate + ingest", _table(res.batches, ["batch", "received_at", "received",
            "accepted", "duplicate", "conflict", "invalid"]), ""]
    out += ["2. Aggregate, rate, invoice", _table(res.periods, ["period", "state", "price_book",
            "events", "late", "tokens", "invoices", "usage", "charges", "total"]), ""]
    out += ["3. Credits (all periods)"] + [f"  {k:<17} {v:>12}" for k, v in res.credits.items()]
    out += ["", f"4. Revenue through {res.revenue['through']}"]
    out += [f"  {k:<17} {v:>12}" for k, v in res.revenue["by_kind"].items()]
    out += [f"  {k:<17} {res.revenue[k]:>12}" for k in
            ("billed", "prepaid_cash", "recognized", "deferred")]
    checked = ", ".join(f"{k} {v}" for k, v in res.reconcile["checked"].items())
    out += ["", "5. Reconcile", f"  checked: {checked}"]
    for g in res.reconcile["gaps"]:
        out.append(f"  GAP {g['check']} period={g['period']} customer={g['customer_id']}: "
                   f"{g['detail']}")
    out += ["", "6. Checks"]
    for c in res.checks:
        out.append(f"  {'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}")
    failed = [c for c in res.checks if not c.ok]
    bar = "=" * 72
    out += ["", bar]
    if failed:
        out.append(f"VERIFY FAILED: {len(failed)} of {len(res.checks)} checks failed, "
                   f"{len(res.reconcile['gaps'])} reconcile gaps")
    else:
        out.append(f"VERIFY PASSED: {len(res.checks)} checks, 0 reconcile gaps")
    out.append(bar)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-n", type=int, default=1000, help="clean events per batch (3 batches)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--db", default=":memory:",
                   help="write the run to this new SQLite file (refuses an existing one)")
    p.add_argument("--inject-gap", action="store_true",
                   help="add an unbalanced ingest batch before reconcile; the run must fail")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    a = p.parse_args(argv)
    if a.n < 100:
        p.error("-n must be at least 100 so every fault type is injected")
    if a.db != ":memory:" and Path(a.db).exists():
        p.error(f"{a.db} already exists; verify needs a fresh database")
    try:
        res = run(a.n, a.seed, a.db, a.inject_gap)
    except Exception as exc:  # a crash is a failure too, never a silent pass
        print(f"VERIFY FAILED: pipeline raised {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if a.json:
        body = asdict(res)
        body["ok"] = res.ok
        print(json.dumps(body, indent=2))
    else:
        print(render(res))
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
