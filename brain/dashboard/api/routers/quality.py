"""Quality review and the per-chapter quality report.

Split out of `main.py`. This group was the last to move: its POST handler
resumes a paused run, which used to mean calling `start_pipeline` in
`main`. `runtime` now owns that indirection, so nothing here imports main.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import re
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from brain.dashboard.api import runtime
from brain.orchestrator.delivery_manager import DeliveryManager
from brain.orchestrator.review_gate import collect_review_gate
from shared.cache import cache_service

logger = logging.getLogger(__name__)

router = APIRouter()


class ReviewItemRequest(BaseModel):
    item_type: Literal["join", "segment"]
    item_id: str = Field(min_length=1, max_length=300)
    disposition: Literal["unreviewed", "acceptable", "needs_remaster", "source_tts_issue", "regenerate"]
    note: str = Field(default="", max_length=2000)


def _join_review_items(project_id: str) -> list[dict[str, Any]]:
    """Load mastering join diagnostics and enrich them with script links."""
    project_dir = runtime.project_dir(project_id)
    line_index = _script_line_index(project_id)
    items: list[dict[str, Any]] = []
    for manifest_path in sorted((project_dir / "manifests").glob("chapter_*.master.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        chapter = int(manifest.get("chapter_number") or 0)
        quality = manifest.get("mastering_quality") or {}
        for diagnostic in quality.get("join_diagnostics") or []:
            if diagnostic.get("status") != "warning":
                continue
            previous_id = str(diagnostic.get("previous_line_id") or "")
            current_id = str(diagnostic.get("current_line_id") or "")
            item_id = f"join:{chapter}:{previous_id}:{current_id}"
            loudness = float(diagnostic.get("loudness_delta_db") or 0.0)
            jump = float(diagnostic.get("zero_gap_sample_jump") or 0.0)
            severity = max(loudness / 8.0, jump / 0.15)
            items.append(
                {
                    "item_type": "join",
                    "item_id": item_id,
                    "chapter_number": chapter,
                    "previous_line_id": previous_id,
                    "current_line_id": current_id,
                    "previous_line": line_index.get(previous_id, {}),
                    "current_line": line_index.get(current_id, {}),
                    "previous_audio_url": (f"api/projects/{project_id}/segments/{previous_id}/audio"),
                    "current_audio_url": (f"api/projects/{project_id}/segments/{current_id}/audio"),
                    "severity": round(severity, 4),
                    **diagnostic,
                }
            )
    return sorted(items, key=lambda item: item["severity"], reverse=True)


@router.get("/api/projects/{project_id}/quality/review")
async def get_quality_review(project_id: str):
    """Return join diagnostics and persisted human review dispositions."""
    runtime.require_job(project_id)
    persisted = {(item["item_type"], item["item_id"]): item for item in runtime.job_queue.get_review_items(project_id)}
    joins = _join_review_items(project_id)
    for item in joins:
        review = persisted.get(("join", item["item_id"]), {})
        item["disposition"] = review.get("disposition", "unreviewed")
        item["review_note"] = review.get("note", "")
        item["reviewed_at"] = review.get("updated_at")
    latest_quality: dict[str, dict[str, Any]] = {}
    for row in runtime.job_queue.get_quality_report(project_id):
        details = row.get("details", {})
        if not details.get("selected"):
            continue
        latest_quality[row["line_id"]] = row
    project_dir = runtime.project_dir(project_id)
    script_lines_by_id: dict[str, dict[str, Any]] = {}
    for chapter_path in sorted((project_dir / "script").glob("chapter_*.json")):
        try:
            chapter = json.loads(chapter_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        chapter_num = chapter.get("chapter_number")
        for line in chapter.get("lines", []):
            lid = str(line.get("line_id") or line.get("id") or "unknown")
            script_lines_by_id[lid] = {**line, "chapter_number": chapter_num}

    segment_reviews = []
    for (item_type, item_id), review in persisted.items():
        if item_type != "segment":
            continue
        quality = latest_quality.get(item_id, {})
        details = quality.get("details", {})
        script_line = script_lines_by_id.get(item_id, {})
        segment_reviews.append(
            {
                "item_id": item_id,
                "chapter_number": quality.get("chapter_number") or script_line.get("chapter_number"),
                "disposition": review.get("disposition", "unreviewed"),
                "review_note": review.get("note", ""),
                "reviewed_at": review.get("updated_at"),
                "status": quality.get("status", "unknown"),
                "validation_confidence": details.get("validation_confidence"),
                "external_validation_provider": details.get("external_validation_provider", ""),
                "external_validation_model": details.get("external_validation_model", ""),
                "external_validation_decision": details.get("external_validation_decision", ""),
                "external_validation_confidence": details.get("external_validation_confidence"),
                "reason": details.get("manual_review_reason") or review.get("note", ""),
                "audio_url": f"api/projects/{project_id}/segments/{item_id}/audio",
                "text": script_line.get("text", ""),
                "speaker": script_line.get("speaker", ""),
            }
        )
    return {
        "join_warnings": joins,
        "segment_reviews": segment_reviews,
        "review_counts": dict(collections.Counter(item["disposition"] for item in joins)),
        "segment_review_counts": dict(collections.Counter(item["disposition"] for item in segment_reviews)),
    }


@router.post("/api/projects/{project_id}/quality/review")
async def update_quality_review(project_id: str, request: ReviewItemRequest):
    """Persist a non-destructive human quality-review disposition."""
    state = runtime.require_job(project_id)
    if request.item_type == "segment" and request.disposition == "regenerate":
        runtime.require_project_stopped(project_id)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", request.item_id):
            raise HTTPException(status_code=400, detail="Invalid line ID")
        quality = [row for row in runtime.job_queue.get_quality_report(project_id) if row["line_id"] == request.item_id]
        if not quality:
            raise HTTPException(status_code=404, detail="Quality record not found")
        chapter_number = int(quality[-1]["chapter_number"])
        workspace = runtime.workspaceruntime.project_dir(project_id)
        audio = workspace / "segments" / f"{request.item_id}.wav"
        audio.unlink(missing_ok=True)
        audio.with_suffix(".pt").unlink(missing_ok=True)
        updates = {
            "generated_chapters": [
                number for number in state.get("generated_chapters", []) if number != chapter_number
            ],
            "mastered_chapters": [number for number in state.get("mastered_chapters", []) if number != chapter_number],
            "export_stale": True,
        }
        runtime.job_queue.update_job(project_id, updates)
        project_dir = runtime.project_dir(project_id)
        for manifest in (
            project_dir / "manifests" / f"chapter_{chapter_number:03d}.segments.json",
            project_dir / "manifests" / f"chapter_{chapter_number:03d}.master.json",
        ):
            manifest.unlink(missing_ok=True)
        DeliveryManager(project_dir).mark_stale_for_chapters(
            {chapter_number},
            f"Manual regeneration requested for {request.item_id}",
        )
    result = runtime.job_queue.set_review_item(
        project_id,
        request.item_type,
        request.item_id,
        request.disposition,
        request.note.strip(),
    )
    runtime.job_queue.reconcile_external_validation(
        project_id,
        request.item_type,
        request.item_id,
        request.disposition,
    )
    result["auto_resuming"] = runtime.schedule_resume_after_reviews(project_id)
    return result


@router.get("/api/projects/{project_id}/reviews")
async def get_attention_reviews(project_id: str):
    """Return the unified, privacy-conscious Attention Required inbox."""
    runtime.require_job(project_id)
    pdir = runtime.project_dir(project_id)
    result = await asyncio.to_thread(lambda: collect_review_gate(project_id, pdir, runtime.job_queue).to_dict())
    release_path = pdir / "pre_master_release.json"
    if release_path.is_file():
        try:
            result["pre_master_release"] = json.loads(release_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            result["pre_master_release"] = None
    result["calibration"] = runtime.job_queue.external_validation_calibration(project_id)
    return result


@router.get("/api/projects/{project_id}/external-validation/events")
@router.get("/api/projects/{project_id}/quality")
async def get_quality_report(project_id: str):
    """Get quality report for a project."""
    if not runtime.job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    logs = runtime.job_queue.get_quality_report(project_id)

    current_chapters: set[int] | None = None
    active_review_item_ids: set[str] = set()
    try:
        job = runtime.job_queue.get_job(project_id)
        if isinstance(job, dict) and ("generated_chapters" in job or "mastered_chapters" in job):
            current_chapters = {
                int(ch) for ch in (job.get("generated_chapters", []) + job.get("mastered_chapters", []))
            }
        if hasattr(runtime.job_queue, "get_review_items"):
            active_review_item_ids = {
                item["item_id"]
                for item in runtime.job_queue.get_review_items(project_id)
                if item.get("item_type") == "segment" and item.get("disposition") in {"unreviewed", "flagged"}
            }
        if isinstance(job, dict):
            active_review_item_ids.update(str(item_id) for item_id in job.get("review_blocking_item_ids", []))
    except Exception:
        current_chapters = None
        active_review_item_ids = set()

    stale_line_ids: set[str] = set()
    filtered_logs = []
    for log in logs:
        ch_num = int(log.get("chapter_number", 0))
        lid = log.get("line_id", "")
        if current_chapters is not None and ch_num not in current_chapters and lid not in active_review_item_ids:
            stale_line_ids.add(lid)
        else:
            filtered_logs.append(log)

    summary = {
        "total_segments": 0,
        "passed_segments": 0,
        "accepted_with_warning_segments": 0,
        "failed_segments": 0,
        "flagged_segments": 0,
        "retries_triggered": 0,
        "average_wer": 0.0,
        "failed_silence": 0,
        "failed_clipping": 0,
        "final_attempts": [],
        "attempts": [],
        "stale_records": len(stale_line_ids),
        "stale": bool(stale_line_ids),
    }

    if not filtered_logs:
        return summary

    def _attempt_payload(log: dict[str, Any], *, include_audio: bool = False) -> dict[str, Any]:
        details = log.get("details", {})
        payload = {
            "line_id": log["line_id"],
            "chapter_number": log["chapter_number"],
            "attempt": log["attempt"],
            "status": log["status"],
            "wer": log["wer"],
            "quality_score": log["quality_score"],
            "acceptance_reason": details.get("acceptance_reason", ""),
            "transcribed_text": details.get("transcribed_text", ""),
            "duration_seconds": details.get("duration_seconds"),
            "noise_floor_db": details.get("noise_floor_db"),
            "speaker_similarity": details.get("speaker_similarity"),
            "prosody_warning": details.get("prosody_warning", False),
            "selected": bool(details.get("selected", False)),
            "validation_confidence": details.get("validation_confidence"),
            "external_validation_provider": details.get("external_validation_provider", ""),
            "external_validation_model": details.get("external_validation_model", ""),
            "external_validation_decision": details.get("external_validation_decision", ""),
            "external_validation_confidence": details.get("external_validation_confidence"),
            "external_validation_reason": details.get("external_validation_reason", ""),
            "manual_review_required": bool(details.get("manual_review_required", False)),
            "manual_review_reason": details.get("manual_review_reason", ""),
            "external_validation_history": details.get("external_validation_history", []),
        }
        if include_audio:
            payload["audio_url"] = f"api/projects/{project_id}/segments/{log['line_id']}/audio"
        return payload

    lines = {}
    retried_ids: set[str] = set()
    for log in filtered_logs:
        line_id = log["line_id"]
        is_selected = bool(log.get("details", {}).get("selected"))
        current_selected = bool(lines.get(line_id, {}).get("details", {}).get("selected"))
        if (
            line_id not in lines
            or (is_selected and not current_selected)
            or (is_selected == current_selected and log["attempt"] > lines[line_id]["attempt"])
        ):
            lines[line_id] = log
        if log["attempt"] > 1:
            summary["retries_triggered"] += 1
            retried_ids.add(line_id)

    summary["attempts"] = [_attempt_payload(log) for log in filtered_logs if log["line_id"] in retried_ids]

    summary["total_segments"] = len(lines)
    total_wer = 0.0

    for line in lines.values():
        if line["status"] == "pass":
            summary["passed_segments"] += 1
        elif line["status"] == "accepted_with_warning":
            summary["accepted_with_warning_segments"] += 1
        elif line["status"] == "flagged":
            summary["flagged_segments"] += 1
        else:
            summary["failed_segments"] += 1
        total_wer += line["wer"] or 0.0

        details = line.get("details", {})
        if details.get("has_long_silence", False):
            summary["failed_silence"] += 1
        if details.get("clipping_detected", False):
            summary["failed_clipping"] += 1
        if line["status"] != "pass" or line["attempt"] > 1 or details.get("manual_review_required", False):
            summary["final_attempts"].append(_attempt_payload(line, include_audio=True))

    if summary["total_segments"] > 0:
        summary["average_wer"] = total_wer / summary["total_segments"]

    return summary


def _script_line_index(project_id: str) -> dict[str, dict[str, Any]]:
    """Return source-safe display metadata for generated line IDs."""
    script_dir = runtime.project_dir(project_id) / "script"
    if not script_dir.is_dir():
        return {}

    try:
        file_mtimes = {
            p.name: p.stat().st_mtime
            for p in script_dir.glob("chapter_*.json")
            if re.fullmatch(r"chapter_\d{3,}\.json", p.name)
        }
    except OSError:
        file_mtimes = {}

    cache_key = f"script_line_index:{project_id}"
    cached = cache_service.get(cache_key)
    if cached and isinstance(cached, dict) and cached.get("file_mtimes") == file_mtimes:
        return cached.get("index", {})

    result: dict[str, dict[str, Any]] = {}
    for path in sorted(script_dir.glob("chapter_*.json")):
        if not re.fullmatch(r"chapter_\d{3,}\.json", path.name):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            ch_num = data.get("chapter_number")
            ch_title = data.get("chapter_title")
            for position, line in enumerate(data.get("lines", []), 1):
                lid = line.get("line_id") or line.get("id")
                if lid:
                    result[lid] = {
                        "chapter_number": ch_num,
                        "chapter_title": ch_title,
                        "position": position,
                        "speaker": line.get("speaker"),
                        "voice_id": line.get("voice_id") or line.get("speaker"),
                        "text": line.get("text", ""),
                    }
        except Exception:
            pass

    cache_service.set(
        cache_key,
        {"file_mtimes": file_mtimes, "index": result},
        ttl_seconds=1800,
    )
    return result
