#!/usr/bin/env python
"""Revenue recognition report: revenue per period by kind, and the deferred
revenue rollforward.

  python scripts/revrec.py --db usage.db --through 2026-11
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from billing import ledger, revrec  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", required=True)
    p.add_argument("--through", required=True, help="YYYY-MM, inclusive")
    a = p.parse_args(argv)
    conn = ledger.connect(a.db)
    sched = revrec.schedule(conn)
    rf = revrec.rollforward(conn, a.through, sched)
    print(json.dumps({
        "through": a.through,
        "by_period": {period: {k: str(v) for k, v in kinds.items()}
                      for period, kinds in sorted(revrec.by_period(sched).items())
                      if period <= a.through},
        "billed": str(rf.billed), "prepaid_cash": str(rf.prepaid_cash),
        "recognized": str(rf.recognized),
        "deferred_subscription": str(rf.deferred_subscription),
        "deferred_prepaid": str(rf.deferred_prepaid),
        "balances": rf.balances,
    }, indent=2))
    return 0 if rf.balances else 1


if __name__ == "__main__":
    raise SystemExit(main())
