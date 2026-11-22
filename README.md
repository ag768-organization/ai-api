# ai-api

A test system for learning: a toy per-token "AI API" used to practice
change-management and billing controls. All data is synthetic. See
`CLAUDE.md` for invariants and commands.
Phase 1 covers **emit → ingest → ledger**. Phase 2 adds
**aggregate → rate → invoice → reconcile**.

## Reliability checklist (seeded from real Phase 1 bugs)

- [ ] **Timestamps:** does any new time field accept numbers? Pydantic reads
      ints as Unix time, so `-5` became a 1969 event until we blocked it.
- [ ] **Ledger writes:** does any new SQL use `INSERT OR REPLACE` / `REPLACE` /
      `UPSERT`? SQLite skips delete triggers on REPLACE unless
      `PRAGMA recursive_triggers = ON` is set on the connection.
- [ ] **Tests using "now":** pass a fixed `now`, or the test isn't deterministic.
