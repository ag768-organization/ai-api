#!/usr/bin/env python
"""Daily reconciliation. Exits 1 if there is any gap.

  python scripts/reconcile.py --db usage.db
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from billing import ledger  # noqa: E402
from billing.reconcile import reconcile  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", required=True)
    a = p.parse_args(argv)
    report = reconcile(ledger.connect(a.db))
    print(json.dumps({"ok": report.ok, "checked": report.checked,
                      "gaps": [asdict(g) for g in report.gaps]}, indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
