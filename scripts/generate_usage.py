#!/usr/bin/env python
"""Generate fake usage and (optionally) ingest it into a ledger DB.

Examples:
  python scripts/generate_usage.py -n 1000 --out events.json
  python scripts/generate_usage.py -n 1000 --dup-rate 0.05 --conflict-rate 0.01 \
      --invalid-rate 0.02 --late-rate 0.03 --db usage.db
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from billing import emit, ledger  # noqa: E402
from billing.ingest import ingest  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-n", type=int, default=100, help="number of clean events")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--dup-rate", type=float, default=0.0)
    p.add_argument("--conflict-rate", type=float, default=0.0)
    p.add_argument("--invalid-rate", type=float, default=0.0)
    p.add_argument("--late-rate", type=float, default=0.0)
    p.add_argument("--cache-rate", type=float, default=0.0, help="share of events with cache tokens")
    p.add_argument("--batch-rate", type=float, default=0.0, help="share of events sent as batch")
    p.add_argument("--out", help="write raw events as JSON to this file")
    p.add_argument("--db", help="ingest into this SQLite ledger")
    a = p.parse_args(argv)

    events = emit.generate(a.n, seed=a.seed, dup_rate=a.dup_rate, conflict_rate=a.conflict_rate,
                           invalid_rate=a.invalid_rate, late_rate=a.late_rate,
                           cache_rate=a.cache_rate, batch_rate=a.batch_rate)
    if a.out:
        Path(a.out).write_text(json.dumps(events, indent=2))
        print(f"wrote {len(events)} raw events to {a.out}")
    if a.db:
        conn = ledger.connect(a.db)
        r = ingest(conn, events)
        print(json.dumps({"received": r.received, "accepted": r.accepted, "duplicate": r.duplicate,
                          "conflict": r.conflict, "invalid": r.invalid,
                          "ledger_events": ledger.count(conn),
                          "ledger_tokens": ledger.total_tokens(conn)}, indent=2))
    if not a.out and not a.db:
        json.dump(events, sys.stdout, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
