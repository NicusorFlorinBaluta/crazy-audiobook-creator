"""Mobile API Router for Crazy Audiobook Creator (/api/mobile/v1/*).

Provides optimized catalog feeds, chapter manifests, and playback progress
synchronization for mobile clients like the Voice Audiobook Player.
"""

from __future__ import annotations

import json
import logging
import re
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from brain.orchestrator.delivery_manager import DeliveryManager
from brain.orchestrator.job_queue import JobQueue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mobile/v1", tags=["mobile"])


class ProgressSyncRequest(BaseModel):
    client_id: str = Field(default="", max_length=128)
    chapter_number: int = Field(default=1, ge=1)
    position_ms: int = Field(default=0, ge=0)
    playback_speed: float = Field(default=1.0, ge=0.25, le=4.0)
    is_completed: bool = False


def _get_job_queue(request: Request) -> JobQueue:
    job_queue = getattr(request.app.state, "job_queue", None)
    if not job_queue:
        # Fallback to default instance
        return JobQueue()
    return job_queue


def _project_dir(project_id: str) -> Path:
    root = Path("brain/projects").resolve()
    candidate = (root / project_id).resolve()
    if not candidate.is_relative_to(root) or candidate == root:
        raise HTTPException(status_code=400, detail="Invalid project ID")
    return candidate


def _workspace_project_dir(project_id: str) -> Path:
    root = Path("workspace").resolve()
    candidate = (root / project_id).resolve()
    if not candidate.is_relative_to(root) or candidate == root:
        raise HTTPException(status_code=400, detail="Invalid project ID")
    return candidate


def _existing_cover_url(project_id: str, project_dir: Path, metadata: dict[str, Any]) -> str | None:
    raw_path = metadata.get("cover_image_path")
    candidates = []
    if raw_path:
        configured = Path(str(raw_path))
        if not configured.is_absolute():
            candidates.extend((project_dir / configured, project_dir / configured.name))
        else:
            candidates.append(configured)
    candidates.extend((project_dir / "cover.jpg", project_dir / "cover.png"))
    for candidate in candidates:
        try:
            if candidate.is_file():
                return f"api/projects/{project_id}/cover?v={candidate.stat().st_mtime_ns}"
        except OSError:
            continue
    return None


def _chapter_duration(project_dir: Path, workspace_dir: Path, chapter_num: int) -> float | None:
    """Read chapter duration from master manifest or estimate from WAV header."""
    manifest_path = project_dir / "manifests" / f"chapter_{chapter_num:03d}.master.json"
    if manifest_path.is_file():
        try:
            m_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            quality = m_data.get("mastering_quality") or {}
            dur = quality.get("duration_seconds") or m_data.get("duration_seconds")
            if dur is not None:
                return round(float(dur), 2)
        except (OSError, ValueError, TypeError):
            pass

    wav_candidates = [
        workspace_dir / "chapters" / f"chapter_{chapter_num:03d}.wav",
        project_dir / "chapters" / f"chapter_{chapter_num:03d}.wav",
    ]
    for wav_path in wav_candidates:
        if wav_path.is_file():
            try:
                with wave.open(str(wav_path), "rb") as handle:
                    frames = handle.getnframes()
                    rate = handle.getframerate()
                    if rate > 0:
                        return round(frames / float(rate), 2)
            except Exception:
                # Fallback: estimate from file size (24kHz 16-bit mono is 48000 bytes/sec)
                size = wav_path.stat().st_size
                if size > 44:
                    return round((size - 44) / 48000.0, 2)
    return None


@router.get("/server-info")
async def get_server_info(request: Request) -> dict[str, Any]:
    """Return server status and capabilities for mobile client discovery."""
    pipeline = getattr(request.app.state, "pipeline", None)
    running_tasks = getattr(request.app.state, "running_tasks", {})
    active_project_id = next(
        (pid for pid, task in running_tasks.items() if not task.done()),
        None,
    )
    return {
        "server_name": "Crazy Audiobook Creator",
        "version": "2.0.0",
        "capabilities": {
            "streaming": True,
            "byte_ranges": True,
            "wav_chapter_streaming": True,
            "incremental_delivery": True,
            "progress_sync": True,
        },
        "active_project_id": active_project_id,
        "is_busy": active_project_id is not None,
    }


@router.get("/catalog")
async def get_catalog(
    request: Request,
    status: Literal["all", "ready_only", "in_progress"] = "all",
) -> dict[str, Any]:
    """Return a clean catalog feed of audiobooks ready for streaming or download."""
    job_queue = _get_job_queue(request)
    projects_root = Path("brain/projects").resolve()
    if not projects_root.is_dir():
        return {"books": []}

    active_job_ids = set()
    try:
        active_job_ids = {j.get("project_id") for j in job_queue.list_jobs() if j.get("project_id")}
    except Exception:
        pass

    books: list[dict[str, Any]] = []

    for project_dir in sorted(projects_root.iterdir()):
        if not project_dir.is_dir() or project_dir.name.startswith((".", "_")):
            continue
        project_id = project_dir.name
        if active_job_ids and project_id not in active_job_ids:
            continue

        try:
            job_state = job_queue.get_job(project_id)
        except KeyError:
            job_state = {}

        if not job_state:
            continue

        book_json_path = project_dir / "book.json"

        metadata: dict[str, Any] = {}
        total_chapters = int(job_state.get("total_chapters") or 0)
        book_chapters: list[dict[str, Any]] = []

        if book_json_path.is_file():
            try:
                bdata = json.loads(book_json_path.read_text(encoding="utf-8"))
                metadata = bdata.get("metadata", {})
                book_chapters = bdata.get("chapters", [])
                if total_chapters == 0:
                    total_chapters = len(book_chapters) or int(metadata.get("total_chapters") or 0)
            except Exception:
                pass

        title = metadata.get("title") or job_state.get("title") or project_id
        author = metadata.get("author") or job_state.get("author") or "Unknown Author"

        mastered_chapters = set(job_state.get("mastered_chapters") or [])
        generated_chapters = set(job_state.get("generated_chapters") or [])

        # Check existing chapter WAVs
        workspace_dir = _workspace_project_dir(project_id)
        for chapter_dir in (workspace_dir / "chapters", project_dir / "chapters"):
            if chapter_dir.is_dir():
                for wav in chapter_dir.glob("chapter_*.wav"):
                    m = re.match(r"chapter_(\d+)\.wav", wav.name)
                    if m:
                        mastered_chapters.add(int(m.group(1)))

        workspace_segments = workspace_dir / "segments"
        if workspace_segments.is_dir():
            for seg in workspace_segments.glob("*.wav"):
                m = re.match(r"ch(\d+)_", seg.name)
                if m:
                    generated_chapters.add(int(m.group(1)))

        # Check export M4B
        full_m4b = project_dir / f"{project_id}.m4b"
        if not full_m4b.is_file():
            full_m4b = workspace_dir / "output" / f"{project_id}.m4b"

        has_full_m4b = full_m4b.is_file() and full_m4b.stat().st_size > 0
        complete_export = has_full_m4b
        export_manifest = project_dir / "export_quality.json"
        if has_full_m4b and export_manifest.is_file():
            try:
                exported = json.loads(
                    export_manifest.read_text(encoding="utf-8")
                )
                exported_chapters = {
                    int(number) for number in exported.get("chapters", [])
                }
                complete_export = (
                    not bool(exported.get("partial"))
                    and exported_chapters == set(range(1, total_chapters + 1))
                )
            except (OSError, ValueError, TypeError):
                complete_export = False
        is_stale = bool(job_state.get("export_stale"))

        if complete_export and not is_stale:
            book_status = "ready_full"
        elif mastered_chapters or full_m4b.is_file():
            book_status = "ready_partial"
        elif generated_chapters or job_state.get("running"):
            book_status = "in_progress"
        else:
            book_status = "queued"

        # Do not expose unstarted/queued projects with 0 audio to mobile clients
        if book_status == "queued" and not mastered_chapters and not generated_chapters:
            continue

        if status == "ready_only" and book_status not in ("ready_full", "ready_partial"):
            continue
        if status == "in_progress" and book_status != "in_progress":
            continue

        # Total duration calculation
        total_duration = 0.0
        for c_num in sorted(mastered_chapters):
            dur = _chapter_duration(project_dir, workspace_dir, c_num)
            if dur:
                total_duration += dur

        cover_url = _existing_cover_url(project_id, project_dir, metadata)

        # File size
        file_size = full_m4b.stat().st_size if full_m4b.is_file() else None

        # Delivery count
        published_deliveries = 0
        try:
            dm = DeliveryManager(project_dir)
            deliveries = dm.load_index().deliveries
            published_deliveries = sum(1 for d in deliveries if getattr(d, "status", "") == "published")
        except Exception:
            pass

        updated_at = job_state.get("updated_at") or datetime.fromtimestamp(
            project_dir.stat().st_mtime, tz=timezone.utc
        ).isoformat()

        books.append({
            "project_id": project_id,
            "title": str(title),
            "author": str(author),
            "genre": str(metadata.get("genre") or ""),
            "year": str(metadata.get("year") or ""),
            "description": str(metadata.get("description") or ""),
            "isbn": str(metadata.get("isbn") or ""),
            "status": book_status,
            "total_chapters": total_chapters,
            "generated_chapters_count": len(generated_chapters),
            "mastered_chapters_count": len(mastered_chapters),
            "total_duration_seconds": round(total_duration, 2),
            "is_live_generating": (len(mastered_chapters) > 0 and len(mastered_chapters) < total_chapters) or bool(job_state.get("running")),
            "cover_url": cover_url,
            "stream_url": f"api/projects/{project_id}/stream",
            "download_url": f"api/projects/{project_id}/download",
            "file_size_bytes": file_size,
            "published_deliveries_count": published_deliveries,
            "updated_at": updated_at,
        })

    return {"books": sorted(books, key=lambda b: b.get("updated_at", ""), reverse=True)}


@router.get("/books/{project_id}")
async def get_book_detail(project_id: str, request: Request) -> dict[str, Any]:
    """Return detailed chapter manifest and delivery batches for an audiobook."""
    job_queue = _get_job_queue(request)
    project_dir = _project_dir(project_id)
    workspace_dir = _workspace_project_dir(project_id)

    try:
        job_state = job_queue.get_job(project_id)
    except KeyError:
        job_state = {}

    book_json_path = project_dir / "book.json"
    metadata: dict[str, Any] = {}
    book_chapters: list[dict[str, Any]] = []

    if book_json_path.is_file():
        try:
            bdata = json.loads(book_json_path.read_text(encoding="utf-8"))
            metadata = bdata.get("metadata", {})
            book_chapters = bdata.get("chapters", [])
        except Exception:
            pass

    title = metadata.get("title") or job_state.get("title") or project_id
    author = metadata.get("author") or job_state.get("author") or "Unknown Author"
    total_chapters = int(job_state.get("total_chapters") or len(book_chapters) or 0)

    mastered_set = set(job_state.get("mastered_chapters") or [])
    generated_set = set(job_state.get("generated_chapters") or [])

    for chapter_dir in (workspace_dir / "chapters", project_dir / "chapters"):
        if chapter_dir.is_dir():
            for wav in chapter_dir.glob("chapter_*.wav"):
                m = re.match(r"chapter_(\d+)\.wav", wav.name)
                if m:
                    mastered_set.add(int(m.group(1)))

    full_m4b = project_dir / f"{project_id}.m4b"
    if not full_m4b.is_file():
        full_m4b = workspace_dir / "output" / f"{project_id}.m4b"
    if full_m4b.is_file() and full_m4b.stat().st_size > 0:
        export_manifest = project_dir / "export_quality.json"
        if export_manifest.is_file():
            try:
                exported = json.loads(
                    export_manifest.read_text(encoding="utf-8")
                )
                mastered_set.update(
                    int(number) for number in exported.get("chapters", [])
                )
            except (OSError, ValueError, TypeError):
                pass

    chapter_titles: dict[int, str] = {}
    for idx, ch in enumerate(book_chapters, 1):
        if isinstance(ch, dict):
            source_heading = ch.get("source_heading") or ch.get("title")
            if source_heading:
                chapter_titles[idx] = str(source_heading)

    narrator = str(metadata.get("narrator") or "")
    if not narrator:
        chars_file = project_dir / "characters.json"
        if chars_file.is_file():
            try:
                cdata = json.loads(chars_file.read_text(encoding="utf-8"))
                for c in cdata.get("characters", []):
                    if c.get("id") == "narrator" or c.get("name", "").lower() == "narrator":
                        narrator = c.get("speaker_name") or c.get("voice_name") or c.get("name")
                        break
            except Exception:
                pass

    chapters_list = []
    max_chapter = max(total_chapters, max(mastered_set, default=0), max(generated_set, default=0))
    cumulative_offset = 0.0
    for c_num in range(1, max_chapter + 1):
        is_mastered = c_num in mastered_set
        is_generated = c_num in generated_set
        ch_status = "mastered" if is_mastered else ("generating" if is_generated else "pending")
        dur = _chapter_duration(project_dir, workspace_dir, c_num) if is_mastered else None

        start_ms = int(cumulative_offset * 1000)
        end_ms = int((cumulative_offset + (dur or 0.0)) * 1000)
        if dur:
            cumulative_offset += dur

        raw_title = (chapter_titles.get(c_num) or "").strip()
        formatted_title = raw_title if raw_title else f"Chapter {c_num}"

        chapters_list.append({
            "number": c_num,
            "title": formatted_title,
            "raw_title": formatted_title,
            "source_heading": formatted_title,
            "status": ch_status,
            "duration_seconds": dur,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "stream_url": f"api/projects/{project_id}/stream/chapter/{c_num}?format=aac" if is_mastered else None,
            "download_url": f"api/projects/{project_id}/download/chapter/{c_num}" if is_mastered else None,
        })

    # Deliveries
    deliveries = []
    try:
        dm = DeliveryManager(project_dir)
        for d in dm.load_index().deliveries:
            if getattr(d, "status", "") == "published":
                d_id = getattr(d, "delivery_id", "") or getattr(d, "id", "")
                d_ord = getattr(d, "ordinal", 1)
                d_chaps = getattr(d, "chapter_numbers", []) or getattr(d, "chapters", [])
                min_c = min(d_chaps) if d_chaps else 1
                max_c = max(d_chaps) if d_chaps else 1
                deliveries.append({
                    "delivery_id": str(d_id),
                    "title": f"Part {d_ord}: Chapters {min_c}-{max_c}",
                    "chapters": d_chaps,
                    "status": "published",
                    "download_url": f"api/projects/{project_id}/deliveries/{d_id}/download",
                })
    except Exception as e:
        logger.warning("Could not load deliveries for %s: %s", project_id, e)

    is_live = (len(mastered_set) > 0 and len(mastered_set) < total_chapters) or bool(job_state.get("running"))

    return {
        "project_id": project_id,
        "title": str(title),
        "author": str(author),
        "genre": str(metadata.get("genre") or ""),
        "year": str(metadata.get("year") or ""),
        "description": str(metadata.get("description") or ""),
        "isbn": str(metadata.get("isbn") or ""),
        "narrator": narrator or "AI Ensemble",
        "series": str(metadata.get("series") or ""),
        "part": str(metadata.get("series_index") or metadata.get("part") or ""),
        "total_chapters": total_chapters,
        "mastered_chapters_count": len(mastered_set),
        "is_live_generating": is_live,
        "cover_url": _existing_cover_url(project_id, project_dir, metadata),
        "stream_url": f"api/projects/{project_id}/stream",
        "download_url": f"api/projects/{project_id}/download",
        "deliveries": deliveries,
        "chapters": chapters_list,
    }


@router.post("/books/{project_id}/progress")
async def save_progress(
    project_id: str,
    request: ProgressSyncRequest,
    req: Request,
) -> dict[str, Any]:
    """Persist user playback progress from mobile client."""
    job_queue = _get_job_queue(req)
    # Validate project exists
    _project_dir(project_id)

    result = job_queue.set_playback_progress(
        project_id=project_id,
        client_id=request.client_id,
        chapter_number=request.chapter_number,
        position_ms=request.position_ms,
        playback_speed=request.playback_speed,
        is_completed=request.is_completed,
    )
    return {
        "status": "synced",
        "progress": result,
    }


@router.get("/books/{project_id}/progress")
async def get_progress(project_id: str, req: Request) -> dict[str, Any]:
    """Get the latest saved playback progress for a book."""
    job_queue = _get_job_queue(req)
    _project_dir(project_id)

    progress = job_queue.get_playback_progress(project_id)
    if progress is None:
        return {
            "project_id": project_id,
            "has_progress": False,
            "chapter_number": 1,
            "position_ms": 0,
            "playback_speed": 1.0,
            "is_completed": False,
        }
    return {
        "has_progress": True,
        **progress,
    }
