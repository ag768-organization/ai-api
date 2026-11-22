"""Shared helpers for the Phase 2 billing tests."""
from __future__ import annotations

from datetime import datetime, timezone

from billing.events import UsageEvent
from tests.test_events import valid

UTC = timezone.utc


def at(text: str) -> datetime:
    """'2026-10-15' or '2026-10-15T12:00' as an aware UTC datetime."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def event(n: int, **over) -> dict:
    """A valid raw event with a distinct event_id per n."""
    return valid(event_id=f"00000000-0000-4000-8000-{n:012d}", **over)


def parsed(n: int, **over) -> UsageEvent:
    return UsageEvent.model_validate(event(n, **over))
