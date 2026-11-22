#!/usr/bin/env python
"""Differential pricing: rate the same generated usage with two price books
and print the change per customer. Use it when a price book changes, and
paste the summary into the PR.

  python scripts/diff_pricing.py --old v1 --new path/to/candidate.json -n 5000 --seed 1
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from billing import aggregate, emit, ledger, rating  # noqa: E402
from billing.ingest import ingest  # noqa: E402


def book(ref: str) -> rating.PriceBook:
    books = rating.price_books()
    return books[ref] if ref in books else rating.load_price_book(ref)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--old", required=True, help="version name or JSON path")
    p.add_argument("--new", required=True, help="version name or JSON path")
    p.add_argument("-n", type=int, default=5_000)
    p.add_argument("--seed", type=int, default=0)
    a = p.parse_args(argv)

    conn = ledger.connect(":memory:")
    ingest(conn, emit.generate(a.n, seed=a.seed))
    old, new = book(a.old), book(a.new)
    diffs = rating.diff_books(aggregate.aggregate(conn), old, new)
    old_total = sum((d.old_total for d in diffs), Decimal(0))
    new_total = sum((d.new_total for d in diffs), Decimal(0))
    print(json.dumps({
        "old": old.version, "new": new.version, "events": a.n, "seed": a.seed,
        "invoices": len(diffs), "changed": sum(1 for d in diffs if d.delta),
        "old_total": str(old_total), "new_total": str(new_total),
        "delta": str(new_total - old_total),
        "by_customer": [{"period": d.period, "customer_id": d.customer_id, "old": str(d.old_total),
                         "new": str(d.new_total), "delta": str(d.delta)} for d in diffs if d.delta],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
