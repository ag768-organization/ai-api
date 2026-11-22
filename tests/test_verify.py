"""The /verify runner (scripts/verify.py): passes on clean generator output,
fails loudly on any gap, and is reproducible."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "verify.py"
_spec = importlib.util.spec_from_file_location("verify_script", _PATH)
verify = importlib.util.module_from_spec(_spec)
sys.modules["verify_script"] = verify  # dataclasses look the module up
_spec.loader.exec_module(verify)


def test_clean_run_passes_every_check():
    res = verify.run(n=200, seed=3)
    assert res.ok, [c for c in res.checks if not c.ok]
    assert res.reconcile["gaps"] == []
    assert res.reconcile["checked"]["closed_periods"] == 3
    # the run really exercised late events, credits and replay
    assert sum(p["late"] for p in res.periods) > 0
    assert res.batches[-1]["batch"] == "replay" and res.batches[-1]["accepted"] == 0


def test_same_seed_same_numbers():
    a, b = verify.run(n=150, seed=5), verify.run(n=150, seed=5)
    assert (a.batches, a.periods, a.credits, a.revenue) == (b.batches, b.periods,
                                                            b.credits, b.revenue)


def test_injected_gap_fails_the_run():
    res = verify.run(n=150, seed=0, inject_gap=True)
    assert not res.ok
    assert [g["check"] for g in res.reconcile["gaps"]] == ["ingest_balance"]
    assert "VERIFY FAILED" in verify.render(res)


def test_main_exit_codes(capsys):
    assert verify.main(["-n", "120"]) == 0
    assert "VERIFY PASSED" in capsys.readouterr().out
    assert verify.main(["-n", "120", "--inject-gap"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  reconcile: zero gaps" in out and "GAP ingest_balance" in out


def test_json_report(capsys):
    assert verify.main(["-n", "120", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is True and all(c["ok"] for c in body["checks"])


def test_refuses_existing_db(tmp_path):
    db = tmp_path / "old.db"
    db.write_text("")
    with pytest.raises(SystemExit):
        verify.main(["--db", str(db)])


def test_writes_fresh_db_when_asked(tmp_path):
    db = tmp_path / "verify.db"
    assert verify.main(["-n", "120", "--db", str(db)]) == 0
    assert db.exists()


def test_pipeline_crash_is_a_failure(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("simulated")
    monkeypatch.setattr(verify, "reconcile", boom)
    assert verify.main(["-n", "120"]) == 1
    assert "VERIFY FAILED: pipeline raised RuntimeError" in capsys.readouterr().err
