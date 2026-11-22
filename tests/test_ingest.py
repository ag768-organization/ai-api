from billing import emit, ledger
from billing.ingest import ingest
from tests.test_events import valid


def test_clean_batch_all_accepted(conn):
    events = emit.generate(200, seed=1)
    r = ingest(conn, events)
    assert (r.received, r.accepted, r.duplicate, r.conflict, r.invalid) == (200, 200, 0, 0, 0)
    assert ledger.count(conn) == 200


def test_exact_duplicate_dropped(conn):
    r = ingest(conn, [valid(), valid()])
    assert (r.accepted, r.duplicate) == (1, 1)
    assert ledger.count(conn) == 1


def test_duplicate_across_batches_dropped(conn):
    ingest(conn, [valid()])
    r = ingest(conn, [valid()])
    assert (r.accepted, r.duplicate) == (0, 1)
    assert ledger.count(conn) == 1


def test_conflicting_reuse_of_event_id_rejected_not_overwritten(conn):
    ingest(conn, [valid()])
    r = ingest(conn, [valid(output_tokens=9_999)])
    assert (r.accepted, r.duplicate, r.conflict) == (0, 0, 1)
    assert r.errors[0]["kind"] == "conflict"
    assert ledger.total_tokens(conn) == 150  # original stands


def test_invalid_counted_not_silently_dropped(conn):
    r = ingest(conn, [valid(model="nope"), {"garbage": True}, valid()])
    assert (r.received, r.accepted, r.invalid) == (3, 1, 2)
    assert len(r.errors) == 2


def test_replay_does_not_change_ledger(conn):
    events = emit.generate(300, seed=7, dup_rate=0.1, invalid_rate=0.05)
    ingest(conn, events)
    before = (ledger.count(conn), ledger.total_tokens(conn))
    r = ingest(conn, events)
    assert r.accepted == 0
    assert (ledger.count(conn), ledger.total_tokens(conn)) == before


def test_counts_always_balance_with_faults(conn):
    events = emit.generate(500, seed=3, dup_rate=0.05, conflict_rate=0.02,
                           invalid_rate=0.03, late_rate=0.05)
    r = ingest(conn, events)
    assert r.balances()
    assert r.received == len(events)
    assert ledger.count(conn) == r.accepted
    # every clean event_id ends up in the ledger exactly once
    assert r.accepted == 500


def test_late_events_are_accepted(conn):
    r = ingest(conn, [valid(occurred_at="2026-01-01T00:00:00+00:00")])
    assert r.accepted == 1
