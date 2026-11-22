from datetime import datetime, timezone

from billing import emit

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_generator_is_deterministic():
    assert emit.generate(50, seed=42, dup_rate=0.1, now=NOW) == emit.generate(50, seed=42, dup_rate=0.1, now=NOW)


def test_clean_event_ids_unique():
    ids = [e["event_id"] for e in emit.generate(1_000, seed=0)]
    assert len(ids) == len(set(ids))


def test_fault_injection_sizes():
    out = emit.generate(100, seed=0, dup_rate=0.1, conflict_rate=0.05, invalid_rate=0.02)
    assert len(out) == 100 + 10 + 5 + 2
