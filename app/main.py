"""FastAPI app: a fake model endpoint that emits usage, plus an ingest endpoint.

Run: uvicorn app.main:app --reload
DB path comes from AI_API_DB (default: usage.db).
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from billing import ledger
from billing.ingest import ingest

app = FastAPI(title="Fake AI API", version="0.1.0")


def get_conn() -> sqlite3.Connection:
    # One connection per request keeps SQLite thread-safety simple.
    conn = ledger.connect(os.environ.get("AI_API_DB", "usage.db"))
    try:
        yield conn
    finally:
        conn.close()


class CompletionRequest(BaseModel):
    customer_id: str
    model: str
    prompt: str = Field(min_length=1)


@app.post("/v1/complete")
def complete(req: CompletionRequest, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """Pretend to run a model. Token counts are a crude word count (GUESS:
    good enough for a fake). The usage event goes through the same ingest
    path as everything else, so there's one way into the ledger."""
    input_tokens = len(req.prompt.split())
    output_tokens = input_tokens * 2
    event = {
        "event_id": str(uuid.uuid4()),
        "customer_id": req.customer_id,
        "model": req.model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    result = ingest(conn, [event])
    if result.accepted != 1:
        raise HTTPException(status_code=422, detail=result.errors)
    return {
        "text": "lorem ipsum " * output_tokens,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        "event_id": event["event_id"],
    }


@app.post("/usage/events")
def ingest_events(events: list[dict[str, Any]], conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    r = ingest(conn, events)
    return {"received": r.received, "accepted": r.accepted, "duplicate": r.duplicate,
            "conflict": r.conflict, "invalid": r.invalid, "errors": r.errors}


@app.get("/ledger/summary")
def ledger_summary(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, int]:
    return {"events": ledger.count(conn), "total_tokens": ledger.total_tokens(conn)}
