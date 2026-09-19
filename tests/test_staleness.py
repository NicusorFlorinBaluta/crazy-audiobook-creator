"""Unit tests for shared/staleness.py."""

import json
import sqlite3
import time
from pathlib import Path

from shared.staleness import check_delivery_staleness, check_pronunciation_evidence_staleness


def test_check_delivery_staleness(tmp_path: Path):
    ws = tmp_path / "workspace"
    chapters = ws / "chapters"
    output = ws / "output"
    chapters.mkdir(parents=True)
    output.mkdir(parents=True)

    # Initially empty
    assert check_delivery_staleness(ws) == []

    # Create chapter 1 and 2
    ch1 = chapters / "chapter_001.wav"
    ch1.write_bytes(b"ch1")
    ch2 = chapters / "chapter_002.wav"
    ch2.write_bytes(b"ch2")

    # Wait 0.05s so delivery is newer
    time.sleep(0.05)
    delivery = output / "book_chapters_1-2.m4b"
    delivery.write_bytes(b"delivery")

    # Delivery is newer -> not stale
    assert check_delivery_staleness(ws) == []

    # Now touch chapter 2 so it's newer than delivery
    time.sleep(0.05)
    ch2.write_bytes(b"ch2-updated")

    stale = check_delivery_staleness(ws)
    assert len(stale) == 1
    assert stale[0]["artifact"] == "book_chapters_1-2.m4b"
    assert stale[0]["newer_chapters"] == [2]

    # Test with deliveries_index
    deliveries_index = [
        {
            "delivery_id": "part-001",
            "artifact": "book_chapters_1-2.m4b",
            "chapter_numbers": [1, 2],
        }
    ]
    stale_indexed = check_delivery_staleness(ws, deliveries_index)
    assert len(stale_indexed) == 1
    assert stale_indexed[0]["delivery_id"] == "part-001"
    assert stale_indexed[0]["newer_chapters"] == [2]


def test_check_pronunciation_evidence_staleness(tmp_path: Path):
    proj = tmp_path / "projects" / "test-book"
    proj.mkdir(parents=True)
    state_db = tmp_path / "pipeline_state.db"

    # Setup state_db
    with sqlite3.connect(state_db) as conn:
        conn.execute(
            "create table quality_logs ("
            "id integer primary key autoincrement, project_id text, line_id text, chapter_number integer, "
            "attempt integer, wer real, quality_score real, status text, details text, created_at text)"
        )
        conn.execute(
            "insert into quality_logs (project_id, line_id, created_at) "
            "values ('test-book', 'c001_0001', '2026-09-10T12:00:00+00:00')"
        )

    # Lexicon file modified after 2026-09-10
    lex_file = proj / "pronunciation_dict.json"
    lex_file.write_text(json.dumps({"Drizzt": "Drizt"}), encoding="utf-8")

    res = check_pronunciation_evidence_staleness(proj, state_db=state_db)
    # Since lex_file is created now (2026-09-16), it is newer than audio from 2026-09-10!
    assert res["evidence_current"] is False
    assert "lexicon changed" in res["evidence_freshness"]
    assert "newest audio is 2026-09-10T12:00" in res["evidence_freshness"]

    # Now simulate audio recorded today after the lexicon
    with sqlite3.connect(state_db) as conn:
        conn.execute(
            "insert into quality_logs (project_id, line_id, created_at) "
            "values ('test-book', 'c001_0002', '2099-01-01T00:00:00+00:00')"
        )

    res2 = check_pronunciation_evidence_staleness(proj, state_db=state_db)
    assert res2["evidence_current"] is True
    assert "newest audio 2099-01-01T00:00" in res2["evidence_freshness"]
