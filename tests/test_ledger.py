import sqlite3

import pytest

from billing import ledger
from billing.events import UsageEvent
from tests.test_events import valid


def test_append_and_count(conn):
    assert ledger.append(conn, UsageEvent.model_validate(valid()))
    assert ledger.count(conn) == 1
    assert ledger.total_tokens(conn) == 150


def test_same_event_id_not_inserted_twice(conn):
    e = UsageEvent.model_validate(valid())
    assert ledger.append(conn, e) is True
    assert ledger.append(conn, e) is False
    assert ledger.count(conn) == 1


def test_update_is_blocked(conn):
    ledger.append(conn, UsageEvent.model_validate(valid()))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE usage_events SET input_tokens = 0")


def test_delete_is_blocked(conn):
    ledger.append(conn, UsageEvent.model_validate(valid()))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM usage_events")


def test_replace_cannot_overwrite(conn):
    """INSERT OR REPLACE deletes then inserts; the delete trigger must stop it."""
    ledger.append(conn, UsageEvent.model_validate(valid()))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT OR REPLACE INTO usage_events (seq, event_id, customer_id, model, input_tokens,"
            " output_tokens, occurred_at, received_at, payload_hash)"
            " SELECT seq, event_id, customer_id, model, 0, 0, occurred_at, received_at, payload_hash"
            " FROM usage_events"
        )
    assert ledger.total_tokens(conn) == 150


def test_schema_is_idempotent(tmp_path):
    db = tmp_path / "l.db"
    c1 = ledger.connect(db)
    ledger.append(c1, UsageEvent.model_validate(valid()))
    c1.commit()
    c1.close()
    c2 = ledger.connect(db)  # re-running the schema must not wipe or fail
    assert ledger.count(c2) == 1
