#!/usr/bin/env python
"""Close a billing period: freeze its aggregates and invoices.

This is one-way. A closed period never reopens; events that arrive late for
it are billed in the next open period.

  python scripts/close_period.py --db usage.db --period 2026-10
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from billing import ledger  # noqa: E402
from billing.invoice import close_period  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", required=True)
    p.add_argument("--period", required=True, help="YYYY-MM (UTC calendar month)")
    a = p.parse_args(argv)
    conn = ledger.connect(a.db)
    invoices = close_period(conn, a.period)
    print(json.dumps({
        "period": a.period,
        "invoices": len(invoices),
        "total": str(sum((i.total for i in invoices), start=0)),
        "price_book_version": invoices[0].lines[0].price_book_version if invoices else None,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
