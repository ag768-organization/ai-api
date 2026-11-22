---
name: verify
description: Run the billing pipeline end to end (generator, ingest, aggregate, rate, invoice, close, reconcile, revrec) on a fresh database and report every check. Use before committing any change to billing/, scripts/ or price books, or when asked to verify, check the pipeline, or prove reconciliation is clean.
---

# /verify

Proves the billing pipeline still balances end to end. `pytest` checks each
piece; `/verify` runs the pieces together on generated usage and shows the
numbers a reviewer would look at. Both are needed before a billing commit.

## Steps

1. **Run it.** From the repo root:

   ```bash
   python scripts/verify.py
   ```

   Pass on any extra arguments the user gave (for example `-n 5000`,
   `--seed 7`). The run uses a fixed seed and fixed timestamps, so the same
   arguments always give the same numbers. It takes about a second.

2. **Show the output.** Paste the whole report in a code block, unedited:
   ingest counts per batch and for the replay, each period's state, price
   book and totals, credits, revenue, reconcile and the PASS/FAIL list.
   Do not summarize it away; the numbers are the evidence.

3. **Read the exit code.**
   - `0` and `VERIFY PASSED`: say so in one line.
   - `1` or `VERIFY FAILED`: **stop.** Lead the reply with
     `VERIFY FAILED`, list every `FAIL` line and every `GAP` line, and do not
     commit, push or describe the change as working. A gap is a failure,
     never a warning, and never something to explain away. Investigate the
     cause, or hand it back to the user. Never edit `scripts/verify.py`,
     `billing/reconcile.py` or a test to make a failing run pass.

4. **Prove the control can fail** when the change touched `reconcile.py`,
   `scripts/verify.py` or this skill:

   ```bash
   python scripts/verify.py --inject-gap   # must exit 1 with GAP ingest_balance
   ```

   If it exits 0, the check is broken: report that as a failure.

5. **Walk the reliability checklist.** Open `RELIABILITY.md`, read the
   current diff (`git diff main...HEAD` plus uncommitted changes), and for
   each item whose trigger matches the diff, say in one line whether it holds
   and how you checked. Skip items that don't apply, but say which ones you
   skipped.

6. **Stamp.** If the user commits after this, the commit message says what
   ran (CLAUDE.md). From Phase 5 it ends with the `Verified:` stamp, and
   `verify` is only allowed when this run passed in the same session. Never
   claim verify ran if it didn't, or passed if it didn't.

## Options

- `-n N` clean events per batch (default 1000, minimum 100; 3 batches).
- `--seed S` reproduce another scenario.
- `--db path.db` keep the run's database to inspect (must not exist yet).
  Never point it at a `.db` file in the repo that you then commit.
- `--json` machine-readable report.
- `--inject-gap` negative control (see step 4).
