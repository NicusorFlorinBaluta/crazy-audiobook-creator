"""Unified segment replacement keeping audio, manifest, cache, and quality logs in sync.

When replacing an existing synthesized segment with a new take, four distinct stores
must be updated atomically and consistently:

1. Segment audio file: replaced on disk, with previous take backed up in
   `segments/repair-backup/`.
2. Manifest: `manifests/chapter_NNN.segments.json` must record the new `output_hash`
   and recompute `manifest_hash`. (`dependency_hash` is left alone so the pipeline
   does not re-derive the chapter).
3. Cache DB: `voice_cache.db` -> `generation_fingerprints.output_hash` must point to
   the new hash so future cache checks recognize the take.
4. Quality logs: `pipeline_state.db` -> `quality_logs` must record a new row carrying
   the repaired transcript, ensuring pronunciation measurement audits read the fresh take.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shared.artifacts import atomic_write_json, fingerprint, hash_file

logger = logging.getLogger(__name__)

_DEFAULT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CACHE_DB = _DEFAULT_ROOT / "voice_cache.db"
_DEFAULT_STATE_DB = _DEFAULT_ROOT / "brain" / "projects" / "pipeline_state.db"


@dataclass(frozen=True)
class SegmentRepairResult:
    """Outcome of a segment replacement."""

    success: bool
    line_id: str
    chapter: int
    new_hash: str
    backup_path: Path | None = None
    manifest_updated: bool = False
    cache_updated: bool = False
    quality_logged: bool = False
    error: str | None = None


def parse_chapter_number(line_id: str) -> int:
    """Extract chapter integer from line_id (e.g. 'c002_0001', 'ch01_0000', 'repair-c002_0001')."""
    clean_id = line_id
    if clean_id.startswith("repair-"):
        clean_id = clean_id[len("repair-"):]
    match = re.search(r"(?:ch|c)?(\d+)_", clean_id, re.IGNORECASE)
    if match:
        return int(match.group(1))
    raise ValueError(f"Cannot extract chapter number from line_id {line_id!r}")


def backup_segment(segment_path: Path) -> Path | None:
    """Back up existing segment to a 'repair-backup' subfolder if not already present."""
    if not segment_path.is_file():
        return None
    backup_dir = segment_path.parent / "repair-backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_file = backup_dir / segment_path.name
    if not backup_file.exists():
        shutil.copy2(segment_path, backup_file)
    return backup_file


def rewrite_manifest_segment(manifest_path: Path, line_id: str, new_hash: str) -> bool:
    """Update line's output_hash in manifest and recompute manifest_hash."""
    if not manifest_path.is_file():
        logger.error("Manifest not found at %s", manifest_path)
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.exception("Failed to read manifest at %s: %s", manifest_path, exc)
        return False

    found = False
    for segment in manifest.get("segments", []):
        if segment.get("line_id") == line_id:
            segment["output_hash"] = new_hash
            found = True
            break

    if not found:
        logger.error("Line %s not found in manifest %s", line_id, manifest_path)
        return False

    manifest["manifest_hash"] = fingerprint({k: v for k, v in manifest.items() if k != "manifest_hash"})
    atomic_write_json(manifest_path, manifest)
    return True


def rewrite_cache_fingerprint(cache_db: Path, project_id: str, line_id: str, new_hash: str) -> bool:
    """Update output_hash in generation_fingerprints table if DB exists."""
    if not cache_db.is_file():
        return False
    try:
        with sqlite3.connect(cache_db) as conn:
            cursor = conn.execute(
                "update generation_fingerprints set output_hash=? where project_id=? and line_id=?",
                (new_hash, project_id, line_id),
            )
            return cursor.rowcount > 0
    except sqlite3.Error as exc:
        logger.warning("Failed to update cache DB at %s: %s", cache_db, exc)
        return False


def record_repaired_validation(
    state_db: Path,
    project_id: str,
    line_id: str,
    chapter: int,
    validated: Any,
    repaired_by: str = "shared/segment_repair.py",
) -> bool:
    """Insert a row in quality_logs carrying the repaired validation and transcript."""
    if not state_db.is_file():
        logger.warning("State DB not found at %s", state_db)
        return False

    payload = validated.model_dump() if hasattr(validated, "model_dump") else dict(validated)
    payload["repaired_by"] = repaired_by

    try:
        with sqlite3.connect(state_db) as conn:
            conn.execute(
                "insert into quality_logs "
                "(project_id, line_id, chapter_number, attempt, wer, quality_score, status, details, created_at) "
                "values (?,?,?,?,?,?,?,?,?)",
                (
                    project_id,
                    line_id,
                    chapter,
                    int(payload.get("attempt") or 1),
                    payload.get("wer"),
                    payload.get("quality_score"),
                    str(payload.get("status") or ""),
                    json.dumps(payload, default=str),
                    datetime.now(UTC).isoformat(),
                ),
            )
            return True
    except sqlite3.Error as exc:
        logger.warning("Failed to record quality log in %s: %s", state_db, exc)
        return False


def replace_segment(
    project_id: str,
    line_id: str,
    candidate_path: Path,
    validated_result: Any,
    *,
    project_dir: Path | None = None,
    segments_dir: Path | None = None,
    cache_db: Path | None = None,
    state_db: Path | None = None,
    chapter: int | None = None,
    repaired_by: str = "shared/segment_repair.py",
) -> SegmentRepairResult:
    """Replace an existing segment WAV with candidate audio, updating all 4 stores.

    Steps:
    1. Validate candidate audio exists and compute its hash.
    2. Update manifest; abort if line not present in manifest.
    3. Back up original segment WAV to segments/repair-backup/.
    4. Move candidate WAV into place.
    5. Update generation_fingerprints in voice_cache.db.
    6. Insert new quality_logs row in pipeline_state.db.
    """
    candidate_path = Path(candidate_path)
    if not candidate_path.is_file():
        return SegmentRepairResult(
            success=False,
            line_id=line_id,
            chapter=chapter or 0,
            new_hash="",
            error=f"Candidate audio does not exist: {candidate_path}",
        )

    if chapter is None:
        try:
            chapter = parse_chapter_number(line_id)
        except ValueError as exc:
            return SegmentRepairResult(
                success=False,
                line_id=line_id,
                chapter=0,
                new_hash="",
                error=str(exc),
            )

    proj_dir = project_dir or (_DEFAULT_ROOT / "brain" / "projects" / project_id)
    seg_dir = segments_dir or (_DEFAULT_ROOT / "workspace" / project_id / "segments")
    cache_path = cache_db or _DEFAULT_CACHE_DB
    state_path = state_db or _DEFAULT_STATE_DB

    manifest_path = proj_dir / "manifests" / f"chapter_{chapter:03d}.segments.json"
    segment_target_path = seg_dir / f"{line_id}.wav"
    new_hash = hash_file(candidate_path)

    # 1. Update manifest first. If manifest update fails, abort before touching files or DB.
    manifest_ok = rewrite_manifest_segment(manifest_path, line_id, new_hash)
    if not manifest_ok:
        return SegmentRepairResult(
            success=False,
            line_id=line_id,
            chapter=chapter,
            new_hash=new_hash,
            error=f"Failed to update manifest at {manifest_path}",
        )

    # 2. Back up original segment if it exists
    backup = backup_segment(segment_target_path)

    # 3. Replace audio file
    try:
        shutil.move(str(candidate_path), str(segment_target_path))
    except Exception as exc:
        logger.exception("Failed moving %s to %s: %s", candidate_path, segment_target_path, exc)
        return SegmentRepairResult(
            success=False,
            line_id=line_id,
            chapter=chapter,
            new_hash=new_hash,
            backup_path=backup,
            manifest_updated=manifest_ok,
            error=f"Audio move failed: {exc}",
        )

    # 4. Update cache
    cache_ok = rewrite_cache_fingerprint(cache_path, project_id, line_id, new_hash)

    # 5. Insert quality log
    quality_ok = record_repaired_validation(
        state_path,
        project_id,
        line_id,
        chapter,
        validated_result,
        repaired_by=repaired_by,
    )

    return SegmentRepairResult(
        success=True,
        line_id=line_id,
        chapter=chapter,
        new_hash=new_hash,
        backup_path=backup,
        manifest_updated=manifest_ok,
        cache_updated=cache_ok,
        quality_logged=quality_ok,
    )
