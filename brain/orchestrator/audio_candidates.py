"""Preserve and rank audio candidates across local and external validation."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shared.artifacts import atomic_write_json


@dataclass(frozen=True)
class PreservedCandidate:
    line_id: str
    audio_path: Path
    metadata_path: Path
    score: float
    result: Any


def candidate_score(result: Any) -> float:
    """Rank hard safety, external decisions, confidence, and local quality."""
    score = 100.0 if bool(result.passed_hard_gates) else -1000.0
    external = str(result.external_validation_decision or "")
    external_confidence = float(result.external_validation_confidence or 0.0)
    if external == "accept":
        score += 50.0 * external_confidence
    elif external == "reject":
        score -= 100.0 * external_confidence
    elif external == "abstain":
        score -= 10.0
    score += 30.0 * float(result.quality_score or 0.0)
    score -= 25.0 * float(result.wer or 0.0)
    score += 10.0 * float(result.validation_confidence or 0.0)
    if result.manual_review_required:
        score -= 5.0
    return score


def preserve_candidate(
    project_dir: Path, audio_path: Path, result: Any, *, retain: int = 2
) -> PreservedCandidate | None:
    """Copy a candidate into a bounded review area with its full decision trail."""
    audio_file = Path(audio_path)
    if not audio_file.is_file():
        return None
    destination = project_dir / "review_candidates" / result.line_id
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    stem = f"{stamp}-attempt-{int(result.attempt)}"
    saved_audio = destination / f"{stem}.wav"
    saved_meta = destination / f"{stem}.json"
    try:
        shutil.copy2(audio_file, saved_audio)
    except OSError:
        return None
    score = candidate_score(result)
    atomic_write_json(
        saved_meta,
        {
            "schema": 1,
            "line_id": result.line_id,
            "score": score,
            "created_at": datetime.now(UTC).isoformat(),
            "quality": result.model_dump(mode="json"),
        },
    )
    wavs = sorted(destination.glob("*.wav"), key=lambda path: path.stat().st_mtime, reverse=True)
    for stale in wavs[max(1, retain) :]:
        if stale != saved_audio:
            try:
                stale.unlink(missing_ok=True)
                stale.with_suffix(".json").unlink(missing_ok=True)
            except OSError:
                pass
    return PreservedCandidate(result.line_id, saved_audio, saved_meta, score, result.model_copy(deep=True))


def list_candidates(project_dir: Path, line_id: str) -> list[dict[str, Any]]:
    """Return ranked candidate metadata without loading audio into memory."""
    rows = []
    for meta_path in (project_dir / "review_candidates" / line_id).glob("*.json"):
        try:
            import json

            row = json.loads(meta_path.read_text(encoding="utf-8"))
            row["filename"] = meta_path.with_suffix(".wav").name
            rows.append(row)
        except (OSError, ValueError, TypeError):
            continue
    return sorted(rows, key=lambda row: (-float(row.get("score", -9999)), row.get("created_at", "")))
