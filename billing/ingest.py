"""Ingest: validate raw events and write them to the ledger exactly once.

Every input lands in exactly one bucket, so reconciliation can prove
`received == accepted + duplicate + conflict + invalid`:

- accepted   new event_id, valid, written to the ledger
- duplicate  same event_id and identical payload as a ledger row; dropped
- conflict   same event_id but a different payload; rejected, never overwritten
- invalid    failed schema validation; rejected

Each batch's counts are also written to `ingest_batches` so the daily
reconciliation can check them later.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from pydantic import ValidationError

from billing import ledger
from billing.events import UsageEvent


@dataclass
class IngestResult:
    received: int = 0
    accepted: int = 0
    duplicate: int = 0
    conflict: int = 0
    invalid: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)

    def balances(self) -> bool:
        return self.received == self.accepted + self.duplicate + self.conflict + self.invalid


def ingest(
    conn: sqlite3.Connection,
    raw_events: Iterable[dict[str, Any]],
    *,
    received_at: datetime | None = None,
) -> IngestResult:
    """Validate and append a batch. The whole batch commits atomically, so a
    crash part-way through can't leave a half-written batch behind.

    Every event in a batch gets the same `received_at` (default: now). Tests
    pass it explicitly to place a batch before or after a period close."""
    received_at = (received_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    result = IngestResult()
    with conn:  # one transaction per batch
        for index, raw in enumerate(raw_events):
            result.received += 1
            try:
                event = UsageEvent.model_validate(raw)
            except ValidationError as exc:
                result.invalid += 1
                result.errors.append(
                    {"index": index, "kind": "invalid",
                     "event_id": raw.get("event_id") if isinstance(raw, dict) else None,
                     "detail": exc.errors(include_url=False, include_context=False)}
                )
                continue

            if ledger.append(conn, event, received_at=received_at):
                result.accepted += 1
                continue

            if ledger.get_payload_hash(conn, event.event_id) == event.payload_hash():
                result.duplicate += 1
            else:
                result.conflict += 1
                result.errors.append(
                    {"index": index, "kind": "conflict", "event_id": event.event_id,
                     "detail": "event_id already recorded with a different payload"}
                )
        if not result.balances():  # not `assert`: must survive python -O
            raise RuntimeError(f"ingest counts do not balance: {result}")
        # Recorded in the same transaction as the events it counts.
        conn.execute(
            "INSERT INTO ingest_batches (received_at, received, accepted, duplicate,"
            " conflict, invalid) VALUES (?, ?, ?, ?, ?, ?)",
            (received_at.isoformat(), result.received, result.accepted,
             result.duplicate, result.conflict, result.invalid),
        )
    return result
