"""Mobile API Router for Crazy Audiobook Creator (/api/mobile/v1/*).

Provides optimized catalog feeds, chapter manifests, and playback progress
synchronization for mobile clients like the Voice Audiobook Player.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
import urllib.request

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from brain.orchestrator.delivery_manager import DeliveryManager
from brain.orchestrator.job_queue import JobQueue
from shared import paths as shared_paths
from shared.artifacts import atomic_write_json

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mobile/v1", tags=["mobile"])


@router.get("/app")
async def download_voice_apk():
    """Download the latest compiled Voice client APK."""
    apk_path = Path("Voice-CrazyAudiobook-debug.apk").resolve()
    if not apk_path.is_file():
        apk_path = Path("E:/Projects/Voice/app/build/outputs/apk/free/debug/app-free-debug.apk").resolve()
    if not apk_path.is_file():
        raise HTTPException(status_code=404, detail="APK not found")
    return FileResponse(
        path=apk_path,
        media_type="application/vnd.android.package-archive",
        filename="Voice-CrazyAudiobook-debug.apk",
    )


class ProgressSyncRequest(BaseModel):
    client_id: str = Field(default="", max_length=128)
    chapter_number: int = Field(default=1, ge=1)
    position_ms: int = Field(default=0, ge=0)
    playback_speed: float = Field(default=1.0, ge=0.25, le=4.0)
    is_completed: bool = False


class PlaybackFlagRequest(BaseModel):
    chapter_number: int = Field(default=1, ge=1)
    position_ms: int = Field(default=0, ge=0)
    issue_type: str = Field(default="wrong_speaker", max_length=64)
    user_note: str = Field(default="", max_length=2000)
    source: str = Field(default="phone", max_length=64)
    line_id: str | None = Field(default=None, max_length=128)


class PlaybackFlagUpdateRequest(BaseModel):
    status: str | None = Field(default=None, max_length=64)
    agent_verdict: str | None = Field(default=None, max_length=64)
    agent_explanation: str | None = Field(default=None, max_length=4000)
    agent_veto: str | None = Field(default=None, max_length=4000)
    resolution: str | None = Field(default=None, max_length=4000)
    resolution_notes: str | None = Field(default=None, max_length=4000)
    resolved_by: str | None = Field(default=None, max_length=128)
    line_id: str | None = Field(default=None, max_length=128)


def _get_job_queue(request: Request) -> JobQueue:
    job_queue = getattr(request.app.state, "job_queue", None)
    if not job_queue:
        # Fallback to default instance
        return JobQueue()
    return job_queue


def _project_dir(project_id: str) -> Path:
    root = shared_paths.PROJECTS_DIR.resolve()
    candidate = (root / project_id).resolve()
    if not candidate.is_relative_to(root) or candidate == root:
        raise HTTPException(status_code=400, detail="Invalid project ID")
    return candidate


def _workspace_project_dir(project_id: str) -> Path:
    root = shared_paths.WORKSPACE_DIR.resolve()
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
            except (OSError, wave.Error, EOFError, ValueError):
                # Fallback: estimate from file size (24kHz 16-bit mono is 48000 bytes/sec)
                size = wav_path.stat().st_size
                if size > 44:
                    return round((size - 44) / 48000.0, 2)
    return None


@router.get("/server-info")
async def get_server_info(request: Request) -> dict[str, Any]:
    """Return server status and capabilities for mobile client discovery."""
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
    projects_root = shared_paths.PROJECTS_DIR.resolve()
    if not projects_root.is_dir():
        return {"books": []}

    active_job_ids = set()
    try:
        active_job_ids = {j.get("project_id") for j in job_queue.list_jobs() if j.get("project_id")}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.debug("Could not list active jobs for the mobile index: %s", exc)

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
            except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
                logger.warning(
                    "Could not read %s; the app will show this book with no chapter count: %s", book_json_path, exc
                )

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
                exported = json.loads(export_manifest.read_text(encoding="utf-8"))
                exported_chapters = {int(number) for number in exported.get("chapters", [])}
                complete_export = not bool(exported.get("partial")) and exported_chapters == set(
                    range(1, total_chapters + 1)
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
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning(
                "Could not read the delivery index for %s; the app will show zero published parts: %s", project_dir, exc
            )

        updated_at = (
            job_state.get("updated_at") or datetime.fromtimestamp(project_dir.stat().st_mtime, tz=UTC).isoformat()
        )

        books.append(
            {
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
                "is_live_generating": (len(mastered_chapters) > 0 and len(mastered_chapters) < total_chapters)
                or bool(job_state.get("running")),
                "cover_url": cover_url,
                "stream_url": f"api/projects/{project_id}/stream",
                "download_url": f"api/projects/{project_id}/download",
                "file_size_bytes": file_size,
                "published_deliveries_count": published_deliveries,
                "updated_at": updated_at,
            }
        )

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
        except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Could not read %s; the app will show placeholder metadata: %s", book_json_path, exc)

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
                exported = json.loads(export_manifest.read_text(encoding="utf-8"))
                mastered_set.update(int(number) for number in exported.get("chapters", []))
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
                # `characters` is a mapping of id -> entry, so iterating it
                # yields the ids. This read `for c in cdata.get("characters")`
                # and then `c.get(...)`, which raised AttributeError on every
                # book that has ever existed -- silently, because the handler
                # caught Exception. Narrowing that handler turned it into a
                # 500 on the Android app's book-detail call, which is how it
                # was finally found.
                entries = cdata.get("characters") or {}
                if isinstance(entries, dict):
                    entries = list(entries.values())
                for c in entries:
                    if not isinstance(c, dict):
                        continue
                    if c.get("id") == "narrator" or str(c.get("name") or "").lower() == "narrator":
                        narrator = c.get("speaker_name") or c.get("voice_name") or c.get("name")
                        break
            except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
                logger.debug("Could not resolve a narrator name from the character registry: %s", exc)

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
                d_dur = float(getattr(d, "duration_seconds", 0.0) or 0.0)
                d_artifact = str(getattr(d, "artifact", "") or "")

                part_ch_details: list[dict[str, Any]] = []
                part_cum_offset = 0.0
                delivery_dl_url = f"api/projects/{project_id}/deliveries/{d_id}/download"
                for c_num in d_chaps:
                    c_dur = _chapter_duration(project_dir, workspace_dir, c_num) or 0.0
                    c_title = chapter_titles.get(c_num) or f"Chapter {c_num}"
                    c_start = int(part_cum_offset * 1000)
                    c_end = int((part_cum_offset + c_dur) * 1000)
                    part_cum_offset += c_dur
                    part_ch_details.append(
                        {
                            "number": c_num,
                            "title": c_title,
                            "raw_title": c_title,
                            "source_heading": c_title,
                            "start_ms": c_start,
                            "end_ms": c_end,
                            "duration_seconds": c_dur,
                            "status": "mastered" if c_num in mastered_set else "pending",
                            "stream_url": delivery_dl_url,
                            "download_url": delivery_dl_url,
                        }
                    )

                if not d_dur and part_cum_offset > 0:
                    d_dur = part_cum_offset

                deliveries.append(
                    {
                        "delivery_id": str(d_id),
                        "title": f"Part {d_ord}: Chapters {min_c}-{max_c}",
                        "chapters": d_chaps,
                        "status": "published",
                        "download_url": f"api/projects/{project_id}/deliveries/{d_id}/download",
                        "filename": d_artifact,
                        "duration_seconds": d_dur,
                        "chapter_details": part_ch_details,
                    }
                )
    except (OSError, ValueError, KeyError, TypeError) as e:
        logger.warning("Could not load deliveries for %s: %s", project_id, e)

    chapters_list = []
    max_chapter = max(total_chapters, max(mastered_set, default=0), max(generated_set, default=0))
    for c_num in range(1, max_chapter + 1):
        is_mastered = c_num in mastered_set
        is_generated = c_num in generated_set
        ch_status = "mastered" if is_mastered else ("generating" if is_generated else "pending")
        dur = _chapter_duration(project_dir, workspace_dir, c_num) if is_mastered else None

        start_ms = 0
        end_ms = int((dur or 0.0) * 1000)

        raw_title = (chapter_titles.get(c_num) or "").strip()
        formatted_title = raw_title if raw_title else f"Chapter {c_num}"

        chapters_list.append(
            {
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
            }
        )

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
        "success": True,
        "progress": result,
        "saved_position": result,
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
            "saved_position": None,
            "chapter_number": 1,
            "position_ms": 0,
            "playback_speed": 1.0,
            "is_completed": False,
        }
    return {
        "project_id": project_id,
        "has_progress": True,
        "saved_position": {
            "client_id": progress.get("client_id", "voice_android"),
            "chapter_number": progress.get("chapter_number", 1),
            "position_ms": progress.get("position_ms", 0),
            "playback_speed": progress.get("playback_speed", 1.0),
            "is_completed": progress.get("is_completed", False),
            "updated_at": progress.get("updated_at"),
        },
        **progress,
    }


def _resolve_chapter_timeline(project_dir: Path, workspace_dir: Path, chapter_num: int) -> dict[str, tuple[int, int]]:
    """Return a mapping of line_id -> (start_ms, end_ms) for a mastered or segmented chapter."""
    timeline_path = project_dir / "manifests" / f"chapter_{chapter_num:03d}.timeline.json"
    if timeline_path.is_file():
        try:
            t_data = json.loads(timeline_path.read_text(encoding="utf-8"))
            if isinstance(t_data, list) and len(t_data) > 0:
                first_start = int(t_data[0].get("start_ms", 0))
                # Validate that timeline is chapter-relative (first line starts within first 15s)
                if 0 <= first_start < 15000:
                    return {
                        str(item["line_id"]): (int(item["start_ms"]), int(item["end_ms"]))
                        for item in t_data
                        if "line_id" in item and "start_ms" in item and "end_ms" in item
                    }
        except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Could not read the chapter timeline; the app cannot seek within this chapter: %s", exc)

    # Compute timeline dynamically from segments manifest
    seg_manifest_path = project_dir / "manifests" / f"chapter_{chapter_num:03d}.segments.json"
    if not seg_manifest_path.is_file():
        return {}

    try:
        seg_data = json.loads(seg_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError):
        return {}

    segments = seg_data.get("segments", [])
    if not segments:
        return {}

    # Measure segment audio lengths
    wav_segments_dir = workspace_dir / "segments"
    if not wav_segments_dir.is_dir():
        wav_segments_dir = project_dir / "segments"

    durations: list[int] = []
    for s in segments:
        lid = s.get("line_id", "")
        wav_candidate = wav_segments_dir / f"{lid}.wav"
        dur_ms = 0
        if wav_candidate.is_file():
            try:
                with wave.open(str(wav_candidate), "rb") as w:
                    frames = w.getnframes()
                    rate = w.getframerate()
                    if rate > 0:
                        dur_ms = int(round((frames / float(rate)) * 1000))
            except (OSError, wave.Error, EOFError, ValueError):
                size = wav_candidate.stat().st_size
                if size > 44:
                    dur_ms = int(round(((size - 44) / 48000.0) * 1000))
        durations.append(dur_ms)

    start_silence_ms = 1000
    current_time_ms = start_silence_ms
    prev_pause_after = 0
    prev_utterance_group = None
    raw_lines: list[tuple[str, int, int]] = []

    for i, s in enumerate(segments):
        lid = str(s.get("line_id", i))
        pause_before = int(s.get("pause_before_ms") or 0)
        ug_id = s.get("utterance_group_id")
        same_ug = ug_id is not None and ug_id == prev_utterance_group

        gap_before = 0 if i == 0 or same_ug else max(prev_pause_after, pause_before)
        start_ms = current_time_ms + gap_before
        dur_ms = durations[i]
        end_ms = start_ms + dur_ms

        raw_lines.append((lid, start_ms, end_ms))
        current_time_ms = end_ms
        prev_pause_after = int(s.get("pause_after_ms") or 500)
        prev_utterance_group = ug_id

    ch_wav = workspace_dir / "chapters" / f"chapter_{chapter_num:03d}.wav"
    if not ch_wav.is_file():
        ch_wav = project_dir / "chapters" / f"chapter_{chapter_num:03d}.wav"

    timeline_dict: dict[str, tuple[int, int]] = {}
    cached_list: list[dict[str, Any]] = []

    if raw_lines and ch_wav.is_file():
        actual_wav_ms = 0
        try:
            with wave.open(str(ch_wav), "rb") as w:
                actual_wav_ms = int(round((w.getnframes() / float(w.getframerate())) * 1000))
        except (OSError, wave.Error, EOFError, ValueError) as exc:
            logger.debug("Could not measure the mastered WAV duration; falling back to the manifest value: %s", exc)

        raw_end = raw_lines[-1][2]
        expected_body_end = max(actual_wav_ms - 2000, start_silence_ms + 1000) if actual_wav_ms > 4000 else raw_end
        diff = (actual_wav_ms - 2000) - raw_end if actual_wav_ms > 0 else 0

        # Check if chapter announcement audio was prepended (typically 2000-4500ms lead)
        if diff > 1500:
            announcement_lead_ms = diff
            body_start_ms = start_silence_ms + announcement_lead_ms
            scale = (actual_wav_ms - 2000 - body_start_ms) / float(max(1, raw_end - start_silence_ms))
            for lid, s_ms, e_ms in raw_lines:
                scaled_start = body_start_ms + int(round((s_ms - start_silence_ms) * scale))
                scaled_end = body_start_ms + int(round((e_ms - start_silence_ms) * scale))
                timeline_dict[lid] = (scaled_start, scaled_end)
                cached_list.append({"line_id": lid, "start_ms": scaled_start, "end_ms": scaled_end})
        elif actual_wav_ms > 4000 and raw_end > start_silence_ms:
            scale = (expected_body_end - start_silence_ms) / float(raw_end - start_silence_ms)
            for lid, s_ms, e_ms in raw_lines:
                scaled_start = start_silence_ms + int(round((s_ms - start_silence_ms) * scale))
                scaled_end = start_silence_ms + int(round((e_ms - start_silence_ms) * scale))
                timeline_dict[lid] = (scaled_start, scaled_end)
                cached_list.append({"line_id": lid, "start_ms": scaled_start, "end_ms": scaled_end})
        else:
            for lid, s_ms, e_ms in raw_lines:
                timeline_dict[lid] = (s_ms, e_ms)
                cached_list.append({"line_id": lid, "start_ms": s_ms, "end_ms": e_ms})
    else:
        for lid, s_ms, e_ms in raw_lines:
            timeline_dict[lid] = (s_ms, e_ms)
            cached_list.append({"line_id": lid, "start_ms": s_ms, "end_ms": e_ms})

    # Cache timeline to disk for future microsecond retrieval
    try:
        timeline_path.parent.mkdir(parents=True, exist_ok=True)
        timeline_path.write_text(json.dumps(cached_list, indent=2), encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("Could not cache the chapter timeline; the app will recompute it every request: %s", exc)

    return timeline_dict


def build_chapter_lyrics(project_id: str, chapter_number: int) -> dict[str, Any]:
    """Build synchronized karaoke/script lines with timestamps for a chapter."""
    project_dir = _project_dir(project_id)
    workspace_dir = _workspace_project_dir(project_id)

    script_file = project_dir / "script" / f"chapter_{chapter_number:03d}.json"
    if not script_file.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Script not available for chapter {chapter_number}",
        )

    try:
        script_data = json.loads(script_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=500, detail="Failed to parse chapter script") from exc

    timeline = _resolve_chapter_timeline(project_dir, workspace_dir, chapter_number)

    lines_out: list[dict[str, Any]] = []
    chapter_title = script_data.get("chapter_title") or script_data.get("title") or f"Chapter {chapter_number}"

    for line in script_data.get("lines", []):
        lid = str(line.get("line_id", ""))
        timing = timeline.get(lid, (0, 0))
        lines_out.append(
            {
                "line_id": lid,
                "speaker": line.get("speaker") or "Narrator",
                "speaker_id": line.get("voice_id") or line.get("speaker") or "narrator",
                "text": line.get("spoken_text") or line.get("text") or "",
                "emotion": line.get("emotion"),
                "start_ms": timing[0],
                "end_ms": timing[1],
                "source_start": line.get("source_start"),
                "source_end": line.get("source_end"),
            }
        )

    return {
        "project_id": project_id,
        "chapter_number": chapter_number,
        "chapter_title": chapter_title,
        "lines": lines_out,
    }


def build_chapter_reader(project_id: str, chapter_number: int) -> dict[str, Any]:
    """Build formatted chapter text partitioned into paragraphs with timing metadata."""
    project_dir = _project_dir(project_id)
    workspace_dir = _workspace_project_dir(project_id)

    # 1. Load book.json for chapter text
    book_file = project_dir / "book.json"
    chapter_text = ""
    chapter_title = f"Chapter {chapter_number}"
    source_heading = f"Chapter {chapter_number}"

    if book_file.is_file():
        try:
            bdata = json.loads(book_file.read_text(encoding="utf-8"))
            chapters = bdata.get("chapters", [])
            if 1 <= chapter_number <= len(chapters):
                ch = chapters[chapter_number - 1]
                chapter_text = ch.get("text", "")
                chapter_title = ch.get("title") or ch.get("source_heading") or chapter_title
                source_heading = ch.get("source_heading") or chapter_title
        except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
            logger.debug("Could not read chapter text from book.json; falling back to script lines: %s", exc)

    # 2. Load script & timeline for timing alignment
    script_file = project_dir / "script" / f"chapter_{chapter_number:03d}.json"
    script_lines = []
    if script_file.is_file():
        try:
            sdata = json.loads(script_file.read_text(encoding="utf-8"))
            script_lines = sdata.get("lines", [])
            if not chapter_text:
                # Fallback: assemble text from script lines if book.json text was missing
                chapter_text = "\n\n".join(l.get("text", "") for l in script_lines if l.get("text"))
        except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
            logger.warning(
                "Could not assemble chapter text from script lines; the app will show an empty chapter: %s", exc
            )

    timeline = _resolve_chapter_timeline(project_dir, workspace_dir, chapter_number)

    # Attach timing to script lines
    for line in script_lines:
        lid = str(line.get("line_id", ""))
        timing = timeline.get(lid, (0, 0))
        line["_start_ms"] = timing[0]
        line["_end_ms"] = timing[1]

    # 3. Partition chapter text into paragraphs
    raw_paragraphs = [p.strip() for p in re.split(r"\n\s*\n", chapter_text) if p.strip()]

    paragraphs_out: list[dict[str, Any]] = []
    char_search_offset = 0

    for idx, p_text in enumerate(raw_paragraphs):
        # Locate character range of this paragraph in chapter_text
        found_pos = chapter_text.find(p_text, char_search_offset)
        if found_pos >= 0:
            p_start_char = found_pos
            p_end_char = found_pos + len(p_text)
            char_search_offset = p_end_char
        else:
            p_start_char = 0
            p_end_char = len(p_text)

        # Match script lines overlapping this paragraph
        overlapping_lines = [
            l
            for l in script_lines
            if (
                l.get("source_start") is not None
                and l.get("source_end") is not None
                and l.get("source_start") < p_end_char
                and l.get("source_end") > p_start_char
                and l.get("_end_ms", 0) > 0
            )
        ]

        if overlapping_lines:
            p_start_ms = min(l["_start_ms"] for l in overlapping_lines)
            p_end_ms = max(l["_end_ms"] for l in overlapping_lines)
        else:
            p_start_ms = 0
            p_end_ms = 0

        paragraphs_out.append(
            {
                "index": idx,
                "text": p_text,
                "start_ms": p_start_ms,
                "end_ms": p_end_ms,
            }
        )

    # Fill in timing holes monotonically if any intro/transition paragraphs missed direct attribution
    last_valid_ms = 0
    for p in paragraphs_out:
        if p["start_ms"] == 0 and p["end_ms"] == 0:
            p["start_ms"] = last_valid_ms
            p["end_ms"] = last_valid_ms + 2000
        else:
            last_valid_ms = p["end_ms"]

    return {
        "project_id": project_id,
        "chapter_number": chapter_number,
        "title": chapter_title,
        "source_heading": source_heading,
        "total_paragraphs": len(paragraphs_out),
        "paragraphs": paragraphs_out,
    }


@router.get("/books/{project_id}/chapters/{chapter_number}/lyrics")
async def get_chapter_lyrics(
    project_id: str,
    chapter_number: int,
    request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Return synchronized karaoke/script lines with timestamps for a chapter."""
    return build_chapter_lyrics(project_id, chapter_number)


@router.get("/books/{project_id}/chapters/{chapter_number}/reader")
async def get_chapter_reader(
    project_id: str,
    chapter_number: int,
    request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Return formatted chapter text partitioned into paragraphs with timing metadata."""
    return build_chapter_reader(project_id, chapter_number)


@router.get("/books/{project_id}/epub")
async def download_book_epub(project_id: str, request: Request):
    """Download the original EPUB source file for offline reading."""
    project_dir = _project_dir(project_id)
    epub_path = project_dir / "source.epub"
    if not epub_path.is_file():
        # Check workspace or root
        candidate = shared_paths.WORKSPACE_DIR / project_id / "source.epub"
        if candidate.is_file():
            epub_path = candidate

    if not epub_path.is_file():
        raise HTTPException(status_code=404, detail="EPUB source file not found for this book")

    return FileResponse(
        path=str(epub_path),
        media_type="application/epub+zip",
        filename=f"{project_id}.epub",
    )


def _enrich_flag_context(
    project_dir: Path,
    workspace_dir: Path,
    chapter_number: int,
    position_ms: int,
    target_line_id: str | None = None,
) -> dict[str, Any]:
    """Enrich a playback flag with matching, candidate, and surrounding script lines, plus manuscript excerpt."""
    timeline = _resolve_chapter_timeline(project_dir, workspace_dir, chapter_number)

    # Load script lines if available
    script_file = project_dir / "script" / f"chapter_{chapter_number:03d}.json"
    script_lines: list[dict[str, Any]] = []
    if script_file.is_file():
        try:
            sdata = json.loads(script_file.read_text(encoding="utf-8"))
            script_lines = sdata.get("lines", [])
        except Exception as exc:
            logger.warning("Could not read script lines for flag enrichment: %s", exc)

    # Attach timing to script lines
    line_map: dict[str, dict[str, Any]] = {}
    for line in script_lines:
        lid = str(line.get("line_id", ""))
        timing = timeline.get(lid, (0, 0))
        line["_start_ms"] = timing[0]
        line["_end_ms"] = timing[1]
        line_map[lid] = line

    # 1. Natural Reaction Delay Window:
    # When listening or driving, users typically react 5-15 seconds after hearing an error.
    # We inspect a ~22 second window preceding and immediately following the tap.
    window_start_ms = max(0, position_ms - 20000)
    window_end_ms = position_ms + 2000

    matched_line: dict[str, Any] | None = None
    matched_idx: int = -1

    if target_line_id and target_line_id in line_map:
        matched_line = line_map[target_line_id]
        for idx, l in enumerate(script_lines):
            if str(l.get("line_id", "")) == target_line_id:
                matched_idx = idx
                break
    else:
        # Find line playing at the moment of tap
        best_candidate = None
        best_dist = float("inf")
        for idx, line in enumerate(script_lines):
            s_ms = line.get("_start_ms", 0)
            e_ms = line.get("_end_ms", 0)
            if s_ms <= position_ms <= e_ms:
                matched_line = line
                matched_idx = idx
                break
            dist = min(abs(position_ms - s_ms), abs(position_ms - e_ms))
            if dist < best_dist:
                best_dist = dist
                best_candidate = (idx, line)
        if matched_line is None and best_candidate:
            matched_idx, matched_line = best_candidate

    def _line_summary(l: dict[str, Any]) -> dict[str, Any]:
        s_ms = l.get("_start_ms", 0)
        e_ms = l.get("_end_ms", 0)
        txt = l.get("spoken_text") or l.get("text") or ""
        is_dialogue = bool(
            l.get("dialogue_kind") in ("spoken", "dialogue")
            or (l.get("speaker") and l.get("speaker") not in ("narrator", ""))
            or ('"' in txt)
            or ('“' in txt)
        )
        rel_sec = round((s_ms - position_ms) / 1000.0, 1)
        return {
            "line_id": str(l.get("line_id", "")),
            "speaker": l.get("speaker") or "narrator",
            "voice_id": l.get("voice_id") or l.get("speaker") or "narrator",
            "text": txt,
            "emotion": l.get("emotion"),
            "speaker_confidence": l.get("speaker_confidence"),
            "speaker_evidence": l.get("speaker_evidence"),
            "dialogue_kind": l.get("dialogue_kind"),
            "start_ms": s_ms,
            "end_ms": e_ms,
            "relative_sec": rel_sec,
            "is_at_tap": (s_ms <= position_ms <= e_ms),
            "is_dialogue": is_dialogue,
            "source_start": l.get("source_start"),
            "source_end": l.get("source_end"),
        }

    # Find all candidate lines that played in the 20-second reaction window
    candidate_lines: list[dict[str, Any]] = []
    candidate_indices: list[int] = []
    for idx, l in enumerate(script_lines):
        s_ms = l.get("_start_ms", 0)
        e_ms = l.get("_end_ms", 0)
        if e_ms >= window_start_ms and s_ms <= window_end_ms:
            candidate_lines.append(_line_summary(l))
            candidate_indices.append(idx)

    # If no candidate lines captured (e.g. at start of chapter), fallback to matched_idx
    if not candidate_indices and matched_idx >= 0:
        candidate_indices = [matched_idx]
        if matched_line:
            candidate_lines = [_line_summary(matched_line)]

    # Surrounding lines covering from 2 lines before earliest candidate to 2 lines after latest candidate
    surrounding: list[dict[str, Any]] = []
    if candidate_indices:
        start_idx = max(0, min(candidate_indices) - 2)
        end_idx = min(len(script_lines), max(candidate_indices) + 3)
        for i in range(start_idx, end_idx):
            surrounding.append(_line_summary(script_lines[i]))
    elif matched_idx >= 0:
        start_idx = max(0, matched_idx - 2)
        end_idx = min(len(script_lines), matched_idx + 3)
        for i in range(start_idx, end_idx):
            surrounding.append(_line_summary(script_lines[i]))

    # Load book.json excerpt covering the entire candidate range plus generous margin
    manuscript_excerpt = ""
    book_file = project_dir / "book.json"
    if book_file.is_file() and script_lines:
        try:
            bdata = json.loads(book_file.read_text(encoding="utf-8"))
            chapters = bdata.get("chapters", [])
            if 1 <= chapter_number <= len(chapters):
                ch_text = chapters[chapter_number - 1].get("text", "")
                
                valid_starts = [
                    l.get("source_start") for l in script_lines
                    if l.get("source_start") is not None and l.get("source_start") >= 0
                    and (not candidate_indices or script_lines.index(l) in candidate_indices)
                ]
                valid_ends = [
                    l.get("source_end") for l in script_lines
                    if l.get("source_end") is not None and l.get("source_end") >= 0
                    and (not candidate_indices or script_lines.index(l) in candidate_indices)
                ]

                if valid_starts and valid_ends:
                    excerpt_start = max(0, min(valid_starts) - 600)
                    excerpt_end = min(len(ch_text), max(valid_ends) + 600)
                    manuscript_excerpt = ch_text[excerpt_start:excerpt_end].strip()
                elif matched_line:
                    s_start = matched_line.get("source_start")
                    s_end = matched_line.get("source_end")
                    if s_start is not None and s_end is not None and 0 <= s_start < len(ch_text):
                        excerpt_start = max(0, s_start - 400)
                        excerpt_end = min(len(ch_text), s_end + 400)
                        manuscript_excerpt = ch_text[excerpt_start:excerpt_end].strip()
        except Exception as exc:
            logger.debug("Could not extract manuscript excerpt: %s", exc)

    return {
        "matched_line_id": str(matched_line.get("line_id", "")) if matched_line else None,
        "active_line": _line_summary(matched_line) if matched_line else None,
        "candidate_lines": candidate_lines,
        "surrounding_lines": surrounding,
        "reaction_window": {
            "window_start_ms": window_start_ms,
            "window_end_ms": window_end_ms,
            "tap_position_ms": position_ms,
            "delay_window_seconds": 20,
        },
        "manuscript_excerpt": manuscript_excerpt,
    }


def _sync_project_flags_json(project_dir: Path, job_queue: JobQueue, project_id: str) -> None:
    """Mirror current project flags to playback_flags.json in project directory."""
    flags = job_queue.get_playback_flags(project_id)
    flags_path = project_dir / "playback_flags.json"
    atomic_write_json(flags_path, {"project_id": project_id, "total_flags": len(flags), "flags": flags})


def _sync_flags_with_streamer_and_disk(
    project_id: str,
    project_dir: Path,
    workspace_dir: Path,
    job_queue: JobQueue,
) -> None:
    """Import any flags recorded on remote NAS Streamer or local disk JSON into SQLite."""
    disk_flags = []
    flags_path = project_dir / "playback_flags.json"
    if flags_path.is_file():
        try:
            with open(flags_path, "r", encoding="utf-8") as f:
                disk_flags = json.load(f).get("flags", [])
        except Exception:
            pass

    remote_flags = []
    try:
        remote_url = f"http://192.168.50.180:8005/api/mobile/v1/books/{project_id}/flags"
        req = urllib.request.Request(remote_url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            if resp.status == 200:
                remote_flags = json.loads(resp.read().decode("utf-8")).get("flags", [])
    except Exception:
        pass

    candidates: dict[str, dict[str, Any]] = {f["flag_id"]: f for f in disk_flags if f.get("flag_id")}
    for rf in remote_flags:
        if rf.get("flag_id"):
            candidates[rf["flag_id"]] = rf

    for fid, f in candidates.items():
        existing = job_queue.get_playback_flag(project_id, fid)
        if not existing:
            ch_num = int(f.get("chapter_number", 1))
            pos_ms = int(f.get("position_ms", 0))
            line_id = f.get("line_id")
            enriched = f.get("enriched_data") or f.get("line_metadata")
            if not enriched:
                enriched = _enrich_flag_context(project_dir, workspace_dir, ch_num, pos_ms, line_id)
            job_queue.create_playback_flag(
                project_id=project_id,
                flag_id=fid,
                chapter_number=ch_num,
                position_ms=pos_ms,
                source=f.get("source", "nas_streamer"),
                issue_type=f.get("issue_type", "wrong_speaker"),
                user_note=f.get("user_note", ""),
                line_id=line_id or enriched.get("matched_line_id"),
                enriched_data=enriched,
            )
            if f.get("status") and f["status"] != "pending":
                job_queue.update_playback_flag(
                    project_id=project_id,
                    flag_id=fid,
                    status=f.get("status"),
                    agent_verdict=f.get("agent_verdict"),
                    agent_explanation=f.get("agent_explanation"),
                    resolution=f.get("resolution"),
                    resolved_by=f.get("resolved_by"),
                )

    try:
        _sync_project_flags_json(project_dir, job_queue, project_id)
    except Exception:
        pass


@router.post("/books/{project_id}/flags", status_code=201)
async def create_playback_flag(
    project_id: str,
    request: PlaybackFlagRequest,
    req: Request,
) -> dict[str, Any]:
    """Flag a playback issue (e.g. wrong speaker/attribution) around the current playback point."""
    project_dir = _project_dir(project_id)
    workspace_dir = _workspace_project_dir(project_id)
    job_queue = _get_job_queue(req)

    flag_id = f"flag_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

    # Auto-enrich context with timeline and manuscript data
    enriched_data = _enrich_flag_context(
        project_dir=project_dir,
        workspace_dir=workspace_dir,
        chapter_number=request.chapter_number,
        position_ms=request.position_ms,
        target_line_id=request.line_id,
    )

    resolved_line_id = request.line_id or enriched_data.get("matched_line_id")

    flag = job_queue.create_playback_flag(
        project_id=project_id,
        flag_id=flag_id,
        chapter_number=request.chapter_number,
        position_ms=request.position_ms,
        source=request.source,
        issue_type=request.issue_type,
        user_note=request.user_note,
        line_id=resolved_line_id,
        enriched_data=enriched_data,
    )

    # Mirror to local project directory
    try:
        _sync_project_flags_json(project_dir, job_queue, project_id)
    except Exception as exc:
        logger.warning("Could not sync playback_flags.json: %s", exc)

    return {
        "status": "flagged",
        "flag": flag,
    }


@router.get("/books/{project_id}/flags")
async def get_playback_flags(
    project_id: str,
    req: Request,
    status: str | None = None,
) -> dict[str, Any]:
    """List all flagged playback issues for a project."""
    project_dir = _project_dir(project_id)
    workspace_dir = _workspace_project_dir(project_id)
    job_queue = _get_job_queue(req)

    # Sync any flags from remote NAS streamer or disk file
    _sync_flags_with_streamer_and_disk(project_id, project_dir, workspace_dir, job_queue)

    flags = job_queue.get_playback_flags(project_id, status=status)
    return {
        "project_id": project_id,
        "total_flags": len(flags),
        "flags": flags,
    }


@router.patch("/books/{project_id}/flags/{flag_id}")
async def update_playback_flag(
    project_id: str,
    flag_id: str,
    request: PlaybackFlagUpdateRequest,
    req: Request,
) -> dict[str, Any]:
    """Update a flag's investigation status, agent verdict, resolution, or veto."""
    project_dir = _project_dir(project_id)
    job_queue = _get_job_queue(req)

    explanation = request.agent_explanation or request.agent_veto
    resolution = request.resolution or request.resolution_notes
    verdict = request.agent_verdict or ("AGENT_VETO" if request.status == "vetoed" or request.agent_veto else None)

    enriched_data = None
    if request.line_id:
        existing_flag = job_queue.get_playback_flag(project_id, flag_id)
        if existing_flag:
            workspace_dir = _workspace_project_dir(project_id)
            enriched_data = _enrich_flag_context(
                project_dir=project_dir,
                workspace_dir=workspace_dir,
                chapter_number=existing_flag["chapter_number"],
                position_ms=existing_flag["position_ms"],
                target_line_id=request.line_id,
            )

    updated = job_queue.update_playback_flag(
        project_id=project_id,
        flag_id=flag_id,
        status=request.status,
        agent_verdict=verdict,
        agent_explanation=explanation,
        resolution=resolution,
        resolved_by=request.resolved_by,
        line_id=request.line_id,
        enriched_data=enriched_data,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Playback flag not found")

    try:
        _sync_project_flags_json(project_dir, job_queue, project_id)
    except Exception as exc:
        logger.warning("Could not sync playback_flags.json after update: %s", exc)

    # Mirror update to remote NAS streamer if reachable
    try:
        remote_patch_url = f"http://192.168.50.180:8005/api/mobile/v1/books/{project_id}/flags/{flag_id}"
        patch_payload = json.dumps({
            "status": request.status,
            "agent_verdict": verdict,
            "agent_explanation": explanation,
            "resolution": resolution,
            "resolved_by": request.resolved_by,
        }).encode("utf-8")
        patch_req = urllib.request.Request(
            remote_patch_url,
            data=patch_payload,
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        with urllib.request.urlopen(patch_req, timeout=1.0) as _:
            pass
    except Exception:
        pass

    return {
        "status": "updated",
        "flag": updated,
    }

