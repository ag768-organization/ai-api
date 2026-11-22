# ai-api

A test system for learning: a toy per-token "AI API" used to practice
change-management and billing controls. All data is synthetic. See
`CLAUDE.md` for invariants and commands.
Phase 1 covers **emit → ingest → ledger**. Phase 2 adds
**aggregate → rate → invoice → reconcile**.

Phase 3 adds the `/verify` skill (`.claude/skills/verify/`), which runs the
whole pipeline end to end with `python scripts/verify.py` and fails on any gap.

## Reliability checklist

Moved to [`RELIABILITY.md`](RELIABILITY.md), which now covers every bug and
trap found through Phase 2 and the subscriptions/rev-rec phase. `/verify`
walks it against the diff before each billing commit.
