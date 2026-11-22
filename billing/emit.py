"""Fake usage generator.

Produces realistic-looking raw usage events (as plain dicts, the way they'd
arrive over the wire). Optional knobs inject the faults ingest must handle:
exact duplicates, conflicting re-used IDs, invalid events and late arrivals.
Seeded, so a run is reproducible.
"""
from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from billing.events import KNOWN_MODELS

DEFAULT_CUSTOMERS = [f"cust_{n:04d}" for n in range(1, 11)]


def make_event(rng: random.Random, now: datetime, customers: list[str]) -> dict[str, Any]:
    model = rng.choice(sorted(KNOWN_MODELS))
    return {
        "event_id": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
        "customer_id": rng.choice(customers),
        "model": model,
        "input_tokens": rng.randint(1, 8_000),
        "output_tokens": rng.randint(0, 4_000),
        "occurred_at": (now - timedelta(seconds=rng.randint(0, 3_600))).isoformat(),
    }


def generate(
    n: int,
    *,
    seed: int = 0,
    dup_rate: float = 0.0,
    conflict_rate: float = 0.0,
    invalid_rate: float = 0.0,
    late_rate: float = 0.0,
    cache_rate: float = 0.0,
    batch_rate: float = 0.0,
    now: datetime | None = None,
    customers: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return `n` clean events plus injected faults appended in random order.

    Rates are fractions of `n`. Late events are clean events stamped 1–40
    days in the past. Cache events get cache write and read tokens; batch
    events are flagged `batch`. Both default to 0, and the generator draws
    nothing extra for a zero rate, so older seeds give the same output.
    """
    rng = random.Random(seed)
    now = now or datetime.now(timezone.utc)
    customers = customers or DEFAULT_CUSTOMERS

    events = [make_event(rng, now, customers) for _ in range(n)]

    for e in rng.sample(events, int(n * late_rate)):
        e["occurred_at"] = (now - timedelta(days=rng.randint(1, 40))).isoformat()
    if cache_rate:
        for e in rng.sample(events, int(n * cache_rate)):
            e["cache_write_tokens"] = rng.randint(0, 20_000)
            e["cache_read_tokens"] = rng.randint(0, 200_000)
    if batch_rate:
        for e in rng.sample(events, int(n * batch_rate)):
            e["batch"] = True

    extras: list[dict[str, Any]] = []
    for e in rng.choices(events, k=int(n * dup_rate)) if events else []:
        extras.append(dict(e))  # exact resend
    for e in rng.choices(events, k=int(n * conflict_rate)) if events else []:
        bad = dict(e)
        bad["output_tokens"] = e["output_tokens"] + rng.randint(1, 500)
        extras.append(bad)
    for _ in range(int(n * invalid_rate)):
        bad = make_event(rng, now, customers)
        bad[rng.choice(["model", "input_tokens", "occurred_at"])] = rng.choice(
            ["not-a-model", -5, "2026-01-01T00:00:00"]  # unknown model / negative / naive ts
        )
        extras.append(bad)

    out = events + extras
    rng.shuffle(out)
    return out
