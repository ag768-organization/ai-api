# Reliability checklist

Go through this before committing a change to `billing/`, `scripts/`, a
price book or `plans.json`. The `/verify` skill walks it against the diff
(step 5 of `.claude/skills/verify/SKILL.md`). Each item says when it applies,
what to check, where it came from, and which test guards it.

**Origin** is either a **bug** (it really happened in this repo and was
fixed) or a **guard** (a trap found while designing or reviewing a change,
blocked before it shipped). New items go at the bottom of their section,
with the PR that taught us the lesson. Don't delete an item because the code
"can't do that anymore": the test it points to is why.

## Always

- [ ] **`/verify` passes and `pytest -q` passes.** Paste the `VERIFY PASSED`
      line in the PR. A gap is a failure, never a warning.
- [ ] **Tests that use "now" pass a fixed time.** `ingest(received_at=)`,
      `close_period(closed_at=)`, `emit.generate(now=)`, `grant_credit(granted_at=)`.
      Otherwise the test changes with the calendar.
      *Origin: bug, Phase 1.*

## If you add or change an event field

- [ ] **Does any time field accept numbers?** Pydantic reads an int as Unix
      time, so `occurred_at: -5` became a 1969 event until we blocked it.
      Reject anything that isn't ISO-8601 text or a `datetime`.
      *Origin: bug, Phase 1. Test: `test_events.py::test_invalid_fields_rejected`.*
- [ ] **Does the field keep old events' hashes?** A new field joins
      `payload_hash` only when it differs from its default, or every event
      already in a ledger turns into a *conflict* when re-sent.
      *Origin: guard, PR #3. Test: `test_cache_batch.py::test_hash_of_an_old_event_is_unchanged`.*
- [ ] **Is the value normalised before de-duplication?** `event_id` is
      lower-cased through `uuid.UUID`, so `ABC...` and `abc...` can't both be
      accepted. Any new identity field needs the same.
      *Origin: guard, Phase 1. Test: `test_events.py::test_event_id_normalised_so_case_cannot_dodge_dedupe`.*

## If you write SQL or touch the schema

- [ ] **Any `INSERT OR REPLACE`, `REPLACE` or `UPSERT`?** SQLite skips delete
      triggers on REPLACE unless `PRAGMA recursive_triggers = ON` is set on the
      connection, so a REPLACE could silently overwrite a ledger row.
      The pragma is set in `ledger.connect`; new writes use plain `INSERT`
      (`ON CONFLICT(event_id) DO NOTHING` at most), never REPLACE.
      *Origin: bug, Phase 1. Test: `test_ledger.py::test_replace_cannot_overwrite`.*
- [ ] **New column on an existing table?** Add it in `ledger._migrate` with
      `ALTER TABLE ADD COLUMN` and a constant default. Never rebuild the
      table: dropping it drops its append-only triggers.
      *Origin: guard, PR #3. Test: `test_cache_batch.py::test_migration_is_idempotent_and_keeps_append_only`.*
- [ ] **New table holding billing results?** Give it the no-UPDATE and
      no-DELETE triggers and add it to the append-only tests.
      *Origin: guard, PR #2 and #4. Test: `test_invoice.py::test_billing_tables_are_append_only`.*

## If pricing or rating changed

- [ ] **Check rounding.** Money is `Decimal`, rounded once per line with
      `ROUND_HALF_UP`. No rounding between tiers, inside a line or on the
      invoice total. `$0.005` must give `0.01` (banker's rounding gives `0.00`),
      and two tiers of `$0.004` must give `0.01`, not `0.00`.
      *Origin: guard, PR #2. Tests: `test_rating.py::test_line_rounds_half_up_once`, `::test_no_rounding_between_tiers`.*
- [ ] **Any float?** Prices in a price book are strings. A float price is
      refused at load.
      *Origin: guard, PR #2. Test: `test_rating.py::test_float_price_rejected`.*
- [ ] **Does an older book refuse what it can't price?** v1 has no cache or
      batch prices; it must raise, not bill those tokens at zero.
      *Origin: guard, PR #3. Test: `test_cache_batch.py::test_v1_refuses_to_rate_cache_tokens`.*
- [ ] **Run the differential test** against the previous book and paste the
      summary in the PR: `python scripts/diff_pricing.py --old v1 --new <book>`.
      *Origin: CLAUDE.md rule.*

## If periods, closes or late events changed

- [ ] **An event received at the exact instant of a close counts as late.**
      Check the boundary with `<=`, not `<`.
      *Origin: guard, PR #2. Test: `test_aggregate.py::test_received_at_exact_close_instant_counts_as_late`.*
- [ ] **Can a close race an ingest batch?** `close_period` takes the write
      lock (`BEGIN IMMEDIATE`) before reading the ledger, and refuses a
      `closed_at` before any event already received.
      *Origin: guard, PR #2. Test: `test_invoice.py::test_cannot_backdate_close_past_received_events`.*

## If credits, subscriptions or revenue changed

- [ ] **Close periods out of order.** A grant must never be overdrawn
      whichever period closes first: open periods only draw what frozen draws
      left.
      *Origin: guard, PR #4. Test: `test_subscriptions.py::test_closing_out_of_order_never_overdraws_a_grant`.*
- [ ] **Can an expired grant still be drawn?** Once a grant's last period has
      closed, its leftover is breakage. An earlier period that closes later
      must not draw on it, or breakage already booked would change.
      *Origin: bug, found while building PR #5 (rev-rec) in PR #4's
      allocation; fixed in `credits.allocate`. Test:
      `test_revrec.py::test_expired_grant_is_not_drawn_by_an_earlier_open_period`.*

## If you add a check or control

- [ ] **Not `assert`.** Python skips `assert` under `python -O`, so a control
      written as one silently turns off. Raise an exception or record a gap.
      *Origin: guard, Phase 1 (`ingest.py`).*
- [ ] **Prove it can fail.** Add a test that tampers with the data and
      expects the gap. For `/verify` itself: `python scripts/verify.py --inject-gap`
      must exit 1.
      *Origin: guard, Phase 3. Test: `test_verify.py::test_injected_gap_fails_the_run`.*
