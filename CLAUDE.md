# CLAUDE.md

Guidance for Claude Code working in this repository.

> **[GUESS]** marks an assumption nobody has confirmed yet. Where the code
> already decides a question, the code wins. Delete a marker once settled.

## What this is

A small, fake "AI API" that bills customers per token. The product is a
learning vehicle: the billing pipeline is treated as a high-risk,
SOX-relevant path (revenue completeness and accuracy), and every change
goes through a controlled change-management pipeline.

All data is synthetic. Do not add real customer data, real pricing from any
employer or client, or anything not drawn from public material.

## Stack

- Python 3.11+, FastAPI, Pydantic v2, SQLite (stdlib `sqlite3`), pytest.
- No ORM. SQL lives next to the code that uses it.
- pip + `pyproject.toml` (editable install with the `dev` extra).
- Hypothesis for property tests (dev extra).
- Planned, not yet installed: ruff (lint and format) and mypy (type checks).
  **[GUESS: ruff and mypy]**

## Layout

- `billing/` is the high-risk path (see "High-risk paths").
  - `events.py`  usage event schema and validation rules
  - `emit.py`    fake usage generator (seeded)
  - `ingest.py`  validation + de-duplication by `event_id`
  - `ledger.py`  append-only SQLite ledger of raw events, plus the
    append-only downstream tables (ingest batches, period closes, frozen
    aggregates, invoice lines, invoices)
  - `aggregate.py` billing-period assignment (incl. late events) and totals
  - `rating.py`  price book loading and rating; `diff_books` for differential runs
  - `subscriptions.py` accounts (postpaid / prepaid), plan terms and
    prepaid credit grants: append-only records, no billing logic
  - `credits.py` subscription fees, included credit and prepaid draw-down
    as invoice charges
  - `plans.json` subscription plans (data). **[GUESS: all plan numbers]**
  - `invoice.py` invoices, drafts, and `close_period` (the one-way freeze)
  - `revrec.py`  revenue recognition schedule and deferred revenue rollforward
  - `reconcile.py` the daily completeness and accuracy check
  - `price_books/*.json` versioned price books (data, not code). All numbers
    are synthetic. **[GUESS: v1 prices, tiers and free allowances; v2
    cache multipliers and batch multiplier]**
- `app/` is the FastAPI app (fake model endpoint + ingest endpoint).
- `scripts/` holds command-line helpers, e.g. `generate_usage.py`.
- `tests/` is the pytest suite, with `tests/property/` (Hypothesis,
  invariants 6 to 9) and `tests/differential/` (old vs new pricing).
- `.github/` is high risk: CI workflows and CODEOWNERS (Phase 4).
- `.claude/` holds skills (`verify`), hooks and settings (Phases 3 and 5).
- `REVIEW.md` holds review rules, read from `main`, never from the PR (Phase 4).

## Pipeline

Each step has one module and one job. A later step never reaches back and
changes an earlier step's data. Phase 1 built steps 1 to 3; Phase 2 builds 4
to 7.

1. **Emit.** Every API call produces a usage event: `event_id` (UUID),
   `customer_id` (`cust_...`), `model` (from a fixed catalogue),
   `input_tokens`, `output_tokens`, `occurred_at` (UTC), plus optional
   `cache_write_tokens`, `cache_read_tokens` (default 0) and `batch`
   (default false). `total_tokens` is derived. Token types are split because
   they are priced differently.
   - Fields added after Phase 2 join `payload_hash` only when they differ
     from their default, so an old event keeps its hash and re-sending it is
     still a duplicate. Any new event field must follow the same rule, and
     the ledger gets it through `ledger._migrate` (`ALTER TABLE ADD COLUMN`
     with a constant default), never by rebuilding the table.
2. **Ingest.** Validate (schema, non-negative integer tokens under a per-call
   cap, known model, aware UTC timestamp not in the future beyond a small
   skew), then de-duplicate by `event_id`. Report accepted / duplicate /
   conflict / invalid counts.
3. **Ledger.** Raw events go into the append-only `usage_events` table.
4. **Aggregate.** Total tokens per customer, per model, per billing period.
   - Billing period: calendar month, UTC. **[GUESS]**
   - Late events: an event whose `occurred_at` falls in a period that was
     already closed when it was *received* is billed in the next period that
     was open at that moment, and flagged late. Closed periods are never
     reopened. The ledger keeps the true `occurred_at` and `received_at`, so
     the shift is visible and auditable.
   - A period closes with `close_period` only after it has ended, and never
     with a `closed_at` before an event already received.
5. **Rate.** Apply price per million tokens per model (input and output
   separately), volume tiers and any free allowance. Every rated line records
   the price book version used.
   - Free allowance is per customer, model, token category and period, and
     comes off first; tier limits count tokens after it. **[GUESS]**
   - A period is rated on the book with the latest `effective_from` on or
     before it; no mid-period price changes. **[GUESS]**
   - From v2, cache writes and reads are the input rate times a per-model
     multiplier, and a batch call multiplies every category by one
     `batch_multiplier` (stacking with cache). Derived categories get no free
     allowance and count their own tiers. A book without `modifiers` (v1)
     refuses to rate cache or batch tokens rather than billing them at zero.
     **[GUESS]**
6. **Invoice.** Line items and a total per customer per period. The invoice
   total equals the sum of its usage lines and charges exactly.
   - Charges, in order: a subscription term's whole fee in its first period
     (billed in advance); then included credit for the active term, then
     prepaid credit (soonest-expiring grant first), both only against
     usage, never against the fee. What's left is overage at API rates.
     **[GUESS]**
   - Included credit expires at the end of each period. A prepaid grant is
     usable from the period it was bought in for 12 periods, then expires.
     **[GUESS]**
   - Closed periods' charges are frozen in `invoice_charges`. Open periods
     draw from what frozen draws left, in date order, so a grant can never
     be overdrawn whatever order periods close in.
   - A `prepaid` account is refused at the API (HTTP 402) once its credit
     is gone; nothing is emitted. Ingest still records every event that
     did happen. **[GUESS]**
7. **Reconcile.** A daily job checks that raw ledger units = aggregated units
   = invoiced units for each customer and period, and that ingest counts add
   up to inputs. It also checks credits and the revenue
   rollforward. Any gap is a failure, not a warning.
8. **Recognize.** `revrec.py` reads billing results and writes nothing.
   Usage billed at API rates is revenue in the period it is billed;
   subscription fees are recognized evenly over the term (share rounded down,
   remainder in the last period); prepaid credit is deferred until drawn,
   and what is left at expiry is breakage once the grant's last period has
   closed. Usage covered by included credit earns no revenue of its own.
   Billed + prepaid cash = recognized + deferred, through every period.
   **[GUESS]** on all of these, simplified from ASC 606.

## Billing invariants (never break these)

1. **Ledger is append-only.** No `UPDATE` or `DELETE` on `usage_events`, ever.
   SQLite triggers enforce this; do not drop or bypass them. Corrections are
   new events: a **reversal event** has its own new `event_id` and points
   back to the original through a `reverses_event_id` field. A reversal must
   reference an event that exists in the ledger, and an event can be reversed
   at most once. (Reversals arrive in a later phase.)
2. **Exactly-once billing per `event_id`.** Re-sending an identical event is a
   no-op. Re-sending the same `event_id` with a different payload is a
   *conflict*: reject it and surface it, never overwrite.
3. **Token counts are integers.** No floats anywhere in usage or money.
   Money is `Decimal` end to end, rounded to cents once per line item with
   `ROUND_HALF_UP`. Never round intermediate values, and never round the
   invoice total separately: it is the sum of already-rounded line items.
4. **Timestamps are timezone-aware UTC.** Naive datetimes are rejected.
5. **Every rejected event is counted, not silently dropped.** Ingest returns
   accepted / duplicate / conflict / invalid counts so reconciliation can
   account for every input.
6. **Invoice total == sum of rated usage** for the period.
7. **Replay never changes totals.** Replaying the same events, in any order
   and with any number of duplicates, leaves every total unchanged.
8. **Every ledger event lands in exactly one billing period.**
9. **Reconciliation reports zero gaps** on generator output.

## High-risk paths

`billing/` and `.github/` are high risk. From Phase 4, CODEOWNERS requires an
approving review from the reviewer account on any change there.

When a change touches either path:

- Say so at the top of the PR description and explain the billing impact.
- Add or update the test first; it must fail without the change.
- Run the full test suite (and `/verify` once it exists) before committing.
- If pricing or rating changed, run the differential test against the
  previous price book and put the summary in the PR.
- Do not weaken a CI check, CODEOWNERS rule, branch protection or hook to get
  a change through. Raise it instead.
- Do not add a dependency to `billing/` without saying why in the PR.
- Do not change `REVIEW.md` and billing code in the same PR.

## Commands

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q                                   # run tests
python scripts/generate_usage.py -n 1000 --dup-rate 0.05 --db usage.db
python scripts/close_period.py --db usage.db --period 2026-10   # one-way
python scripts/reconcile.py --db usage.db   # exits 1 on any gap
python scripts/revrec.py --db usage.db --through 2026-11   # revenue + deferred
python scripts/diff_pricing.py --old v1 --new candidate.json   # pricing PRs
uvicorn app.main:app --reload               # run the API
```

## Testing rules

- Every change ships with tests. A bug fix starts with a failing test that
  reproduces the bug.
- Do not edit, weaken, skip, xfail or delete an existing test to make a change
  pass; explain why the test is wrong instead.
- Tests use a fresh in-memory or temp SQLite database, never a real `.db`
  file in the repo.
- The generator takes a seed, so any failure can be reproduced.
- Property tests (Hypothesis) cover invariants 6 to 9; differential tests
  run old and new pricing on the same generated usage and compare totals per
  customer. Hypothesis tests build their own in-memory DB inside the test
  body, not via a fixture.

## Commits and PRs

- Never push to `main` directly. Work on a branch and open a PR.
- **Ask before pushing, opening PRs, or changing `.github/` and CODEOWNERS.**
- Run `pytest -q` before every commit and say in the commit message what ran.
  From Phase 5, each message ends with `Verified: tests | verify | none`.
  Never claim a check that did not run.
- Keep PRs small, one pipeline step per PR where possible.
