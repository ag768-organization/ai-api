from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from billing.events import UsageEvent


def valid(**over):
    base = {
        "event_id": "6f1c2b0e-7a4d-4e8b-9f3a-0c5d2e1b4a77",
        "customer_id": "cust_0001",
        "model": "small-1",
        "input_tokens": 100,
        "output_tokens": 50,
        "occurred_at": "2026-11-01T12:00:00+00:00",
    }
    base.update(over)
    return base


def test_valid_event_parses():
    e = UsageEvent.model_validate(valid())
    assert e.total_tokens == 150
    assert e.occurred_at.tzinfo == timezone.utc


@pytest.mark.parametrize(
    "field,value",
    [
        ("event_id", "not-a-uuid"),
        ("customer_id", ""),
        ("customer_id", "acme"),
        ("model", "gpt-9000"),
        ("input_tokens", -1),
        ("output_tokens", 2_000_001),
        ("input_tokens", 1.5),       # floats never allowed
        ("input_tokens", "100"),     # no string coercion
        ("input_tokens", True),      # bool is not an int here
        ("occurred_at", "2026-11-01T12:00:00"),  # naive timestamp
        ("occurred_at", "garbage"),
        ("occurred_at", -5),         # ints would be read as Unix time
        ("occurred_at", 1_700_000_000),
    ],
)
def test_invalid_fields_rejected(field, value):
    with pytest.raises(ValidationError):
        UsageEvent.model_validate(valid(**{field: value}))


def test_future_timestamp_rejected():
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    with pytest.raises(ValidationError):
        UsageEvent.model_validate(valid(occurred_at=future))


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        UsageEvent.model_validate(valid(discount=0.5))


def test_event_id_normalised_so_case_cannot_dodge_dedupe():
    a = UsageEvent.model_validate(valid(event_id="6F1C2B0E-7A4D-4E8B-9F3A-0C5D2E1B4A77"))
    b = UsageEvent.model_validate(valid())
    assert a.event_id == b.event_id
    assert a.payload_hash() == b.payload_hash()


def test_non_utc_offset_converted_to_utc():
    e = UsageEvent.model_validate(valid(occurred_at="2026-11-01T07:00:00-05:00"))
    assert e.occurred_at == datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
