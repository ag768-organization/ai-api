"""Usage event schema and validation rules.

A usage event is the atomic unit of billing: one fake API call by one
customer against one model. Everything downstream (aggregate, rate,
invoice, reconcile) is derived from these events, so validation here is
strict on purpose.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from pydantic import (BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr,
                      field_validator)

# GUESS: a fixed model catalogue. Pricing per model arrives in Phase 2.
KNOWN_MODELS: frozenset[str] = frozenset({"tiny-1", "small-1", "medium-1", "large-1"})

# GUESS: reject events stamped more than 5 minutes in the future (clock skew
# allowance). Late events (in the past) are accepted; the late-arrival rule
# belongs to Aggregate in Phase 2.
MAX_FUTURE_SKEW = timedelta(minutes=5)

# GUESS: a single call can't exceed 2M tokens. Guards against garbage input.
MAX_TOKENS_PER_FIELD = 2_000_000

# Token fields on an event, in ledger column order. `input_tokens` is new
# (uncached) input; cache writes and cache reads are counted separately
# because they are priced as multiples of the input rate.
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens")

# Fields added after Phase 2, with the value an event that predates them is
# read as. `payload_hash` leaves a field out while it holds this value, so
# every event already in a ledger keeps its hash, and re-sending an old event
# without the new fields is still a duplicate, not a conflict.
NEW_FIELD_DEFAULTS = {"cache_write_tokens": 0, "cache_read_tokens": 0, "batch": False}


class UsageEvent(BaseModel):
    """One billable API call.

    GUESS: tokens are split into input and output because real per-token
    pricing almost always prices them differently. `total_tokens` is derived.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: StrictStr = Field(min_length=1)
    customer_id: StrictStr = Field(min_length=1, max_length=64, pattern=r"^cust_[A-Za-z0-9]+$")
    model: StrictStr
    input_tokens: StrictInt = Field(ge=0, le=MAX_TOKENS_PER_FIELD)
    output_tokens: StrictInt = Field(ge=0, le=MAX_TOKENS_PER_FIELD)
    # GUESS: one kind of cache write (no separate short and long TTL prices).
    cache_write_tokens: StrictInt = Field(default=0, ge=0, le=MAX_TOKENS_PER_FIELD)
    cache_read_tokens: StrictInt = Field(default=0, ge=0, le=MAX_TOKENS_PER_FIELD)
    # Sent through the batch interface: every token category gets the batch
    # discount, on top of any cache multiplier.
    batch: StrictBool = False
    occurred_at: datetime

    @field_validator("event_id")
    @classmethod
    def _event_id_is_uuid(cls, v: str) -> str:
        try:
            parsed = uuid.UUID(v)
        except ValueError as exc:
            raise ValueError("event_id must be a UUID") from exc
        # Normalise so "ABC..." and "abc..." can't sneak past de-duplication.
        return str(parsed)

    @field_validator("model")
    @classmethod
    def _model_is_known(cls, v: str) -> str:
        if v not in KNOWN_MODELS:
            raise ValueError(f"unknown model {v!r}")
        return v

    @field_validator("occurred_at", mode="before")
    @classmethod
    def _occurred_at_not_a_number(cls, v: object) -> object:
        # Pydantic would read an int as a Unix timestamp, so a stray -5 or 0
        # would become a 1969/1970 event. Require ISO-8601 text or a datetime.
        if not isinstance(v, (str, datetime)):
            raise ValueError("occurred_at must be an ISO-8601 string")
        return v

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_is_utc_and_not_future(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        v = v.astimezone(timezone.utc)
        if v > datetime.now(timezone.utc) + MAX_FUTURE_SKEW:
            raise ValueError("occurred_at is in the future")
        return v

    @property
    def total_tokens(self) -> int:
        return sum(getattr(self, f) for f in TOKEN_FIELDS)

    def payload_hash(self) -> str:
        """Stable hash of the billable content, used to tell an exact
        duplicate (safe to drop) from a conflicting re-use of an event_id.

        Fields in NEW_FIELD_DEFAULTS join the hash only when they differ from
        their default, so the hash of an event that doesn't use them is the
        same as it was before they existed (see tests/test_cache_batch.py)."""
        body: dict[str, object] = {
            "event_id": self.event_id,
            "customer_id": self.customer_id,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "occurred_at": self.occurred_at.isoformat(),
        }
        for name, default in NEW_FIELD_DEFAULTS.items():
            value = getattr(self, name)
            if value != default:
                body[name] = value
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
