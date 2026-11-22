import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_API_DB", str(tmp_path / "api.db"))
    return TestClient(app)


def test_complete_writes_one_ledger_event(client):
    r = client.post("/v1/complete", json={"customer_id": "cust_0001", "model": "tiny-1",
                                          "prompt": "hello there world"})
    assert r.status_code == 200
    assert r.json()["usage"] == {"input_tokens": 3, "output_tokens": 6}
    assert client.get("/ledger/summary").json() == {"events": 1, "total_tokens": 9}


def test_complete_rejects_unknown_model(client):
    r = client.post("/v1/complete", json={"customer_id": "cust_0001", "model": "nope", "prompt": "hi"})
    assert r.status_code == 422
    assert client.get("/ledger/summary").json()["events"] == 0


def test_ingest_endpoint_reports_buckets(client):
    from tests.test_events import valid
    r = client.post("/usage/events", json=[valid(), valid(), valid(model="nope")])
    body = r.json()
    assert (body["accepted"], body["duplicate"], body["invalid"]) == (1, 1, 1)
