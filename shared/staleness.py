"""Staleness detection for audiobook deliveries and pronunciation evidence.

Detects when:
1. Deliveries (.m4b) are older than the remastered chapter audio (.wav) they contain.
2. Stored audio validation evidence in quality_logs is older than the book's pronunciation lexicon.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_STATE_DB = _DEFAULT_ROOT / "brain" / "projects" / "pipeline_state.db"


def check_delivery_staleness(
    workspace_dir: Path,
    deliveries_index: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return deliveries that are older than any chapter audio file they contain.

    Args:
        workspace_dir: Path to project workspace (contains 'chapters/' and 'output/')
        deliveries_index: Optional list of delivery dicts from DeliveryManager
    """
    chapters_dir = workspace_dir / "chapters"
    output_dir = workspace_dir / "output"
    if not chapters_dir.is_dir() or not output_dir.is_dir():
        return []

    chapter_mtimes: dict[int, float] = {}
    for path in chapters_dir.glob("chapter_*.wav"):
        stem = path.stem
        # Matches chapter_001, chapter_1, etc.
        parts = stem.split("_")
        if len(parts) >= 2 and parts[-1].isdigit():
            chapter_mtimes[int(parts[-1])] = path.stat().st_mtime

    if not chapter_mtimes:
        return []

    stale: list[dict[str, Any]] = []

    if deliveries_index:
        for item in deliveries_index:
            artifact_name = item.get("artifact", "")
            m4b_path = output_dir / artifact_name if artifact_name else None
            if not m4b_path or not m4b_path.is_file():
                continue
            built = m4b_path.stat().st_mtime
            chapters = [int(c) for c in item.get("chapter_numbers", [])]
            newer = sorted(c for c in chapters if chapter_mtimes.get(c, 0) > built)
            if newer:
                stale.append({
                    "delivery_id": item.get("delivery_id"),
                    "artifact": artifact_name,
                    "newer_chapters": newer,
                    "built_at": datetime.fromtimestamp(built, tz=UTC).isoformat(),
                })
        return stale

    # Fallback to inspecting output_dir files matching naming convention
    for delivery in sorted(output_dir.glob("*.m4b")):
        built = delivery.stat().st_mtime
        match = re.search(r"_chapters_(\d+)-(\d+)\.m4b$", delivery.name)
        if match:
            covered = set(range(int(match.group(1)), int(match.group(2)) + 1))
        else:
            covered = set(chapter_mtimes.keys())

        newer = sorted(c for c in covered if chapter_mtimes.get(c, 0) > built)
        if newer:
            stale.append({
                "delivery_id": delivery.stem,
                "artifact": delivery.name,
                "newer_chapters": newer,
                "built_at": datetime.fromtimestamp(built, tz=UTC).isoformat(),
            })

    return stale


def check_pronunciation_evidence_staleness(
    project_dir: Path,
    state_db: Path | None = None,
) -> dict[str, Any]:
    """Check if the latest quality_logs audio transcript is newer than the pronunciation dictionary.

    Returns dict with keys:
        evidence_current: bool
        evidence_freshness: str
        newest_audio: str | None
        lexicon_changed: str | None
    """
    db_path = state_db or _DEFAULT_STATE_DB
    if not db_path.is_file():
        return {
            "evidence_current": True,
            "evidence_freshness": "no database to verify against",
            "newest_audio": None,
            "lexicon_changed": None,
        }

    project_id = project_dir.name
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "select max(created_at) from quality_logs where project_id=?",
                (project_id,),
            ).fetchone()
            newest_audio = row[0] if row else None
    except Exception as exc:
        return {
            "evidence_current": True,
            "evidence_freshness": f"could not query quality logs: {exc}",
            "newest_audio": None,
            "lexicon_changed": None,
        }

    if not newest_audio:
        return {
            "evidence_current": False,
            "evidence_freshness": "no transcripts stored",
            "newest_audio": None,
            "lexicon_changed": None,
        }

    lexicon_mtime = 0.0
    for name in ("pronunciation_dict.json", "pronunciation_recommendations.json"):
        path = project_dir / name
        if path.is_file():
            lexicon_mtime = max(lexicon_mtime, path.stat().st_mtime)

    if lexicon_mtime == 0.0:
        return {
            "evidence_current": True,
            "evidence_freshness": "no lexicon files found",
            "newest_audio": newest_audio,
            "lexicon_changed": None,
        }

    lexicon_changed = datetime.fromtimestamp(lexicon_mtime, tz=UTC).isoformat()

    if lexicon_changed <= newest_audio:
        return {
            "evidence_current": True,
            "evidence_freshness": f"newest audio {newest_audio[:16]}, lexicon last changed {lexicon_changed[:16]}",
            "newest_audio": newest_audio,
            "lexicon_changed": lexicon_changed,
        }
    return {
        "evidence_current": False,
        "evidence_freshness": f"lexicon changed {lexicon_changed[:16]} but newest audio is {newest_audio[:16]}",
        "newest_audio": newest_audio,
        "lexicon_changed": lexicon_changed,
    }
