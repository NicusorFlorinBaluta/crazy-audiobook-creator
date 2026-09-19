"""Tests for shared/segment_repair.py."""

import json
import sqlite3
from pathlib import Path

import pytest

from shared.artifacts import fingerprint, hash_file
from shared.models import QualityResult
from shared.segment_repair import (
    backup_segment,
    parse_chapter_number,
    record_repaired_validation,
    replace_segment,
    rewrite_cache_fingerprint,
    rewrite_manifest_segment,
)


def test_parse_chapter_number():
    assert parse_chapter_number("c002_0001") == 2
    assert parse_chapter_number("ch01_0000") == 1
    assert parse_chapter_number("c29_0012") == 29
    assert parse_chapter_number("repair-c004_0007") == 4
    with pytest.raises(ValueError, match="Cannot extract chapter number"):
        parse_chapter_number("invalid_line_format")


def test_backup_segment(tmp_path: Path):
    seg = tmp_path / "c001_0001.wav"
    seg.write_bytes(b"initial-audio-content")

    backup = backup_segment(seg)
    assert backup is not None
    assert backup.is_file()
    assert backup.parent.name == "repair-backup"
    assert backup.read_bytes() == b"initial-audio-content"

    # Second call must not overwrite existing backup even if segment changes
    seg.write_bytes(b"mutated-content")
    backup2 = backup_segment(seg)
    assert backup2 == backup
    assert backup.read_bytes() == b"initial-audio-content"


def test_rewrite_manifest_segment(tmp_path: Path):
    manifest_path = tmp_path / "chapter_001.segments.json"
    initial_manifest = {
        "chapter_number": 1,
        "dependency_hash": "dep-12345",
        "segments": [
            {"line_id": "c001_0001", "output_hash": "old-hash-1"},
            {"line_id": "c001_0002", "output_hash": "hash-2"},
        ],
    }
    initial_manifest["manifest_hash"] = fingerprint({k: v for k, v in initial_manifest.items() if k != "manifest_hash"})
    manifest_path.write_text(json.dumps(initial_manifest), encoding="utf-8")

    # Update line 1
    assert rewrite_manifest_segment(manifest_path, "c001_0001", "new-hash-1") is True

    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert updated["segments"][0]["output_hash"] == "new-hash-1"
    assert updated["segments"][1]["output_hash"] == "hash-2"
    assert updated["dependency_hash"] == "dep-12345"  # untouched!
    expected_manifest_hash = fingerprint({k: v for k, v in updated.items() if k != "manifest_hash"})
    assert updated["manifest_hash"] == expected_manifest_hash

    # Missing line ID returns False
    assert rewrite_manifest_segment(manifest_path, "c001_9999", "another-hash") is False


def test_rewrite_cache_fingerprint(tmp_path: Path):
    cache_db = tmp_path / "voice_cache.db"
    with sqlite3.connect(cache_db) as conn:
        conn.execute(
            "create table generation_fingerprints ("
            "project_id text, line_id text, output_hash text, primary key (project_id, line_id))"
        )
        conn.execute("insert into generation_fingerprints values ('proj1', 'c001_0001', 'old-cache-hash')")

    assert rewrite_cache_fingerprint(cache_db, "proj1", "c001_0001", "new-cache-hash") is True
    assert rewrite_cache_fingerprint(cache_db, "proj1", "c001_9999", "new-cache-hash") is False

    with sqlite3.connect(cache_db) as conn:
        row = conn.execute(
            "select output_hash from generation_fingerprints where project_id='proj1' and line_id='c001_0001'"
        ).fetchone()
        assert row[0] == "new-cache-hash"


def test_record_repaired_validation(tmp_path: Path):
    state_db = tmp_path / "pipeline_state.db"
    with sqlite3.connect(state_db) as conn:
        conn.execute(
            "create table quality_logs ("
            "id integer primary key autoincrement, project_id text, line_id text, chapter_number integer, "
            "attempt integer, wer real, quality_score real, status text, details text, created_at text)"
        )

    val = QualityResult(
        line_id="c001_0001",
        wer=0.05,
        quality_score=0.95,
        status="pass",
        transcribed_text="The quick brown fox",
    )
    ok = record_repaired_validation(state_db, "proj1", "c001_0001", 1, val, repaired_by="test_runner")
    assert ok is True

    with sqlite3.connect(state_db) as conn:
        rows = conn.execute("select * from quality_logs where project_id='proj1'").fetchall()
        assert len(rows) == 1
        _, proj, line, ch, att, wer, score, status, details, created = rows[0]
        assert proj == "proj1"
        assert line == "c001_0001"
        assert ch == 1
        assert wer == 0.05
        assert score == 0.95
        assert status == "pass"
        details_obj = json.loads(details)
        assert details_obj.get("repaired_by") == "test_runner"
        assert details_obj.get("transcribed_text") == "The quick brown fox"


def test_replace_segment_end_to_end(tmp_path: Path):
    # Setup directories
    proj_dir = tmp_path / "brain" / "projects" / "test-book"
    manifests_dir = proj_dir / "manifests"
    manifests_dir.mkdir(parents=True)
    segments_dir = tmp_path / "workspace" / "test-book" / "segments"
    segments_dir.mkdir(parents=True)
    cache_db = tmp_path / "voice_cache.db"
    state_db = proj_dir / "pipeline_state.db"

    # Setup DBs
    with sqlite3.connect(cache_db) as conn:
        conn.execute("create table generation_fingerprints (project_id text, line_id text, output_hash text)")
        conn.execute("insert into generation_fingerprints values ('test-book', 'c002_0001', 'old-hash')")
    with sqlite3.connect(state_db) as conn:
        conn.execute(
            "create table quality_logs (id integer primary key autoincrement, project_id text, line_id text, "
            "chapter_number integer, attempt integer, wer real, quality_score real, status text, details text, created_at text)"
        )

    # Setup segment wav
    orig_wav = segments_dir / "c002_0001.wav"
    orig_wav.write_bytes(b"original-take-audio")

    # Setup candidate wav
    candidate_wav = tmp_path / "candidate_take.wav"
    candidate_wav.write_bytes(b"repaired-take-audio-content")
    candidate_hash = hash_file(candidate_wav)

    # Setup manifest
    manifest_path = manifests_dir / "chapter_002.segments.json"
    manifest_data = {
        "chapter_number": 2,
        "dependency_hash": "dep-keep",
        "segments": [{"line_id": "c002_0001", "output_hash": "old-hash"}],
    }
    manifest_data["manifest_hash"] = fingerprint({k: v for k, v in manifest_data.items() if k != "manifest_hash"})
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    val = QualityResult(
        line_id="c002_0001", wer=0.02, quality_score=0.98, status="pass", transcribed_text="Drizzt Do'Urden"
    )

    result = replace_segment(
        "test-book",
        "c002_0001",
        candidate_wav,
        val,
        project_dir=proj_dir,
        segments_dir=segments_dir,
        cache_db=cache_db,
        state_db=state_db,
    )

    assert result.success is True
    assert result.line_id == "c002_0001"
    assert result.chapter == 2
    assert result.new_hash == candidate_hash
    assert result.manifest_updated is True
    assert result.cache_updated is True
    assert result.quality_logged is True

    # 1. WAV replaced and backup exists
    assert orig_wav.read_bytes() == b"repaired-take-audio-content"
    backup = segments_dir / "repair-backup" / "c002_0001.wav"
    assert backup.is_file()
    assert backup.read_bytes() == b"original-take-audio"

    # 2. Manifest updated
    updated_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert updated_manifest["segments"][0]["output_hash"] == candidate_hash

    # 3. Cache updated
    with sqlite3.connect(cache_db) as conn:
        row = conn.execute("select output_hash from generation_fingerprints").fetchone()
        assert row[0] == candidate_hash

    # 4. Quality log written
    with sqlite3.connect(state_db) as conn:
        row = conn.execute("select status, details from quality_logs").fetchone()
        assert row[0] == "pass"
        assert "Drizzt Do'Urden" in row[1]
