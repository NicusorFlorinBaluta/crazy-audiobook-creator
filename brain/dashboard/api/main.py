"""Brain Dashboard API — FastAPI application.

Serves the web dashboard and orchestrates the audiobook pipeline (v2).
Endpoints:
  - Static file serving for frontend (HTML/CSS/JS)
  - Project management CRUD
  - Pipeline control (start/stop/status)
  - Script viewer data
  - Quality reports
  - WebSocket for real-time updates
"""

from __future__ import annotations

import asyncio
import array
import collections
import csv
import hashlib
import io
import json
import logging
import math
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import time
import threading
import unicodedata
import uuid
import wave
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

import yaml
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

os.environ.setdefault("ROCM_SDK_TARGET_FAMILY", "custom")

from brain.orchestrator.pipeline import Pipeline
from brain.dashboard.api.security import (
    configured_dashboard_token,
    dashboard_request_authorized,
    is_cross_site_mutation,
    is_loopback_client,
)
from brain.orchestrator.job_queue import JobQueue
from shared.constants import PipelineStage, VOICE_CAST_SCHEMA_VERSION
from shared.artifacts import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    fingerprint,
    hash_file,
)
from shared.voice_casting import (
    build_voice_cast,
    compile_effective_voice_prompt,
    required_voice_character_ids,
    speaking_character_ids,
)
from shared.performance import read_metrics, summarize_metrics
from shared.runtime_preflight import collect_runtime_report
from voice.tts_server.voice_library import VoiceLibraryManager

logger = logging.getLogger(__name__)

FRONTEND_BUILD = "2026.08.12.2"


class AsyncioConnectionResetFilter(logging.Filter):
    """Filter out benign Windows asyncio socket connection reset errors."""
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "_call_connection_lost" in msg or "10054" in msg:
            return False
        return True

logging.getLogger("asyncio").addFilter(AsyncioConnectionResetFilter())

# Global state
pipeline: Pipeline | None = None
job_queue: JobQueue | None = None
ws_connections: list[WebSocket] = []
running_tasks: dict[str, asyncio.Task] = {}
_dashboard_shutdown_task: asyncio.Task | None = None
_metadata_locks: dict[str, threading.Lock] = {}
_metadata_locks_guard = threading.Lock()

_VOICE_STAGES = {
    PipelineStage.BOOTSTRAPPING.value,
    PipelineStage.GENERATING.value,
    PipelineStage.VALIDATING.value,
    PipelineStage.MASTERING.value,
    PipelineStage.EXPORTING.value,
}


async def _release_gpu_resources(wait_seconds: float = 15.0) -> None:
    """Stop active work and best-effort unload every app-owned GPU model."""
    if not pipeline:
        return

    active_projects = [
        project_id
        for project_id, task in running_tasks.items()
        if not task.done()
    ]
    for project_id in active_projects:
        pipeline.stop(project_id)

    voice_available = False
    try:
        await asyncio.to_thread(pipeline.voice_client.health_check_once)
        voice_available = True
    except Exception:
        pass

    if voice_available:
        for project_id in active_projects:
            try:
                await asyncio.to_thread(
                    pipeline.voice_client.cancel_project,
                    project_id,
                )
            except Exception as exc:
                logger.warning(
                    "Voice cancellation failed during shutdown for %s: %s",
                    project_id,
                    exc,
                )

    active_tasks = [
        task for task in running_tasks.values() if not task.done()
    ]
    if active_tasks:
        await asyncio.wait(active_tasks, timeout=wait_seconds)

    try:
        await asyncio.to_thread(pipeline.ollama.unload_model)
    except Exception:
        pass
    if voice_available:
        try:
            await asyncio.to_thread(pipeline.voice_client.unload_models)
        except Exception:
            pass
    pipeline._stop_ollama_server()
    pipeline._stop_voice_server()


async def _shutdown_dashboard_process(delay_seconds: float = 0.35) -> None:
    """Release app-owned resources, then terminate this exact API process.

    The short delay lets the HTTP 202 response reach the local restart helper.
    ``os._exit`` is intentional after cleanup: on Windows the dashboard may be
    the child of a virtual-environment launcher that Task Scheduler cannot end
    as a complete process tree from another restricted session.
    """
    await asyncio.sleep(delay_seconds)
    try:
        await _release_gpu_resources()
    finally:
        logging.shutdown()
        os._exit(0)


class ChapterSelectionRequest(BaseModel):
    chapters: list[int] | None = None


class VoiceAssignmentRequest(BaseModel):
    voice_id: str = Field(min_length=1, max_length=128)


class VoiceRegenerationRequest(BaseModel):
    voice_description: str = Field(min_length=12, max_length=1000)


class VoiceApprovalRequest(BaseModel):
    continue_pipeline: bool = True


class ReviewItemRequest(BaseModel):
    item_type: Literal["join", "segment"]
    item_id: str = Field(min_length=1, max_length=300)
    disposition: Literal[
        "unreviewed", "acceptable", "needs_remaster", "source_tts_issue"
    ]
    note: str = Field(default="", max_length=2000)


class CleanupRequest(BaseModel):
    confirmation_token: str = Field(min_length=64, max_length=64)


class MetadataFetchRequest(BaseModel):
    apply: bool = False
    refresh: bool = False
    replace_cover: bool = False
    provider_id: str | None = Field(default=None, max_length=128)
    query_title: str | None = Field(default=None, max_length=300)
    query_author: str | None = Field(default=None, max_length=300)


class MetadataSearchRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    author: str = Field(default="", max_length=300)


def _require_job(project_id: str) -> dict[str, Any]:
    if not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    try:
        return job_queue.get_job(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc


def _validation_reset_targets(state) -> list[int]:
    """Return the current audio selection for a validation-only rerun."""
    selection = state.get("generation_chapter_selection")
    if selection is None:
        selection = state.get("scripted_chapters") or []
    return sorted({int(chapter) for chapter in selection if int(chapter) > 0})

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


def _voice_project_dir(project_id: str) -> Path:
    config_path = Path("voice/config.yaml")
    config = (
        yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if config_path.exists()
        else {}
    )
    root = Path(
        config.get("storage", {}).get("voice_library_dir", "voice_library")
    ).resolve()
    candidate = (root / project_id).resolve()
    if not candidate.is_relative_to(root) or candidate == root:
        raise HTTPException(status_code=400, detail="Invalid project ID")
    return candidate


def _load_character_registry(project_id: str) -> tuple[Path, dict[str, Any]]:
    chars_path = _project_dir(project_id) / "characters.json"
    if not chars_path.exists():
        raise HTTPException(status_code=404, detail="Characters not analyzed yet")
    try:
        registry = json.loads(chars_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=500,
            detail="Character registry is invalid",
        ) from exc
    if not isinstance(registry.get("characters"), dict):
        raise HTTPException(status_code=500, detail="Character registry is invalid")
    return chars_path, registry


def _script_chapters(project_id: str) -> list[Any]:
    from shared.models import ScriptChapter

    chapters = []
    for path in sorted((_project_dir(project_id) / "script").glob("chapter_*.json")):
        if not re.fullmatch(r"chapter_\d{3,}\.json", path.name):
            continue
        try:
            chapters.append(
                ScriptChapter.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            )
        except Exception:
            logger.warning("Ignoring invalid script while building cast: %s", path)
    return chapters


def _script_line_index(project_id: str) -> dict[str, dict[str, Any]]:
    """Return source-safe display metadata for generated line IDs."""
    result: dict[str, dict[str, Any]] = {}
    for chapter in _script_chapters(project_id):
        for position, line in enumerate(chapter.lines, 1):
            result[line.line_id] = {
                "chapter_number": chapter.chapter_number,
                "chapter_title": chapter.chapter_title,
                "position": position,
                "speaker": line.speaker,
                "voice_id": line.voice_id or line.speaker,
                "text": line.text,
            }
    return result


def _join_review_items(project_id: str) -> list[dict[str, Any]]:
    """Load mastering join diagnostics and enrich them with script links."""
    project_dir = _project_dir(project_id)
    line_index = _script_line_index(project_id)
    items: list[dict[str, Any]] = []
    for manifest_path in sorted(
        (project_dir / "manifests").glob("chapter_*.master.json")
    ):
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
                    "previous_audio_url": (
                        f"api/projects/{project_id}/segments/{previous_id}/audio"
                    ),
                    "current_audio_url": (
                        f"api/projects/{project_id}/segments/{current_id}/audio"
                    ),
                    "severity": round(severity, 4),
                    **diagnostic,
                }
            )
    return sorted(items, key=lambda item: item["severity"], reverse=True)


def _path_inventory(root: Path) -> dict[str, int]:
    """Count regular files and bytes without following directory symlinks."""
    files = 0
    size = 0
    if not root.exists():
        return {"files": 0, "bytes": 0}
    for entry in root.rglob("*"):
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
            files += 1
            size += entry.stat().st_size
        except (FileNotFoundError, PermissionError):
            continue
    return {"files": files, "bytes": size}


def _cleanup_candidates(project_id: str) -> list[Path]:
    """Return only incomplete retry/temp artifacts under one workspace."""
    root = _workspace_project_dir(project_id)
    candidates: list[Path] = []
    for pattern in ("segments/.*.attempt-*.wav", "**/*.tmp"):
        for path in root.glob(pattern):
            resolved = path.resolve()
            if (
                resolved.is_relative_to(root)
                and resolved.is_file()
                and not resolved.is_symlink()
            ):
                candidates.append(resolved)
    return sorted(set(candidates))


def _cleanup_token(paths: list[Path]) -> str:
    payload = "\n".join(
        f"{path}|{path.stat().st_size}|{path.stat().st_mtime_ns}"
        for path in paths
        if path.exists()
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _redact_config(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "<redacted>"
                if any(term in key.lower() for term in ("token", "secret", "password"))
                and item
                else _redact_config(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_config(item) for item in value]
    return value


def _download_name_component(value: str, fallback: str) -> str:
    """Return a readable cross-platform filename component."""
    clean = unicodedata.normalize("NFKC", str(value or ""))
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", clean)
    clean = re.sub(r"\s+", " ", clean).strip(" ._")
    clean = clean or fallback
    reserved = {"con", "prn", "aux", "nul"}
    reserved.update(f"com{index}" for index in range(1, 10))
    reserved.update(f"lpt{index}" for index in range(1, 10))
    if clean.casefold() in reserved:
        clean = f"_{clean}"
    return clean[:80].rstrip(" ._") or fallback


def _registered_voice_path(
    project_id: str,
    voice_id: str,
) -> tuple[Path, dict[str, Any]]:
    """Resolve one registered reference without permitting registry escape."""
    voice_dir = _voice_project_dir(project_id).resolve()
    registry_path = voice_dir / "voices.json"
    try:
        registered = (
            json.loads(registry_path.read_text(encoding="utf-8")).get("voices", {})
            if registry_path.exists()
            else {}
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Voice registry is invalid") from exc
    info = registered.get(voice_id)
    if not info:
        raise HTTPException(status_code=404, detail="Voice not found")
    voice_path = (voice_dir / info.get("file", f"{voice_id}.wav")).resolve()
    if not voice_path.is_relative_to(voice_dir):
        raise HTTPException(status_code=400, detail="Invalid voice registry path")
    if not voice_path.is_file():
        raise HTTPException(status_code=404, detail="Voice sample is not available")
    return voice_path, info


def _voice_download_label(
    cast: dict[str, Any],
    voice_id: str,
    info: dict[str, Any],
) -> str:
    """Name a profile using its character owner and optional variant."""
    profiles = cast.get("voices", {})
    profile = profiles.get(voice_id, {})
    owner_id = str(profile.get("owner_character_id") or voice_id)
    owner = profiles.get(owner_id, {})
    owner_name = str(
        owner.get("name")
        or (profile.get("name") if owner_id == voice_id else "")
        or info.get("name")
        or ("Narrator" if owner_id == "narrator" else owner_id)
    )
    variant_name = str(profile.get("name") or info.get("name") or "").strip()
    if owner_id != voice_id and variant_name and variant_name.casefold() != owner_name.casefold():
        return f"{owner_name} - {variant_name}"
    return owner_name


def _load_or_build_voice_cast(
    project_id: str,
    registry_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cast_path = _project_dir(project_id) / "voice_cast.json"
    if registry_data is None:
        _, registry_data = _load_character_registry(project_id)
    registry_characters = registry_data.get("characters", {})
    if cast_path.is_file():
        try:
            cast = json.loads(cast_path.read_text(encoding="utf-8"))
            has_narrator = any(
                profile.get("owner_character_id") == "narrator"
                for profile in cast.get("voices", {}).values()
            )
            if (
                isinstance(cast.get("voices"), dict)
                and "speaking_characters" in cast
                and ("narrator" not in registry_characters or has_narrator)
            ):
                return cast
            logger.info("Rebuilding cast missing required narrator: %s", project_id)
        except (OSError, json.JSONDecodeError):
            logger.warning("Rebuilding invalid voice cast for %s", project_id)

    from shared.models import CharacterRegistry

    registry = CharacterRegistry.model_validate(registry_data)
    chapters = _script_chapters(project_id)
    speaker_ids = required_voice_character_ids(chapters, registry)
    voice_config_path = Path("voice/config.yaml")
    voice_config = (
        yaml.safe_load(voice_config_path.read_text(encoding="utf-8")) or {}
        if voice_config_path.exists()
        else {}
    )
    tts_config = voice_config.get("tts", {})
    cast = build_voice_cast(
        project_id=project_id,
        registry=registry,
        speaking_ids=speaker_ids,
        design_model=tts_config.get(
            "voice_design_model",
            "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        ),
        design_config={
            "test_sentences": tts_config.get(
                "voice_design_test_sentences", {}
            ),
            "language": tts_config.get("language", "English"),
            "reference_text_policy": "actual-dialogue-v1",
        },
    )
    atomic_write_json(cast_path, cast)
    return cast


def _save_voice_cast(project_id: str, cast: dict[str, Any]) -> None:
    from shared.artifacts import fingerprint

    payload = dict(cast)
    payload.pop("fingerprint", None)
    payload["fingerprint"] = fingerprint(payload)
    atomic_write_json(_project_dir(project_id) / "voice_cast.json", payload)


def _inspect_pcm_voice(path: Path) -> dict[str, float | int]:
    """Validate the canonical PCM WAV without loading optional audio packages."""
    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        sample_rate = audio.getframerate()
        sample_width = audio.getsampwidth()
        frame_count = audio.getnframes()
        frames = audio.readframes(frame_count)
    if channels != 1 or sample_rate != 24000 or sample_width != 2:
        raise ValueError("Voice conversion did not produce mono 24 kHz PCM16")
    samples = array.array("h")
    samples.frombytes(frames)
    if not samples:
        raise ValueError("Uploaded voice contains no audio samples")
    peak = max(abs(sample) for sample in samples) / 32768.0
    rms = math.sqrt(
        sum(float(sample) * float(sample) for sample in samples)
        / len(samples)
    ) / 32768.0
    duration = frame_count / sample_rate
    clipped = sum(abs(sample) >= 32760 for sample in samples) / len(samples)
    if duration < 3.0 or duration > 30.0:
        raise ValueError(
            "Reference audio must be between 3 and 30 seconds after conversion"
        )
    if rms < 0.003:
        raise ValueError("Reference audio is silent or too quiet")
    if clipped > 0.001:
        raise ValueError("Reference audio contains excessive clipping")
    return {
        "duration_seconds": duration,
        "sample_rate": sample_rate,
        "peak": peak,
        "rms": rms,
    }


async def _interrupt_pipeline_worker(
    project_id: str,
    *,
    reason: str,
    wait_seconds: float = 20.0,
) -> None:
    if not pipeline or not job_queue:
        return
    try:
        state = job_queue.get_job(project_id)
    except KeyError:
        return
    job_queue.update_job(
        project_id,
        {
            "status": PipelineStage.PAUSED.value,
            "pause_reason": reason,
            "running": False,
        },
    )
    pipeline.stop(project_id)
    
    task = running_tasks.get(project_id)
    if task and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=wait_seconds)
        except Exception:
            pass

def _voice_review_approval_update(approved_at: str, cast_revision: str) -> dict:
    return {
        "voice_review_status": "approved",
        "voice_review_approved_at": approved_at,
        "voice_review_approved_revision": cast_revision,
        "voice_review_approved": True,
        "pause_reason": None
    }

def _uploaded_transcript_error(result, threshold: float = 0.20) -> str | None:
    """Return a safe user-facing mismatch error, or None when ASR agrees."""
    import re
    effective_error = float(
        result.effective_text_error
        if getattr(result, "effective_text_error", None) is not None
        else result.wer
    )
    if effective_error <= threshold:
        return None
    heard = re.sub(r"\s+", " ", result.transcribed_text).strip()
    return (
        "Uploaded transcript does not match the recording "
        f"(effective error {effective_error:.1%}). "
        f"Whisper heard: {heard[:240] or '[no speech]'}"
    )

def _ensure_voice_editable(project_id: str) -> dict[str, Any]:
    if not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    try:
        state = job_queue.get_job(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc
    if state.get("running"):
        raise HTTPException(
            status_code=409,
            detail=(
                "Pause the pipeline at a safe boundary before changing voices. "
                "Voice previews remain available while it runs."
            ),
        )
    return state


def _chapters_for_speakers(
    project_id: str,
    speaker_ids: set[str],
) -> list[int]:
    from shared.models import ScriptChapter

    affected: list[int] = []
    script_dir = _project_dir(project_id) / "script"
    for path in sorted(script_dir.glob("chapter_*.json")):
        try:
            chapter = ScriptChapter.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except Exception:
            continue
        if any(line.speaker in speaker_ids for line in chapter.lines):
            affected.append(chapter.chapter_number)
    return sorted(set(affected))


def _mark_voice_chapters_stale(
    project_id: str,
    affected_chapters: list[int],
) -> None:
    if not job_queue or not affected_chapters:
        return
    state = job_queue.get_job(project_id)
    affected = set(affected_chapters)
    pending = set(state.get("voice_revision_pending_chapters", [])) | affected
    job_queue.update_job(
        project_id,
        {
            "generated_chapters": [
                number
                for number in state.get("generated_chapters", [])
                if number not in affected
            ],
            "mastered_chapters": [
                number
                for number in state.get("mastered_chapters", [])
                if number not in affected
            ],
            "voice_revision_pending_chapters": sorted(pending),
        },
    )


def _validate_epub_archive(path: Path, max_expanded_mb: int) -> None:
    """Reject unsafe ZIP paths and implausibly large EPUB expansions."""
    try:
        with zipfile.ZipFile(path) as archive:
            total = 0
            for info in archive.infolist():
                member = Path(info.filename)
                if member.is_absolute() or ".." in member.parts:
                    raise ValueError(f"Unsafe EPUB member path: {info.filename}")
                total += info.file_size
                if total > max_expanded_mb * 1024 * 1024:
                    raise ValueError(
                        f"Expanded EPUB exceeds {max_expanded_mb} MB limit"
                    )
                if (
                    info.compress_size > 0
                    and info.file_size / info.compress_size > 1000
                ):
                    raise ValueError(
                        f"Suspicious compression ratio in EPUB member: {info.filename}"
                    )
    except zipfile.BadZipFile as exc:
        raise ValueError("Uploaded file is not a valid EPUB/ZIP archive") from exc


def _purge_project_cache(project_id: str, voice_project: Path) -> None:
    """Remove project-scoped cache rows and clone prompts for its references."""
    cache_path = Path("voice_cache.db")
    if not cache_path.is_file():
        return
    reference_hashes = [
        digest
        for digest in (
            hash_file(path)
            for path in voice_project.glob("*.wav")
            if path.is_file()
        )
        if digest
    ]
    with sqlite3.connect(cache_path, timeout=10) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM generation_fingerprints WHERE project_id = ?",
            (project_id,),
        )
        connection.execute(
            "DELETE FROM speaker_embeddings WHERE project_id = ?",
            (project_id,),
        )
        if reference_hashes:
            placeholders = ",".join("?" for _ in reference_hashes)
            connection.execute(
                f"DELETE FROM voice_clone_prompts "
                f"WHERE ref_audio_hash IN ({placeholders})",
                reference_hashes,
            )
            connection.execute(
                f"DELETE FROM fx_prompt_cache "
                f"WHERE source_audio_hash IN ({placeholders})",
                reference_hashes,
            )
        connection.commit()

# ---------------------------------------------------------------------------
# Per-project log capture
# ---------------------------------------------------------------------------

# project_id -> deque of log line strings (ring buffer, max 500)
_project_logs: dict[str, collections.deque] = {}
# project_id -> list of asyncio.Queue for SSE subscribers
_log_subscribers: dict[str, list[asyncio.Queue]] = {}


class ProjectLogHandler(logging.Handler):
    """Logging handler that captures records to a per-project ring buffer
    and fans out to any live SSE subscribers."""

    # Suppress these noisy loggers from the project log stream
    _SUPPRESS = {
        "brain.dashboard.api.main",
        "uvicorn.access",
        "uvicorn.error",
    }

    def __init__(self, project_id: str):
        super().__init__()
        self.project_id = project_id
        self._loop: asyncio.AbstractEventLoop | None = None
        self.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord) -> None:
        # Skip dashboard / uvicorn noise
        if record.name in self._SUPPRESS:
            return
        try:
            line = self.format(record)
            pid = self.project_id

            # Store in ring buffer (safe from any thread)
            if pid not in _project_logs:
                _project_logs[pid] = collections.deque(maxlen=500)
            _project_logs[pid].append(line)

            # Append to disk log file so logs survive server restarts
            try:
                log_file = Path("brain/projects") / pid / "pipeline.log"
                log_file.parent.mkdir(parents=True, exist_ok=True)
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass

            # Fan out to SSE subscribers — MUST be thread-safe because
            # the pipeline runs in a thread-pool executor, not the event loop.
            loop = self._loop
            if loop and loop.is_running():
                for q in list(_log_subscribers.get(pid, [])):
                    loop.call_soon_threadsafe(q.put_nowait, line)
        except Exception:
            self.handleError(record)


def _attach_project_logger(project_id: str) -> ProjectLogHandler:
    """Attach a ProjectLogHandler to the root logger for this pipeline run."""
    if project_id not in _project_logs:
        _project_logs[project_id] = collections.deque(maxlen=500)
    if project_id not in _log_subscribers:
        _log_subscribers[project_id] = []

    handler = ProjectLogHandler(project_id)
    handler.setLevel(logging.INFO)
    # Capture the running event loop now (we're on the async thread)
    try:
        handler._loop = asyncio.get_running_loop()
    except RuntimeError:
        handler._loop = None
    logging.getLogger().addHandler(handler)
    return handler


def _detach_project_logger(handler: ProjectLogHandler) -> None:
    """Remove the handler from the root logger."""
    logging.getLogger().removeHandler(handler)


def load_config(config_path: str = "brain/config.yaml") -> dict[str, Any]:
    """Load configuration from YAML."""
    path = Path(config_path)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global pipeline, job_queue

    from shared.single_instance import SingleInstanceLock
    lock = SingleInstanceLock("dashboard.lock")
    if not lock.acquire():
        logger.error("Another Dashboard API instance is already running! Exiting.")
        import sys
        sys.exit(1)

    config = load_config()
    pipeline = Pipeline(config_path="brain/config.yaml")
    job_queue = pipeline.job_queue
    
    logging.getLogger().setLevel(logging.INFO)
    logger.info("Brain Dashboard starting...")

    # A process restart cannot preserve worker threads. Convert stale running
    # state into a resumable state; scheduled jobs remain eligible for the
    # automatic scheduler below.
    for stale_job in job_queue.list_jobs():
        if not stale_job.get("running"):
            continue
        stale_status = stale_job.get("status")
        replacement = (
            PipelineStage.PAUSED_SCHEDULED.value
            if stale_status == PipelineStage.PAUSED_SCHEDULED.value
            else PipelineStage.PAUSED.value
        )
        job_queue.update_job(
            stale_job["project_id"],
            {
                "status": replacement,
                "running": False,
                "pause_reason": "dashboard restarted",
            },
        )

    # Periodic background task to push live project updates via WebSocket
    async def ws_broadcast_loop():
        while True:
            await asyncio.sleep(2)
            if ws_connections and job_queue:
                try:
                    jobs = job_queue.list_jobs()
                    for job in jobs:
                        pid = job.get("project_id")
                        if pid and job.get("running"):
                            st = await get_pipeline_status(pid)
                            for ws in list(ws_connections):
                                try:
                                    await ws.send_json({
                                        "type": "status_update",
                                        "project_id": pid,
                                        "status": st
                                    })
                                except Exception:
                                    pass
                except Exception:
                    pass

    broadcast_task = asyncio.create_task(ws_broadcast_loop())

    async def schedule_resume_loop():
        while True:
            await asyncio.sleep(15)
            if not pipeline or not job_queue:
                continue
            # ``schedule_is_open`` deliberately returns True when scheduling
            # is disabled. This also releases jobs that were persisted as
            # paused_scheduled before a dashboard restart.
            if not pipeline.schedule_is_open():
                continue
            if any(not task.done() for task in running_tasks.values()):
                continue
            for scheduled_job in job_queue.list_jobs():
                if (
                    scheduled_job.get("status")
                    != PipelineStage.PAUSED_SCHEDULED.value
                ):
                    continue
                try:
                    await start_pipeline(scheduled_job["project_id"])
                    logger.info(
                        "Automatically resumed scheduled project %s",
                        scheduled_job["project_id"],
                    )
                except Exception:
                    logger.exception(
                        "Could not automatically resume scheduled project %s",
                        scheduled_job["project_id"],
                    )
                break

    schedule_task = asyncio.create_task(schedule_resume_loop())

    yield

    broadcast_task.cancel()
    schedule_task.cancel()
    if pipeline:
        await _release_gpu_resources()
        try:
            pipeline.ollama.close()
        except Exception:
            pass
        try:
            pipeline.voice_client.close()
        except Exception:
            pass
    lock.release()


app = FastAPI(
    title="Crazy Audiobook Creator — Brain Dashboard",
    description="Pipeline orchestration and monitoring dashboard",
    version="0.1.0",
    lifespan=lifespan,
)

_import_config = load_config()
_dashboard_cfg = _import_config.get("dashboard", {})
_cors_origins = _dashboard_cfg.get(
    "cors_origins",
    ["*"],
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials="*" not in _cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def disable_api_caching(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.middleware("http")
async def require_dashboard_token(request: Request, call_next):
    if (
        request.url.path.startswith("/api/")
        and is_cross_site_mutation(
            request.method,
            request.headers.get("Sec-Fetch-Site"),
        )
    ):
        return JSONResponse(
            {"detail": "Cross-site state changes are not allowed"},
            status_code=403,
        )
    token = configured_dashboard_token(_dashboard_cfg)
    client_host = request.client.host if request.client else None
    if (
        request.url.path.startswith("/api/")
        and not dashboard_request_authorized(
            client_host=client_host,
            configured_token=token,
            presented_token=request.headers.get("X-API-Token"),
        )
    ):
        if not token and not is_loopback_client(client_host):
            return JSONResponse(
                {"detail": "Remote dashboard access is not configured"},
                status_code=503,
            )
        return JSONResponse({"detail": "Invalid API token"}, status_code=401)
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        # The dashboard is also embedded in Home Assistant. Browsers can retain
        # an iframe and its static assets for much longer than a normal tab, so
        # force revalidation after every app restart/deployment. Asset query
        # revisions still prevent an old HTML document from mixing JS/CSS
        # generations once the iframe is reloaded.
        response.headers["Cache-Control"] = (
            "no-store, no-cache, max-age=0, must-revalidate"
        )
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["X-Crazy-Audiobook-UI-Version"] = FRONTEND_BUILD
    return response


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Static files (frontend)
# ---------------------------------------------------------------------------

frontend_dir = Path(__file__).parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")


@app.get("/")
async def serve_dashboard():
    """Serve the dashboard home page with dynamic cache busters."""
    index_path = frontend_dir / "index.html"
    if index_path.exists():
        content = index_path.read_text(encoding="utf-8")
        timestamp = str(int(time.time()))
        content = re.sub(
            r'((?:src|href)="static/[^"]+?\.(?:js|css))(?:\?v=[^"]*)?(")',
            rf'\1?v={timestamp}\2',
            content,
        )
        return HTMLResponse(
            content=content,
            headers={
                "Cache-Control": "no-store, no-cache, max-age=0, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
                "Clear-Site-Data": '"cache"',
            },
        )
    return JSONResponse(
        {"message": "Dashboard frontend not found. Place files in brain/dashboard/frontend/"},
        status_code=404,
    )


@app.get("/health", include_in_schema=False)
async def dashboard_health():
    """Return minimal readiness information for HA and the reverse proxy."""
    return {
        "status": "ok",
        "ready": pipeline is not None and job_queue is not None,
        "pipeline_running": any(not task.done() for task in running_tasks.values()),
    }


# ---------------------------------------------------------------------------
# Project management
# ---------------------------------------------------------------------------


@app.get("/api/projects")
async def list_projects():
    """List all projects."""
    if not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    return job_queue.list_jobs()


@app.post("/api/projects")
async def create_project(
    file: UploadFile = File(...),
    title: str = Form(default=""),
    author: str = Form(default=""),
):
    """Create a new project from an uploaded EPUB file."""
    if not pipeline:
        raise HTTPException(status_code=503, detail="Server not initialized")

    cfg = load_config()
    dashboard_cfg = cfg.get("dashboard", {})
    max_upload_mb = int(dashboard_cfg.get("max_upload_size_mb", 100))
    max_expanded_mb = int(dashboard_cfg.get("max_epub_expanded_mb", 1000))

    # Stream to a unique temporary file with a hard byte limit.
    temp_dir = Path("brain/projects/_uploads")
    temp_dir.mkdir(parents=True, exist_ok=True)
    safe_filename = Path(file.filename or "upload.epub").name
    if Path(safe_filename).suffix.lower() != ".epub":
        raise HTTPException(status_code=400, detail="Only .epub files are accepted")
    temp_path = temp_dir / f"{uuid.uuid4().hex}.epub"

    try:
        total_bytes = 0
        with open(temp_path, "xb") as handle:
            while chunk := await file.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > max_upload_mb * 1024 * 1024:
                    raise ValueError(f"EPUB exceeds {max_upload_mb} MB upload limit")
                handle.write(chunk)
        _validate_epub_archive(temp_path, max_expanded_mb)
        logger.info(
            "[DashboardAPI] Uploaded EPUB '%s' (%d bytes) for project creation",
            file.filename,
            total_bytes,
        )

        status = pipeline.create_project(str(temp_path))
        metadata_updates: dict[str, str] = {}
        if title.strip():
            metadata_updates["title"] = title.strip()[:300]
        if author.strip():
            metadata_updates["author"] = author.strip()[:300]
        if metadata_updates:
            project_dir = _project_dir(status.project_id)
            book_json = project_dir / "book.json"
            book_data = json.loads(book_json.read_text(encoding="utf-8"))
            book_data.setdefault("metadata", {}).update(metadata_updates)
            atomic_write_json(book_json, book_data)
            status = status.model_copy(update=metadata_updates)
            pipeline.job_queue.update_job(status.project_id, metadata_updates)
        logger.info("[DashboardAPI] Created project '%s' (%d chapters detected)", status.project_id, status.total_chapters)

        if cfg.get("metadata", {}).get("auto_fetch_external", False):
            asyncio.create_task(
                asyncio.to_thread(_auto_fetch_metadata_sync, status.project_id)
            )

        return {
            "project_id": status.project_id,
            "title": status.title,
            "author": status.author,
            "chapters_detected": status.total_chapters,
            "status": status.status,
        }

    except Exception as e:
        logger.error("[DashboardAPI] Failed to create project from '%s': %s", file.filename, e)
        raise HTTPException(status_code=400, detail=str(e))

    finally:
        if temp_path.exists():
            temp_path.unlink()
        for extracted_cover in temp_path.parent.glob(
            f"{temp_path.stem}_cover.*"
        ):
            if extracted_cover.is_file():
                extracted_cover.unlink()


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str):
    """Get project details."""
    if not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    try:
        job = job_queue.get_job(project_id)
        try:
            cast = _load_or_build_voice_cast(project_id)
            if cast:
                job["voice_cast"] = cast
                job_queue.update_job(project_id, {"voice_cast": cast})
        except Exception:
            logger.warning(
                "Could not merge dynamic voice cast for project %s",
                project_id,
                exc_info=True,
            )
        return await get_pipeline_status(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found")


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    """Delete a stopped project's state and local artifacts."""
    if not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    if project_id in running_tasks and not running_tasks[project_id].done():
        raise HTTPException(
            status_code=409,
            detail="Pause the pipeline before deleting this project",
        )
    try:
        job_queue.get_job(project_id)
        roots = [
            _project_dir(project_id),
            _workspace_project_dir(project_id),
        ]
        voice_project = _voice_project_dir(project_id)
        roots.append(voice_project)
        try:
            _purge_project_cache(project_id, voice_project)
        except sqlite3.Error as exc:
            logger.warning(
                "Could not purge cache rows for deleted project %s: %s",
                project_id,
                exc,
            )
        for root in roots:
            if root.is_dir():
                shutil.rmtree(root)
            elif root.exists():
                root.unlink()
        job_queue.delete_job(project_id)
        running_tasks.pop(project_id, None)
        _project_logs.pop(project_id, None)
        _log_subscribers.pop(project_id, None)
        return {"status": "deleted", "project_id": project_id}
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found")


# ---------------------------------------------------------------------------
# Pipeline control
# ---------------------------------------------------------------------------

@app.post("/api/projects/{project_id}/start")
async def start_pipeline(project_id: str):
    """Start the pipeline for a project."""
    if not pipeline or not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")

    if project_id in running_tasks and not running_tasks[project_id].done():
        raise HTTPException(status_code=409, detail="Pipeline already running")
    active_project = next(
        (
            pid
            for pid, task in running_tasks.items()
            if pid != project_id and not task.done()
        ),
        None,
    )
    if active_project:
        raise HTTPException(
            status_code=409,
            detail=(
                "GPU pipeline is serialized; wait for or pause project "
                f"'{active_project}' before starting another"
            ),
        )

    current = job_queue.get_job(project_id)
    if (
        current.get("voice_review_policy", "grandfathered")
        == "required_once"
        and current.get("bootstrapping_completed", False)
        and current.get("voice_review_status") != "approved"
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Voice casting approval is required before generation. "
                "Review the speaking cast and use Approve voices & continue."
            ),
        )
    reset_target = current.get("reset_target_stage")
    if reset_target:
        resume_stage = PipelineStage(reset_target)
    elif current.get("bootstrapping_completed", False):
        resume_stage = PipelineStage.GENERATING
    elif current.get("script_completed", False):
        resume_stage = PipelineStage.BOOTSTRAPPING
    else:
        resume_stage = PipelineStage.CREATED

    # Clear stale terminal state synchronously so status never reports
    # error/paused while the replacement worker is already running.
    job_queue.update_job(
        project_id,
        {
            "deployment_requested": False,
            "error_message": None,
            "running": True,
            "status": resume_stage.value,
            "active_stage": resume_stage.value,
            "pause_reason": None,
            "reset_target_stage": None,
        },
    )

    # Clear old logs for this project on a fresh start
    _project_logs[project_id] = collections.deque(maxlen=500)

    async def run_in_background():
        handler = _attach_project_logger(project_id)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, pipeline.run, project_id)
        except Exception as e:
            logger.error("Pipeline failed for %s: %s", project_id, e)
            for ws in ws_connections:
                try:
                    await ws.send_json({
                        "type": "error",
                        "project_id": project_id,
                        "message": str(e),
                    })
                except Exception:
                    pass
        finally:
            # The stop endpoint deliberately persists running=True while a
            # cooperative stop is in flight.  A worker can finish at a natural
            # boundary (for example voice review) before observing that stop
            # flag, so always reconcile the durable flag when its task exits.
            try:
                job_queue.update_job(project_id, {"running": False})
            except KeyError:
                pass
            _detach_project_logger(handler)
            # Send sentinel to all SSE subscribers so they know the run ended
            for q in list(_log_subscribers.get(project_id, [])):
                try:
                    q.put_nowait(None)  # None = stream done
                except Exception:
                    pass

    task = asyncio.create_task(run_in_background())
    running_tasks[project_id] = task

    return {"status": "started", "project_id": project_id}


@app.post("/api/projects/{project_id}/stop")
async def stop_pipeline(project_id: str):
    """Request a cooperative stop and report a transitional PAUSING state."""
    if not pipeline or not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
        
    try:
        state = job_queue.get_job(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found")
    job_queue.update_job(
        project_id,
        {
            "status": PipelineStage.PAUSED.value,
            "active_stage": state.get("active_stage") or state.get("status"),
            "pause_reason": "user requested stop",
            "running": True,
        },
    )

    pipeline.stop(project_id)

    active_stage = str(
        state.get("active_stage") or state.get("status") or ""
    )
    if active_stage in _VOICE_STAGES:
        try:
            await asyncio.to_thread(pipeline.voice_client.health_check_once)
            await asyncio.to_thread(
                pipeline.voice_client.cancel_project,
                project_id,
            )
        except Exception as exc:
            logger.warning(
                "Voice cancellation request failed for %s: %s",
                project_id,
                exc,
            )

    return {"status": "pausing", "project_id": project_id}


@app.post("/api/projects/{project_id}/reset")
async def reset_pipeline_stage(project_id: str, request: Request):
    """Reset the pipeline to a specific stage."""
    if not job_queue or not pipeline:
        raise HTTPException(status_code=503, detail="Server not initialized")
        
    if project_id in running_tasks and not running_tasks[project_id].done():
        raise HTTPException(status_code=409, detail="Cannot reset while pipeline is running. Please stop it first.")
        
    data = await request.json()
    stage_value = data.get("stage")
    if not stage_value:
        raise HTTPException(status_code=400, detail="Missing 'stage' in request body")
        
    try:
        stage = PipelineStage(stage_value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid stage: {stage_value}")
    supported = {
        PipelineStage.EXTRACTING,
        PipelineStage.SCRIPTING,
        PipelineStage.BOOTSTRAPPING,
        PipelineStage.VOICE_REVIEW,
        PipelineStage.GENERATING,
        PipelineStage.VALIDATING,
        PipelineStage.MASTERING,
        PipelineStage.EXPORTING,
    }
    if stage not in supported:
        raise HTTPException(
            status_code=422,
            detail=f"Stage '{stage_value}' is not supported for reset",
        )
        
    try:
        project_dir = _project_dir(project_id)
        workspace_dir = _workspace_project_dir(project_id)
        
        import shutil
        
        update: dict[str, Any] = {
            "status": "paused",
            "active_stage": stage.value,
            "pause_reason": "voice_review" if stage == PipelineStage.VOICE_REVIEW else "pipeline reset",
            "error_message": None,
            "running": False,
            "reset_target_stage": stage.value,
        }
        
        if stage == PipelineStage.EXTRACTING:
            try:
                await asyncio.to_thread(pipeline.reextract_project, project_id)
            except FileNotFoundError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            update.update({
                "script_completed": False,
                "bootstrapping_completed": False,
                "voice_review_approved": False,
                "voice_review_status": "pending",
                "voice_review_approved_at": None,
                "scripted_chapters": [],
                "generated_chapters": [],
                "mastered_chapters": [],
                "force_character_analysis": True,
            })
            for p in [project_dir / "characters.json", project_dir / "voice_cast.json"]:
                p.unlink(missing_ok=True)
            for d in [project_dir / "script", project_dir / "segments", project_dir / "mastered", workspace_dir / "segments", workspace_dir / "mastered"]:
                if d.exists() and d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
                    
        elif stage == PipelineStage.SCRIPTING:
            update.update({
                "script_completed": False,
                "bootstrapping_completed": False,
                "voice_review_approved": False,
                "voice_review_status": "pending",
                "voice_review_approved_at": None,
                "scripted_chapters": [],
                "generated_chapters": [],
                "mastered_chapters": [],
                "force_character_analysis": True,
            })
            for p in [project_dir / "characters.json", project_dir / "voice_cast.json"]:
                p.unlink(missing_ok=True)
            for d in [project_dir / "script", project_dir / "segments", project_dir / "mastered", workspace_dir / "segments", workspace_dir / "mastered"]:
                if d.exists() and d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
                    
        elif stage == PipelineStage.BOOTSTRAPPING:
            update.update({
                "bootstrapping_completed": False,
                "voice_review_approved": False,
                "voice_review_status": "pending",
                "voice_review_approved_at": None,
                "bootstrapping_fingerprint": None,
                "generated_chapters": [],
                "mastered_chapters": [],
                "force_voice_regeneration": True,
            })
            (project_dir / "voice_cast.json").unlink(missing_ok=True)
            for d in [project_dir / "segments", project_dir / "mastered", workspace_dir / "segments", workspace_dir / "mastered"]:
                if d.exists() and d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
                    
        elif stage == PipelineStage.VOICE_REVIEW:
            update.update({
                "status": "paused",
                "active_stage": "voice_review",
                "pause_reason": "voice_review",
                "voice_review_status": "pending",
                "voice_review_policy": "required_once",
                "voice_review_approved": False,
                "voice_review_approved_at": None,
                "generated_chapters": [],
                "mastered_chapters": [],
            })
            for d in [project_dir / "segments", project_dir / "mastered", workspace_dir / "segments", workspace_dir / "mastered"]:
                if d.exists() and d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
                    
        elif stage == PipelineStage.GENERATING:
            update.update({
                "generated_chapters": [],
                "mastered_chapters": [],
            })
            for d in [project_dir / "segments", project_dir / "mastered", workspace_dir / "segments", workspace_dir / "mastered"]:
                if d.exists() and d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)

        elif stage == PipelineStage.VALIDATING:
            update.update({
                "generated_chapters": [],
                "mastered_chapters": [],
                "validation_revision": uuid.uuid4().hex,
            })
            manifests_dir = project_dir / "manifests"
            if manifests_dir.is_dir():
                for manifest in manifests_dir.glob("chapter_*.segments.json"):
                    manifest.unlink(missing_ok=True)
                for manifest in manifests_dir.glob("chapter_*.master.json"):
                    manifest.unlink(missing_ok=True)
            for d in [project_dir / "mastered", workspace_dir / "mastered"]:
                if d.exists() and d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
                    
        elif stage == PipelineStage.MASTERING:
            update.update({"mastered_chapters": []})
            for d in [project_dir / "mastered", workspace_dir / "mastered"]:
                if d.exists() and d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
            (project_dir / f"{project_id}.m4b").unlink(missing_ok=True)
            (workspace_dir / "output" / f"{project_id}.m4b").unlink(missing_ok=True)

        elif stage == PipelineStage.EXPORTING:
            (project_dir / f"{project_id}.m4b").unlink(missing_ok=True)
            (workspace_dir / "output" / f"{project_id}.m4b").unlink(missing_ok=True)

        job_queue.update_job(project_id, update)
        return {"status": "success", "project_id": project_id, "stage": stage.value}
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found")


@app.get("/api/projects/{project_id}/download")
async def download_audiobook(project_id: str):
    """Download the final mastered audiobook."""
    project_dir = _project_dir(project_id)
    workspace_dir = _workspace_project_dir(project_id)
    m4b_path = project_dir / f"{project_id}.m4b"
    if not m4b_path.exists():
        m4b_path = workspace_dir / "output" / f"{project_id}.m4b"
    if not m4b_path.exists():
        partials = sorted(project_dir.glob("*.m4b"), key=lambda p: p.stat().st_mtime)
        if not partials:
            partials = sorted(
                workspace_dir.glob("**/*.m4b"),
                key=lambda p: p.stat().st_mtime,
            )
        if partials:
            m4b_path = partials[-1]
        else:
            raise HTTPException(status_code=404, detail="Audiobook file not found")
        
    title = project_id
    book_json = project_dir / "book.json"
    if book_json.is_file():
        try:
            title = (
                json.loads(book_json.read_text(encoding="utf-8"))
                .get("metadata", {})
                .get("title")
                or project_id
            )
        except (OSError, json.JSONDecodeError):
            pass
    return FileResponse(
        path=m4b_path,
        filename=f"{_download_name_component(str(title), project_id)}.m4b",
        media_type="audio/mp4"
    )


@app.get("/api/projects/{project_id}/download/chapter/{chapter_num}")
async def download_chapter_audio(project_id: str, chapter_num: int):
    """Download the mastered WAV file for a specific chapter."""
    if chapter_num < 1:
        raise HTTPException(status_code=422, detail="Chapter number must be positive")
    ch_file = (
        _workspace_project_dir(project_id)
        / "chapters"
        / f"chapter_{chapter_num:03d}.wav"
    )
    if not ch_file.exists():
        ch_file = _project_dir(project_id) / "chapters" / f"chapter_{chapter_num:03d}.wav"
    if not ch_file.exists():
        raise HTTPException(status_code=404, detail=f"Chapter {chapter_num} mastered audio not found")
    return FileResponse(
        path=ch_file,
        filename=f"{project_id}_chapter_{chapter_num:03d}.wav",
        media_type="audio/wav"
    )


@app.get("/api/projects/{project_id}/status")
async def get_pipeline_status(project_id: str):
    """Get the current pipeline status with detailed per-chapter metrics."""
    if not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    try:
        state = job_queue.get_job(project_id)
        state["running"] = (
            project_id in running_tasks
            and not running_tasks[project_id].done()
        )

        # Enrich state with per-chapter progress details
        project_dir = _project_dir(project_id)
        workspace_dir = Path("workspace") / project_id
        script_dir = project_dir / "script"
        segments_dir = workspace_dir / "segments"
        segment_counts: collections.Counter[int] = collections.Counter()
        if segments_dir.is_dir():
            for segment_path in segments_dir.glob("*.wav"):
                match = re.match(r"ch(\d+)_", segment_path.name)
                if match:
                    segment_counts[int(match.group(1))] += 1

        chapter_details = []
        total_chapters = state.get("total_chapters") or 0

        book_chapter_titles = {}
        book_json_path = project_dir / "book.json"
        if book_json_path.exists():
            try:
                import json
                bdata = json.loads(book_json_path.read_text(encoding="utf-8"))
                meta = bdata.get("metadata", {})
                b_chaps = bdata.get("chapters", [])
                
                if not state.get("title") or state.get("title") == "Unknown":
                    state["title"] = meta.get("title") or "Untitled"
                if not state.get("author") or state.get("author") == "Unknown":
                    state["author"] = meta.get("author") or "Unknown Author"

                state["description"] = meta.get("description") or ""
                state["genre"] = meta.get("genre") or ""
                state["year"] = meta.get("year") or ""
                state["isbn"] = meta.get("isbn") or ""
                cover = _existing_project_cover(project_dir, meta)
                if cover:
                    state["cover_url"] = (
                        f"api/projects/{project_id}/cover?v={cover.stat().st_mtime_ns}"
                    )

                calc_total = len(b_chaps) or meta.get("total_chapters", 0)
                if total_chapters == 0 and calc_total > 0:
                    total_chapters = calc_total
                state["total_chapters"] = total_chapters

                for idx, ch in enumerate(b_chaps, 1):
                    if isinstance(ch, dict) and ch.get("title"):
                        book_chapter_titles[idx] = ch.get("title")
            except Exception:
                pass

        stage = str(state.get("active_stage") or state.get("status") or "").lower()
        scripted_chapters = set(state.get("scripted_chapters", []))
        generated_chapters = set(state.get("generated_chapters", []))
        mastered_chapters = set(state.get("mastered_chapters", []))
        
        # Dynamically infer the next active scripting chapter if existing scripts exist
        existing_scripted = [
            c for c in range(1, total_chapters + 1)
            if (script_dir / f"chapter_{c:03d}.json").exists() or c in scripted_chapters
        ]
        if existing_scripted:
            current_script_ch = min(max(existing_scripted) + 1, total_chapters)
        else:
            current_script_ch = state.get("current_script_chapter") or 1
            
        work_prog = state.get("work_progress") or {}

        for ch_num in range(1, total_chapters + 1):
            ch_script_file = script_dir / f"chapter_{ch_num:03d}.json"
            title = book_chapter_titles.get(ch_num) or f"Chapter {ch_num}"
            total_lines = 0

            if ch_script_file.exists():
                try:
                    import json
                    script_data = json.loads(ch_script_file.read_text(encoding="utf-8"))
                    title = script_data.get("chapter_title", title)
                    raw_lines = script_data.get("lines", [])
                    total_lines = len(raw_lines)
                except Exception:
                    pass

            gen_count = segment_counts[ch_num] if total_lines > 0 else 0
            validated_count = (
                int(state.get("lines_validated") or 0)
                if ch_num == state.get("current_gen_chapter")
                else total_lines if ch_num in generated_chapters else 0
            )

            # Compute stage-aware progress percentage
            if ch_num in mastered_chapters or ch_num in generated_chapters:
                pct = 100
            elif "script" in stage or stage in ["voice_review", "bootstrapping"]:
                if ch_script_file.exists() or ch_num in scripted_chapters:
                    pct = 100
                elif ch_num == current_script_ch:
                    # Scan recent log lines for active fragment chunk progress (e.g. Chunk 3/5)
                    cur_chunk = 0
                    tot_chunks = 1
                    logs = list(_project_logs.get(project_id, []))
                    if not logs:
                        log_file = project_dir / "pipeline.log"
                        if log_file.exists():
                            try:
                                logs = log_file.read_text(encoding="utf-8", errors="ignore").splitlines()[-100:]
                            except Exception:
                                pass
                    for line in reversed(logs):
                        m_chunk = re.search(r"Processing fragment chunk\s+(\d+)/(\d+)", line)
                        if m_chunk:
                            cur_chunk = int(m_chunk.group(1))
                            tot_chunks = int(m_chunk.group(2))
                            break
                    if cur_chunk > 0 and tot_chunks > 0:
                        pct = int(100 * (cur_chunk - 0.5) / tot_chunks)
                        pct = min(max(pct, 10), 95)
                    else:
                        pct = 15
                else:
                    pct = 0
            elif total_lines > 0:
                pct = int((gen_count / total_lines) * 100)
            elif ch_script_file.exists() or ch_num in scripted_chapters:
                pct = 100
            else:
                pct = 0

            chapter_details.append({
                "number": ch_num,
                "title": title,
                "total_lines": total_lines,
                "lines_generated": gen_count,
                "lines_validated": min(validated_count, total_lines),
                "progress_percent": min(max(pct, 0), 100)
            })

        state["chapter_details"] = chapter_details
        return state
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found")


# ---------------------------------------------------------------------------
# Feature Expansion Endpoints (Schedule, Metadata, Deploy, Selective)
# ---------------------------------------------------------------------------


@app.post("/api/schedule")
async def update_schedule(request: Request):
    """Update schedule config in brain/config.yaml."""
    data = await request.json()
    enabled = bool(data.get("enabled", False))
    timezone_name = str(
        data.get("timezone", "Europe/Bucharest")
    ).strip()
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown timezone: {timezone_name}",
        ) from exc
    valid_day_order = (
        "Monday", "Tuesday", "Wednesday", "Thursday",
        "Friday", "Saturday", "Sunday",
    )
    valid_days = set(valid_day_order)
    windows = data.get("windows", [])
    if not isinstance(windows, list):
        raise HTTPException(status_code=422, detail="windows must be a list")
    normalized_windows = []
    for index, window in enumerate(windows):
        try:
            start = str(window["start"])
            end = str(window["end"])
            parsed_start = datetime.strptime(start, "%H:%M")
            parsed_end = datetime.strptime(end, "%H:%M")
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Window {index + 1} must use HH:MM start/end times",
            ) from exc
        days = window.get("days", [])
        if (
            not isinstance(days, list)
            or not days
            or any(day not in valid_days for day in days)
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Window {index + 1} must select at least one valid "
                    "weekday"
                ),
            )
        start = parsed_start.strftime("%H:%M")
        end = parsed_end.strftime("%H:%M")
        if start == end:
            raise HTTPException(
                status_code=422,
                detail=f"Window {index + 1} start and end must differ",
            )
        normalized_windows.append(
            {
                "days": [day for day in valid_day_order if day in set(days)],
                "start": start,
                "end": end,
            }
        )
    if enabled and not normalized_windows:
        raise HTTPException(
            status_code=422,
            detail="Add at least one working window before enabling scheduling",
        )
    data = {
        "enabled": enabled,
        "timezone": timezone_name,
        "windows": normalized_windows,
    }
    config_path = Path("brain/config.yaml")
    _replace_yaml_section(config_path, "schedule", data)
    if pipeline:
        pipeline.config = pipeline._load_config()
    return {"status": "success", "schedule": data}


def _replace_yaml_section(
    path: Path,
    section: str,
    value: dict[str, Any],
) -> None:
    """Replace one top-level YAML section without discarding file comments."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    block = yaml.safe_dump(
        {section: value},
        sort_keys=False,
        allow_unicode=True,
    ).rstrip() + "\n"
    section_pattern = re.compile(
        rf"(?ms)^{re.escape(section)}:\s*\n.*?(?=^[A-Za-z_][\w-]*:\s*(?:#.*)?$|\Z)"
    )
    if section_pattern.search(text):
        updated = section_pattern.sub(block, text, count=1)
    else:
        updated = text.rstrip() + ("\n\n" if text.strip() else "") + block
    atomic_write_text(path, updated)


@app.get("/api/schedule")
async def get_schedule():
    """Return the working-hours configuration and current open/closed state."""
    config = load_config()
    schedule = config.get(
        "schedule",
        {
            "enabled": False,
            "timezone": "Europe/Bucharest",
            "windows": [],
        },
    )
    return {
        "schedule": schedule,
        "is_open": pipeline.schedule_is_open() if pipeline else True,
    }


@app.post("/api/system/shutdown", status_code=202)
async def shutdown_dashboard():
    """Gracefully stop the dashboard after normal API authentication."""
    global _dashboard_shutdown_task

    if _dashboard_shutdown_task and not _dashboard_shutdown_task.done():
        return {
            "status": "already_shutting_down",
            "pid": os.getpid(),
        }

    _dashboard_shutdown_task = asyncio.create_task(
        _shutdown_dashboard_process()
    )
    return {
        "status": "shutting_down",
        "pid": os.getpid(),
    }


@app.get("/api/voice/health")
async def get_voice_health():
    """Report actual Voice server availability without retry/startup noise."""
    if not pipeline:
        return {"online": False, "status": "dashboard_initializing"}
    try:
        health = await asyncio.to_thread(
            pipeline.voice_client.health_check_once,
            1.5,
        )
        return {
            "online": health.status == "ok",
            "status": health.status,
            "model": health.model_loaded,
            "attention_backend": health.attention_backend,
        }
    except Exception:
        return {"online": False, "status": "offline"}


def _metadata_lock(project_id: str) -> threading.Lock:
    with _metadata_locks_guard:
        return _metadata_locks.setdefault(project_id, threading.Lock())


def _metadata_cache_key(metadata: dict[str, Any]) -> str:
    return fingerprint(
        {
            "title": str(metadata.get("title") or "").strip().casefold(),
            "author": str(metadata.get("author") or "").strip().casefold(),
            "language": str(metadata.get("language") or "").strip().casefold(),
        }
    )


def _metadata_candidate_cover(project_dir: Path) -> Path | None:
    for suffix in (".jpg", ".png"):
        candidate = project_dir / f"metadata_candidate{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _read_metadata_candidate(
    project_dir: Path,
    cache_key: str,
) -> dict[str, Any] | None:
    candidate_path = project_dir / "metadata_candidate.json"
    if not candidate_path.is_file():
        return None
    try:
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(candidate["fetched_at"])
        cache_hours = float(
            load_config().get("metadata", {}).get("cache_hours", 24)
        )
        age_seconds = (datetime.now(timezone.utc) - fetched_at).total_seconds()
        if (
            candidate.get("status") != "matched"
            or candidate.get("cache_key") != cache_key
            or cache_hours <= 0
            or age_seconds > cache_hours * 3600
        ):
            return None
        if candidate.get("has_cover") and not _metadata_candidate_cover(project_dir):
            candidate["has_cover"] = False
        candidate["cached"] = True
        return candidate
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        logger.warning("Ignoring invalid metadata candidate in %s", project_dir)
        return None


def _persist_metadata_candidate(
    project_dir: Path,
    fetched: Any,
    cache_key: str,
) -> dict[str, Any]:
    candidate = fetched.serializable()
    candidate.update(
        {
            "cache_key": cache_key,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "cached": False,
        }
    )
    if fetched.cover_image_bytes:
        extension = ".png" if fetched.cover_mime_type == "image/png" else ".jpg"
        atomic_write_bytes(
            project_dir / f"metadata_candidate{extension}",
            fetched.cover_image_bytes,
        )
        other_extension = ".jpg" if extension == ".png" else ".png"
        (project_dir / f"metadata_candidate{other_extension}").unlink(missing_ok=True)
    else:
        for stale_suffix in (".jpg", ".png"):
            (project_dir / f"metadata_candidate{stale_suffix}").unlink(missing_ok=True)
    atomic_write_json(project_dir / "metadata_candidate.json", candidate)
    return candidate


def _existing_project_cover(project_dir: Path, metadata: dict[str, Any]) -> Path | None:
    raw_path = metadata.get("cover_image_path")
    if raw_path:
        configured = Path(str(raw_path))
        candidates = [configured]
        if not configured.is_absolute():
            candidates.extend((project_dir / configured, project_dir / configured.name))
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                if resolved.is_relative_to(project_dir.resolve()) and resolved.is_file():
                    return resolved
            except OSError:
                continue
    for name in ("cover.jpg", "cover.png"):
        candidate = project_dir / name
        if candidate.is_file():
            return candidate
    return None


def _metadata_response(
    project_id: str,
    candidate: dict[str, Any],
    metadata: dict[str, Any],
    *,
    applied: bool = False,
) -> dict[str, Any]:
    payload = dict(candidate)
    payload.pop("cache_key", None)
    payload["applied"] = applied
    payload["existing_cover"] = _existing_project_cover(
        _project_dir(project_id), metadata
    ) is not None
    payload["cover_preview_url"] = (
        f"api/projects/{project_id}/metadata-candidate/cover"
        if candidate.get("has_cover")
        else None
    )
    return payload


def _exported_audiobook_paths(project_id: str) -> list[Path]:
    """Return unique existing full/partial audiobook exports for a project."""
    candidates = [
        *_project_dir(project_id).glob("*.m4b"),
        *(_workspace_project_dir(project_id) / "output").glob("*.m4b"),
    ]
    unique: dict[str, Path] = {}
    for candidate in candidates:
        if candidate.is_file():
            unique[str(candidate.resolve()).casefold()] = candidate.resolve()
    return sorted(unique.values(), key=lambda path: str(path).casefold())


def _refresh_exported_audiobook_metadata(
    project_id: str,
    metadata: dict[str, Any],
) -> list[str]:
    """Atomically remux existing M4Bs with current tags/cover, copying audio."""
    exports = _exported_audiobook_paths(project_id)
    if not exports:
        return []
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "Book details were saved, but FFmpeg is unavailable to refresh "
            "the existing audiobook package"
        )
    project_dir = _project_dir(project_id)
    cover = _existing_project_cover(project_dir, metadata)
    refreshed: list[str] = []
    for export in exports:
        temporary = export.with_name(f".{export.name}.{uuid.uuid4().hex}.metadata.m4b")
        command = [ffmpeg, "-y", "-i", str(export)]
        if cover:
            command.extend(["-i", str(cover), "-map", "0:a", "-map", "1:v:0"])
            command.extend(["-disposition:v:0", "attached_pic"])
        else:
            command.extend(["-map", "0"])
        command.extend(
            [
                "-map_metadata", "0",
                "-map_chapters", "0",
                "-c", "copy",
                "-metadata", f"title={metadata.get('title') or ''}",
                "-metadata", f"artist={metadata.get('author') or ''}",
                "-metadata", f"album={metadata.get('title') or ''}",
                "-metadata", f"genre={metadata.get('genre') or ''}",
                "-metadata", f"date={metadata.get('year') or ''}",
                "-metadata", f"comment={metadata.get('description') or ''}",
            ]
        )
        isbn = str(metadata.get("isbn") or "").strip()
        if isbn:
            # M4B has no universally supported ISBN atom. The standard
            # grouping atom survives across players without hiding cover art.
            command.extend([
                "-metadata", f"isbn={isbn}",
                "-metadata", f"grouping=ISBN {isbn}",
            ])
        command.extend(["-movflags", "+faststart", str(temporary)])
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
                detail = (result.stderr or "FFmpeg produced no output")[-1200:]
                raise RuntimeError(f"Could not refresh audiobook metadata: {detail}")
            os.replace(temporary, export)
            refreshed.append(str(export))
        finally:
            temporary.unlink(missing_ok=True)
    return refreshed


def _fetch_metadata_sync(
    project_id: str,
    *,
    refresh: bool = False,
    apply: bool = False,
    replace_cover: bool = False,
    only_missing: bool = False,
    provider_id: str | None = None,
    query_title: str | None = None,
    query_author: str | None = None,
) -> dict[str, Any]:
    """Fetch/cache a candidate and optionally merge it into book.json."""
    from brain.extractor.metadata_fetcher import MetadataFetcher

    project_dir = _project_dir(project_id)
    book_json = project_dir / "book.json"
    if not book_json.is_file():
        raise FileNotFoundError("Project book.json not found")

    with _metadata_lock(project_id):
        book_data = json.loads(book_json.read_text(encoding="utf-8"))
        metadata = book_data.setdefault("metadata", {})
        cache_key = _metadata_cache_key(metadata)
        candidate = (
            None if refresh or provider_id
            else _read_metadata_candidate(project_dir, cache_key)
        )
        if provider_id:
            fetched = MetadataFetcher.fetch_volume(
                provider_id,
                str(query_title or metadata.get("title", "")).strip(),
                str(query_author or metadata.get("author", "")).strip(),
            )
            if fetched.status != "matched":
                return fetched.serializable()
            candidate = _persist_metadata_candidate(project_dir, fetched, cache_key)
        elif candidate is None:
            fetched = MetadataFetcher.fetch(
                metadata.get("title", ""),
                metadata.get("author", ""),
                metadata.get("language", ""),
            )
            if fetched.status != "matched":
                return fetched.serializable()
            candidate = _persist_metadata_candidate(project_dir, fetched, cache_key)

        if not apply:
            return _metadata_response(project_id, candidate, metadata)

        # Automatic enrichment preserves the EPUB identity. Explicit human
        # approval applies the reviewed title/author and retains source values
        # as provenance for support and recovery.
        if not only_missing:
            metadata.setdefault("source_title", metadata.get("title", ""))
            metadata.setdefault("source_author", metadata.get("author", ""))
            if candidate.get("title"):
                metadata["title"] = candidate["title"]
            if candidate.get("author"):
                metadata["author"] = candidate["author"]
        for key in ("description", "isbn", "genre", "year"):
            if candidate.get(key) and (not only_missing or not metadata.get(key)):
                metadata[key] = candidate[key]
        metadata.update(
            {
                "metadata_provider": candidate.get("provider", ""),
                "metadata_provider_id": candidate.get("provider_id", ""),
                "metadata_confidence": candidate.get("confidence"),
                "metadata_fetched_at": candidate.get("fetched_at", ""),
            }
        )
        candidate_cover = _metadata_candidate_cover(project_dir)
        current_cover = _existing_project_cover(project_dir, metadata)
        if candidate_cover and (replace_cover or current_cover is None):
            destination = project_dir / f"cover{candidate_cover.suffix.lower()}"
            atomic_write_bytes(destination, candidate_cover.read_bytes())
            other = project_dir / ("cover.png" if destination.suffix == ".jpg" else "cover.jpg")
            if other != current_cover:
                other.unlink(missing_ok=True)
            metadata["cover_image_path"] = str(destination)
        atomic_write_json(book_json, book_data)
        try:
            refreshed_exports = _refresh_exported_audiobook_metadata(
                project_id, metadata
            )
        except Exception:
            if job_queue:
                try:
                    job_queue.update_job(
                        project_id, {"export_metadata_stale": True}
                    )
                except KeyError:
                    pass
            raise
        if job_queue:
            try:
                job_queue.update_job(
                    project_id,
                    {
                        "title": metadata.get("title", ""),
                        "author": metadata.get("author", ""),
                        "export_metadata_stale": False,
                        "export_metadata_refreshed_at": datetime.now(
                            timezone.utc
                        ).isoformat(),
                    },
                )
            except KeyError:
                pass
        response = _metadata_response(
            project_id, candidate, metadata, applied=True
        )
        response["refreshed_exports"] = len(refreshed_exports)
        return response


def _search_metadata_sync(
    project_id: str,
    title: str,
    author: str = "",
) -> dict[str, Any]:
    """Search external metadata without changing the project's cached match."""
    from brain.extractor.metadata_fetcher import MetadataFetcher

    book_json = _project_dir(project_id) / "book.json"
    if not book_json.is_file():
        raise FileNotFoundError("Project book.json not found")
    book_data = json.loads(book_json.read_text(encoding="utf-8"))
    language = str((book_data.get("metadata") or {}).get("language") or "")
    return MetadataFetcher.search(title, author, language).serializable()


def _auto_fetch_metadata_sync(project_id: str) -> None:
    """Best-effort metadata enrichment that preserves embedded cover art."""
    try:
        result = _fetch_metadata_sync(
            project_id,
            apply=True,
            replace_cover=False,
            only_missing=True,
        )
        if result.get("status") == "matched":
            logger.info("Auto-fetched metadata for project %s", project_id)
        else:
            logger.warning(
                "Auto metadata lookup for %s returned %s: %s",
                project_id,
                result.get("status"),
                result.get("error"),
            )
    except Exception as exc:
        logger.warning("Auto metadata fetch failed for %s: %s", project_id, exc)


@app.post("/api/projects/{project_id}/fetch-metadata")
async def fetch_project_metadata(
    project_id: str,
    request: MetadataFetchRequest,
):
    """Preview or apply a validated external metadata candidate."""
    if not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    try:
        result = await asyncio.to_thread(
            _fetch_metadata_sync,
            project_id,
            refresh=request.refresh,
            apply=request.apply,
            replace_cover=request.replace_cover,
            provider_id=request.provider_id,
            query_title=request.query_title,
            query_author=request.query_author,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result.get("status") == "provider_error":
        raise HTTPException(
            status_code=502,
            detail=f"Metadata provider unavailable: {result.get('error') or 'unknown error'}",
        )
    if result.get("status") == "no_match":
        raise HTTPException(
            status_code=404,
            detail=result.get("error") or "No close metadata match was found",
        )
    return result


@app.post("/api/projects/{project_id}/search-metadata")
async def search_project_metadata(
    project_id: str,
    request: MetadataSearchRequest,
):
    """Return ranked Google Books candidates for explicit human selection."""
    if not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    try:
        result = await asyncio.to_thread(
            _search_metadata_sync,
            project_id,
            request.title,
            request.author,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result.get("status") == "provider_error":
        raise HTTPException(
            status_code=502,
            detail=f"Metadata provider unavailable: {result.get('error') or 'unknown error'}",
        )
    return result


@app.get("/api/projects/{project_id}/metadata-candidate/cover")
async def get_metadata_candidate_cover(project_id: str):
    project_dir = _project_dir(project_id)
    with _metadata_lock(project_id):
        cover = _metadata_candidate_cover(project_dir)
        if not cover:
            raise HTTPException(status_code=404, detail="Metadata candidate has no cover")
        media_type = "image/png" if cover.suffix.lower() == ".png" else "image/jpeg"
        return Response(content=cover.read_bytes(), media_type=media_type)


@app.get("/api/projects/{project_id}/cover")
async def get_project_cover(project_id: str):
    project_dir = _project_dir(project_id)
    book_json = project_dir / "book.json"
    if not book_json.is_file():
        raise HTTPException(status_code=404, detail="Project book.json not found")
    book_data = json.loads(book_json.read_text(encoding="utf-8"))
    cover = _existing_project_cover(project_dir, book_data.get("metadata", {}))
    if not cover:
        raise HTTPException(status_code=404, detail="Project has no cover")
    media_type = "image/png" if cover.suffix.lower() == ".png" else "image/jpeg"
    return FileResponse(cover, media_type=media_type)


@app.post("/api/projects/{project_id}/request-deploy")
async def request_deploy_pause(project_id: str):
    """Request pipeline to park at next chapter boundary for safe deployment."""
    if not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    job_queue.update_job(project_id, {"deployment_requested": True})
    return {"status": "success", "project_id": project_id, "deployment_requested": True}


@app.post("/api/projects/{project_id}/resume-deploy")
async def resume_from_deploy_pause(project_id: str):
    """Resume pipeline from safe deployment parking point."""
    if not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    job_queue.update_job(project_id, {"deployment_requested": False})
    return {"status": "success", "project_id": project_id, "deployment_requested": False}


@app.post("/api/projects/{project_id}/set-selection")
async def set_chapter_selection(
    project_id: str,
    request: ChapterSelectionRequest,
):
    """Set which chapters to generate in the next run."""
    if not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    state = job_queue.get_job(project_id)
    selection = request.chapters
    if selection is not None:
        selection = sorted(set(selection))
        total = int(state.get("total_chapters") or 0)
        invalid = [
            chapter for chapter in selection if chapter < 1 or chapter > total
        ]
        if invalid:
            raise HTTPException(
                status_code=422,
                detail=f"Chapter selection is out of range: {invalid}",
            )
    job_queue.update_job(project_id, {"generation_chapter_selection": selection})
    return {"status": "success", "project_id": project_id, "selection": selection}


@app.post("/api/projects/{project_id}/export-partial")
async def export_partial_m4b(project_id: str):
    """Trigger a partial M4B export with all currently mastered chapters."""
    if not pipeline:
        raise HTTPException(status_code=503, detail="Server not initialized")
    project_dir = _project_dir(project_id)
    await asyncio.to_thread(pipeline._run_export, project_id, project_dir, partial=True)
    return {"status": "success", "project_id": project_id}


# ---------------------------------------------------------------------------
# Log streaming (SSE)
# ---------------------------------------------------------------------------


@app.get("/api/projects/{project_id}/logs")
async def get_log_history(project_id: str):
    """Return all buffered log lines for a project (up to last 500)."""
    lines = list(_project_logs.get(project_id, []))
    if not lines:
        log_file = _project_dir(project_id) / "pipeline.log"
        if log_file.exists():
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    all_lines = [line.rstrip() for line in f if line.strip()]
                    lines = all_lines[-500:]
                    # Hydrate RAM buffer
                    _project_logs[project_id] = collections.deque(lines, maxlen=500)
            except Exception:
                pass
    return {"project_id": project_id, "lines": lines}


@app.get("/api/projects/{project_id}/logs/stream")
async def stream_logs(project_id: str, request: Request):
    """SSE endpoint — streams live log lines for a running pipeline."""
    if project_id not in _log_subscribers:
        _log_subscribers[project_id] = []

    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _log_subscribers[project_id].append(q)

    # First, replay any buffered lines so the client catches up
    buffered = list(_project_logs.get(project_id, []))

    async def event_generator():
        try:
            # Replay history
            for line in buffered:
                yield f"data: {line}\n\n"

            # Stream live
            while True:
                if await request.is_disconnected():
                    break
                try:
                    line = await asyncio.wait_for(q.get(), timeout=15.0)
                    if line is None:  # sentinel: pipeline finished
                        yield "data: [PIPELINE ENDED]\n\n"
                        break
                    yield f"data: {line}\n\n"
                except asyncio.TimeoutError:
                    yield "data: \n\n"  # heartbeat keep-alive
        finally:
            try:
                _log_subscribers[project_id].remove(q)
            except ValueError:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Script & quality data
# ---------------------------------------------------------------------------


@app.get("/api/projects/{project_id}/script")
async def get_script(project_id: str):
    """Get the generated script for a project."""
    script_path = _project_dir(project_id) / "book_script.json"
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="Script not generated yet")
    return FileResponse(str(script_path), media_type="application/json")


@app.get("/api/projects/{project_id}/characters")
async def get_characters(project_id: str):
    """Get the character registry for a project."""
    chars_path = _project_dir(project_id) / "characters.json"
    if not chars_path.exists():
        raise HTTPException(status_code=404, detail="Characters not analyzed yet")
    return FileResponse(str(chars_path), media_type="application/json")


@app.get("/api/projects/{project_id}/voices")
async def get_project_voices(project_id: str):
    """List only speaking cast members and their assignable references."""
    _, registry = _load_character_registry(project_id)
    characters = registry["characters"]
    cast = _load_or_build_voice_cast(project_id, registry)
    speaking_ids = set(cast.get("speaking_characters", []))
    voice_dir = _voice_project_dir(project_id)
    voice_registry_path = voice_dir / "voices.json"
    try:
        registered = (
            json.loads(voice_registry_path.read_text(encoding="utf-8"))
            .get("voices", {})
            if voice_registry_path.exists()
            else {}
        )
    except (OSError, json.JSONDecodeError):
        registered = {}

    voices = []
    for voice_id, profile in sorted(cast.get("voices", {}).items()):
        owner = characters.get(voice_id, {})
        info = registered.get(voice_id, {})
        actual_file = info.get("file", f"{voice_id}.wav")
        preview_path = voice_dir / actual_file
        assigned_characters = sorted(
            character_id
            for character_id in profile.get("assigned_characters", [])
            if character_id in speaking_ids
        )
        voices.append(
            {
                "voice_id": voice_id,
                "name": profile.get("name") or owner.get("name") or info.get("name") or voice_id,
                "gender": profile.get("gender") or owner.get("gender", "other"),
                "age_range": profile.get("age_range")
                or owner.get("age_range", "unknown"),
                "source_description": profile.get("source_description", ""),
                "description": profile.get("effective_prompt")
                or info.get("description")
                or "",
                "warnings": profile.get("warnings", []),
                "source_type": info.get("source_type", "generated"),
                "ref_text": info.get("ref_text", ""),
                "ready": preview_path.is_file(),
                "preview_url": (
                    f"api/projects/{project_id}/voices/{voice_id}/preview?v={int(preview_path.stat().st_mtime)}"
                    if preview_path.is_file()
                    else None
                ),
                "download_url": (
                    f"api/projects/{project_id}/voices/{voice_id}/download"
                    if preview_path.is_file()
                    else None
                ),
                "assigned_characters": assigned_characters,
                "required": bool(assigned_characters),
                "owner_character_id": profile.get("owner_character_id") or voice_id,
            }
        )

    running = False
    if job_queue:
        try:
            running = bool(job_queue.get_job(project_id).get("running"))
        except KeyError:
            pass
    state: dict[str, Any] = {}
    if job_queue:
        try:
            state = job_queue.get_job(project_id)
        except KeyError:
            pass
    speaking_characters = [
        {
            "character_id": character_id,
            "name": characters[character_id].get("name") or character_id,
            "gender": characters[character_id].get("gender", "other"),
            "age_range": characters[character_id].get("age_range", "unknown"),
            "voice_id": (
                characters[character_id].get("voice_id") or character_id
            ),
        }
        for character_id in sorted(speaking_ids)
        if character_id in characters
    ]
    narrator_options = [
        voice for voice in voices if voice.get("owner_character_id") == "narrator"
    ]
    narrator_selected = next(
        (voice["voice_id"] for voice in narrator_options if voice["assigned_characters"]),
        None,
    )
    return {
        "cast_schema": VOICE_CAST_SCHEMA_VERSION,
        "voices": voices,
        "speaking_characters": speaking_characters,
        "non_speaking_count": len(set(characters) - speaking_ids),
        "narrator_choice": (
            {
                "character_id": "narrator",
                "selected_voice_id": narrator_selected,
                "options": narrator_options,
            }
            if narrator_options
            else None
        ),
        "editable": not running,
        "review": {
            "policy": state.get("voice_review_policy", "grandfathered"),
            "status": state.get(
                "voice_review_status", "grandfathered"
            ),
            "approved_at": state.get("voice_review_approved_at"),
            "required": (
                (
                    state.get("voice_review_policy", "grandfathered")
                    == "required_once"
                    and state.get("voice_review_status") != "approved"
                )
                or state.get("active_stage") == "voice_review"
                or state.get("pause_reason") == "voice_review"
                or not state.get("voice_review_approved", True)
            ),
        },
        "change_policy": (
            "Preview at any stage. Pause at a safe boundary to reassign or "
            "redesign a voice. Affected chapters must then be regenerated."
        ),
    }


@app.get("/api/projects/{project_id}/voices/download-all")
async def download_all_project_voices(project_id: str):
    """Download every prepared cast reference as one reusable ZIP bundle."""
    state = _require_job(project_id)
    cast = _load_or_build_voice_cast(project_id)
    book_name = _download_name_component(
        str(state.get("title") or project_id),
        "Untitled book",
    )
    archive = io.BytesIO()
    manifest: list[dict[str, Any]] = []
    used_names: set[str] = set()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for voice_id, profile in sorted(cast.get("voices", {}).items()):
            try:
                voice_path, info = _registered_voice_path(project_id, voice_id)
            except HTTPException as exc:
                if exc.status_code == 404:
                    continue
                raise
            character_label = _download_name_component(
                _voice_download_label(cast, voice_id, info),
                "Narrator" if voice_id.startswith("narrator") else "Character",
            )
            archive_name = (
                f"{book_name} - {character_label} - voice-reference.wav"
            )
            if archive_name.casefold() in used_names:
                safe_voice_id = _download_name_component(voice_id, "voice")
                archive_name = (
                    f"{book_name} - {character_label} - {safe_voice_id} "
                    "- voice-reference.wav"
                )
            used_names.add(archive_name.casefold())
            bundle.write(voice_path, arcname=archive_name)
            manifest.append(
                {
                    "voice_id": voice_id,
                    "character": character_label,
                    "filename": archive_name,
                    "source_type": info.get("source_type", "generated"),
                    "reference_text": info.get("ref_text", ""),
                    "assigned_characters": profile.get("assigned_characters", []),
                }
            )
        if not manifest:
            raise HTTPException(
                status_code=404,
                detail="No prepared voice samples are available",
            )
        bundle.writestr(
            "voice-samples.json",
            json.dumps(
                {"book": book_name, "samples": manifest},
                ensure_ascii=False,
                indent=2,
            ),
        )
    filename = f"{book_name} - voice-samples.zip"
    return Response(
        content=archive.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-Voice-Sample-Count": str(len(manifest)),
        },
    )


@app.get("/api/projects/{project_id}/voices/{voice_id}/preview")
async def preview_project_voice(project_id: str, voice_id: str):
    """Stream an existing voice-reference WAV for dashboard preview."""
    cast = _load_or_build_voice_cast(project_id)
    voice_dir = _voice_project_dir(project_id)
    voice_registry_path = voice_dir / "voices.json"
    try:
        registered = (
            json.loads(voice_registry_path.read_text(encoding="utf-8")).get("voices", {})
            if voice_registry_path.exists() else {}
        )
    except (OSError, json.JSONDecodeError):
        registered = {}

    if voice_id not in cast.get("voices", {}) and voice_id not in registered:
        raise HTTPException(status_code=404, detail="Voice not found")
    actual_file = registered.get(voice_id, {}).get("file", f"{voice_id}.wav")
    voice_path = (_voice_project_dir(project_id) / actual_file).resolve()
    if not voice_path.is_relative_to(_voice_project_dir(project_id)):
        raise HTTPException(status_code=400, detail="Invalid voice ID")
    if not voice_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Voice preview is not available until voice preparation completes",
        )
    return FileResponse(
        str(voice_path),
        media_type="audio/wav",
        filename=f"{voice_id}-preview.wav",
        content_disposition_type="inline",
    )


@app.get("/api/projects/{project_id}/voices/{voice_id}/download")
async def download_project_voice(project_id: str, voice_id: str):
    """Download a reusable reference WAV named for its book and character."""
    state = _require_job(project_id)
    cast = _load_or_build_voice_cast(project_id)
    voice_path, info = _registered_voice_path(project_id, voice_id)
    book_name = _download_name_component(
        str(state.get("title") or project_id),
        "Untitled book",
    )
    character_name = _download_name_component(
        _voice_download_label(cast, voice_id, info),
        "Narrator" if voice_id.startswith("narrator") else "Character",
    )
    return FileResponse(
        voice_path,
        media_type="audio/wav",
        filename=f"{book_name} - {character_name} - voice-reference.wav",
        content_disposition_type="attachment",
    )


@app.patch("/api/projects/{project_id}/characters/{character_id}/voice")
async def assign_character_voice(
    project_id: str,
    character_id: str,
    request: VoiceAssignmentRequest,
):
    """Assign a character to an existing or pending reference-voice owner."""
    _ensure_voice_editable(project_id)
    chars_path, registry = _load_character_registry(project_id)
    characters = registry["characters"]
    if character_id not in characters:
        raise HTTPException(status_code=404, detail="Character not found")
    cast = _load_or_build_voice_cast(project_id, registry)
    if character_id not in set(cast.get("speaking_characters", [])):
        raise HTTPException(
            status_code=422,
            detail="Only characters with spoken script lines can receive voices",
        )

    assignable_ids = set(cast.get("voices", {}))
    if request.voice_id not in assignable_ids:
        raise HTTPException(status_code=422, detail="Selected voice is not assignable")

    previous_voice_id = (
        characters[character_id].get("voice_id") or character_id
    )
    if previous_voice_id == request.voice_id:
        return {
            "status": "unchanged",
            "character_id": character_id,
            "voice_id": request.voice_id,
            "affected_chapters": [],
        }

    characters[character_id]["voice_id"] = request.voice_id
    atomic_write_json(chars_path, registry)
    for profile in cast.get("voices", {}).values():
        assigned = [
            candidate
            for candidate in profile.get("assigned_characters", [])
            if candidate != character_id
        ]
        if profile.get("voice_id") == request.voice_id:
            assigned.append(character_id)
        profile["assigned_characters"] = sorted(set(assigned))
    _save_voice_cast(project_id, cast)
    affected = _chapters_for_speakers(project_id, {character_id})
    _mark_voice_chapters_stale(project_id, affected)
    logger.info(
        "Character voice reassigned: project=%s character=%s %s -> %s; "
        "affected_chapters=%s",
        project_id,
        character_id,
        previous_voice_id,
        request.voice_id,
        affected,
    )
    return {
        "status": "updated",
        "character_id": character_id,
        "voice_id": request.voice_id,
        "previous_voice_id": previous_voice_id,
        "affected_chapters": affected,
    }


@app.post("/api/projects/{project_id}/voices/{voice_id}/regenerate")
async def regenerate_project_voice(
    project_id: str,
    voice_id: str,
    request: VoiceRegenerationRequest,
):
    """Redesign one reference voice and invalidate only its dependent chapters."""
    _ensure_voice_editable(project_id)
    if not pipeline:
        raise HTTPException(status_code=503, detail="Server not initialized")
    chars_path, registry_data = _load_character_registry(project_id)
    from shared.models import BootstrapVoicesRequest, CharacterRegistry, Gender

    registry = CharacterRegistry.model_validate(registry_data)
    cast = _load_or_build_voice_cast(project_id, registry_data)
    if voice_id not in cast.get("voices", {}):
        raise HTTPException(status_code=404, detail="Voice owner not found")
    owner_id = cast["voices"][voice_id].get("owner_character_id", voice_id)
    if owner_id not in registry.characters:
        raise HTTPException(status_code=500, detail="Voice owner registry is invalid")
    owner = registry.characters[owner_id]
    if owner_id != "narrator" and (owner.voice_id or owner_id) != owner_id:
        raise HTTPException(
            status_code=422,
            detail="Redesign the owning voice rather than a shared assignment",
        )

    profile = cast["voices"][voice_id]
    profile_gender = Gender(profile.get("gender", owner.gender.value))
    effective_prompt, prompt_warnings = compile_effective_voice_prompt(
        gender=profile_gender,
        age_range=owner.age_range,
        source_description=request.voice_description.strip(),
        speaking_style=owner.speaking_style,
    )
    design_fingerprint = fingerprint(
        {
            "schema": cast.get("schema", "1"),
            "voice_id": voice_id,
            "gender": profile_gender.value,
            "age_range": owner.age_range,
            "effective_prompt": effective_prompt,
            "test_sentence": profile.get("test_sentence") or owner.test_sentence,
            "design_model": profile.get("design_model", ""),
            "design_config": profile.get("design_config", {}),
        }
    )
    request_character = owner.model_copy(
        update={
            "id": voice_id,
            "name": profile.get("name") or owner.name,
            "gender": profile_gender,
            "voice_description": effective_prompt,
            "test_sentence": profile.get("test_sentence") or owner.test_sentence,
        }
    )
    managed_before = getattr(pipeline, "_voice_server_proc", None)
    try:
        await asyncio.to_thread(pipeline._start_voice_server)
        response = await asyncio.to_thread(
            pipeline.voice_client.bootstrap_voices,
            BootstrapVoicesRequest(
                project_id=project_id,
                characters={voice_id: request_character},
                force_regenerate=True,
                design_fingerprints={voice_id: design_fingerprint},
                candidate_counts={voice_id: 1},
            ),
        )
    except Exception as exc:
        logger.exception(
            "Voice regeneration failed: project=%s voice=%s",
            project_id,
            voice_id,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Voice regeneration failed: {exc}",
        ) from exc
    finally:
        if (
            managed_before is None
            and getattr(pipeline, "_voice_server_proc", None) is not None
        ):
            await asyncio.to_thread(pipeline._stop_voice_server)

    if voice_id == owner_id:
        registry.characters[owner_id] = owner.model_copy(
            update={"voice_description": request.voice_description.strip()}
        )
        atomic_write_json(
            chars_path,
            registry.model_dump(mode="json"),
        )
    profile.update(
        {
            "source_description": request.voice_description.strip(),
            "effective_prompt": effective_prompt,
            "warnings": prompt_warnings,
            "design_fingerprint": design_fingerprint,
            "source_type": "generated",
        }
    )
    _save_voice_cast(project_id, cast)
    dependent_speakers = set(profile.get("assigned_characters", []))
    affected = _chapters_for_speakers(project_id, dependent_speakers)
    _mark_voice_chapters_stale(project_id, affected)
    logger.info(
        "Reference voice regenerated: project=%s voice=%s "
        "affected_chapters=%s",
        project_id,
        voice_id,
        affected,
    )
    return {
        "status": "success",
        "voice_id": voice_id,
        "affected_chapters": affected,
        "preview_url": (
            f"api/projects/{project_id}/voices/{voice_id}/preview"
            f"?v={int(time.time())}"
        ),
        "result": response.voices_generated.get(voice_id),
    }


@app.post("/api/projects/{project_id}/voices/{voice_id}/upload")
async def upload_project_voice(
    project_id: str,
    voice_id: str,
    file: UploadFile = File(...),
    transcript: str = Form(...),
):
    """Replace a speaking voice with a validated user-supplied reference."""
    _ensure_voice_editable(project_id)
    transcript = re.sub(r"\s+", " ", transcript).strip()
    if len(transcript) < 3 or len(transcript) > 2000:
        raise HTTPException(
            status_code=422,
            detail="Provide the exact spoken transcript (3-2000 characters)",
        )

    chars_path, registry = _load_character_registry(project_id)
    cast = _load_or_build_voice_cast(project_id, registry)
    if voice_id not in cast.get("voices", {}):
        raise HTTPException(
            status_code=404,
            detail="Only voices used by speaking characters can be replaced",
        )
    owner_id = cast["voices"][voice_id].get("owner_character_id", voice_id)
    owner = registry["characters"].get(owner_id)
    if not owner:
        raise HTTPException(status_code=500, detail="Voice owner is invalid")

    extension = Path(file.filename or "").suffix.lower()
    if extension not in {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg"}:
        raise HTTPException(
            status_code=400,
            detail="Supported voice files: WAV, FLAC, MP3, M4A, AAC, OGG",
        )
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise HTTPException(
            status_code=503,
            detail="FFmpeg is required to import reference audio",
        )

    voice_dir = _voice_project_dir(project_id)
    voice_dir.mkdir(parents=True, exist_ok=True)
    raw_path = voice_dir / f".{voice_id}-{uuid.uuid4().hex}{extension}"
    canonical_path = voice_dir / f".{voice_id}-{uuid.uuid4().hex}.wav"
    backup_path = voice_dir / f".{voice_id}-{uuid.uuid4().hex}.backup.wav"
    max_bytes = 25 * 1024 * 1024
    try:
        total_bytes = 0
        with raw_path.open("xb") as handle:
            while chunk := await file.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise ValueError("Voice upload exceeds the 25 MB limit")
                handle.write(chunk)

        # Run FFmpeg in a thread so the event loop stays responsive
        # (prevents /health from timing out while converting audio).
        result = await asyncio.to_thread(
            subprocess.run,
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(raw_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "24000",
                "-c:a",
                "pcm_s16le",
                str(canonical_path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0 or not canonical_path.is_file():
            raise ValueError(
                "FFmpeg could not decode the uploaded audio: "
                + (result.stderr.strip()[-500:] or "unknown format")
            )
        audio_info = _inspect_pcm_voice(canonical_path)

        # Phase 3.3: Validate uploaded reference sample transcription
        # Run Whisper validation in a thread so the server stays responsive
        # during the 30-90 second model load + inference.
        import tempfile
        import sys
        import json
        val_script = """
import sys, json
from voice.validator.whisper_validator import WhisperValidator
try:
    val = WhisperValidator(
        model_name="large-v3",
        device="auto",
        backend=os.environ.get(
            "CRAZY_AUDIOBOOK_WHISPER_BACKEND",
            "openai_whisper",
        ),
    )
    transcribed = val.transcribe(sys.argv[1])
    wer = val.calculate_wer(sys.argv[2], transcribed)
    print(json.dumps({"wer": float(wer), "transcribed_text": transcribed}))
except Exception as e:
    print(json.dumps({"error": str(e)}))
"""
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(val_script)
            script_path = f.name
        
        try:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent.parent.parent)
            proc = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, script_path, str(canonical_path), transcript],
                capture_output=True,
                text=True,
                timeout=180,
                env=env,
            )
            if proc.returncode != 0:
                logger.error("Whisper validation script failed: %s", proc.stderr)
                raise ValueError(f"Whisper validation failed: {proc.stderr[-200:]}")
                
            try:
                val_res = json.loads(proc.stdout)
                if "error" in val_res:
                    raise ValueError(f"Whisper validation script error: {val_res['error']}")
                else:
                    class DummyResult:
                        wer = val_res["wer"]
                        transcribed_text = val_res["transcribed_text"]
                        effective_text_error = val_res["wer"]
                    
                    mismatch_err = _uploaded_transcript_error(DummyResult())
                    if mismatch_err:
                        raise ValueError(mismatch_err)
            except json.JSONDecodeError:
                logger.error("Could not decode validator output: %s", proc.stdout)
                raise ValueError("Whisper validator returned invalid format.")
        finally:
            Path(script_path).unlink(missing_ok=True)

        target_path = voice_dir / f"{voice_id}_{uuid.uuid4().hex[:8]}.wav"
        
        # We do not overwrite the exact same filename to prevent WinError 5 locking.
        os.replace(canonical_path, target_path)
        try:
            profile = cast["voices"][voice_id]
            reference_fingerprint = fingerprint(
                {
                    "source_type": "uploaded",
                    "audio_hash": hash_file(target_path),
                    "ref_text": transcript,
                }
            )
            library = VoiceLibraryManager(voice_dir.parent)
            library.register_voice(
                project_id=project_id,
                character_id=voice_id,
                name=str(owner.get("name") or voice_id),
                description=str(profile.get("effective_prompt") or ""),
                gender=str(owner.get("gender") or "other"),
                file_path=str(target_path),
                duration_seconds=float(audio_info["duration_seconds"]),
                sample_rate=int(audio_info["sample_rate"]),
                ref_text=transcript,
                design_fingerprint=reference_fingerprint,
                source_type="uploaded",
                source_filename=Path(file.filename or "uploaded audio").name,
            )
        except Exception:
            target_path.unlink(missing_ok=True)
            if backup_path.exists():
                os.replace(backup_path, target_path)
            raise
        backup_path.unlink(missing_ok=True)
        profile["source_type"] = "uploaded"
        profile["design_fingerprint"] = reference_fingerprint
        _save_voice_cast(project_id, cast)

        dependent_speakers = set(profile.get("assigned_characters", []))
        affected = _chapters_for_speakers(project_id, dependent_speakers)
        _mark_voice_chapters_stale(project_id, affected)
        return {
            "status": "success",
            "voice_id": voice_id,
            "source_type": "uploaded",
            "duration_seconds": audio_info["duration_seconds"],
            "affected_chapters": affected,
            "preview_url": (
                f"api/projects/{project_id}/voices/{voice_id}/preview"
                f"?v={int(time.time())}"
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            status_code=504,
            detail="Voice conversion exceeded two minutes",
        ) from exc
    finally:
        raw_path.unlink(missing_ok=True)
        canonical_path.unlink(missing_ok=True)
        backup_path.unlink(missing_ok=True)


@app.post("/api/projects/{project_id}/voice-review/approve")
async def approve_voice_cast(
    project_id: str,
    request: VoiceApprovalRequest,
):
    """Approve the speaking cast once and optionally continue generation."""
    state = _ensure_voice_editable(project_id)
    cast = _load_or_build_voice_cast(project_id)
    if state.get("voice_review_policy", "grandfathered") != "required_once":
        return {
            "status": "grandfathered",
            "project_id": project_id,
            "continued": False,
        }

    voice_dir = _voice_project_dir(project_id)
    try:
        registered = json.loads((voice_dir / "voices.json").read_text(encoding="utf-8")).get("voices", {})
    except (OSError, json.JSONDecodeError):
        registered = {}

    required_voice_ids = {
        voice_id
        for voice_id, profile in cast.get("voices", {}).items()
        if profile.get("assigned_characters")
    }
    missing = [
        voice_id
        for voice_id in required_voice_ids
        if not (voice_dir / registered.get(voice_id, {}).get("file", f"{voice_id}.wav")).is_file()
    ]
    if missing:
        raise HTTPException(
            status_code=409,
            detail=(
                "Every speaking voice needs a valid preview before approval. "
                f"Missing: {', '.join(sorted(missing))}"
            ),
        )

    approved_at = datetime.now(timezone.utc).isoformat()
    job_queue.update_job(
        project_id,
        {
            **_voice_review_approval_update(
                approved_at,
                str(cast.get("fingerprint") or ""),
            ),
            "pause_reason": None,
        },
    )
    if request.continue_pipeline:
        started = await start_pipeline(project_id)
        return {
            "status": "approved",
            "project_id": project_id,
            "approved_at": approved_at,
            "continued": True,
            "pipeline": started,
        }
    return {
        "status": "approved",
        "project_id": project_id,
        "approved_at": approved_at,
        "continued": False,
    }


@app.get("/api/system/preflight")
async def runtime_preflight():
    """Return a read-only compatibility report without importing GPU models."""
    return collect_runtime_report()


@app.get("/api/projects/{project_id}/metrics")
async def get_performance_metrics(
    project_id: str,
    format: Literal["json", "csv"] = "json",
):
    """Return a deterministic summary of versioned project performance data."""
    _require_job(project_id)
    project_dir = _project_dir(project_id)
    summary = summarize_metrics(
        read_metrics(project_dir / "performance_metrics.jsonl")
    )
    if format == "json":
        return summary
    buffer = io.StringIO()
    fields = [
        "chapter_number", "segments", "synthesis_cache_hits",
        "synthesis_cache_misses", "validation_cache_hits",
        "validation_cache_misses", "retries", "accepted_with_warning",
        "failed_validation", "audio_duration_seconds",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(summary["chapters"])
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{project_id}-metrics.csv"'
        },
    )


@app.get("/api/projects/{project_id}/storage")
async def get_project_storage(project_id: str):
    """Report storage by artifact class and preview safe temp cleanup."""
    _require_job(project_id)
    project_dir = _project_dir(project_id)
    workspace_dir = _workspace_project_dir(project_id)
    categories = {
        "source_and_state": _path_inventory(project_dir),
        "segments": _path_inventory(workspace_dir / "segments"),
        "mastered_chapters": _path_inventory(workspace_dir / "chapters"),
        "exports": _path_inventory(workspace_dir / "output"),
        "voice_library": _path_inventory(_voice_project_dir(project_id)),
    }
    candidates = _cleanup_candidates(project_id)
    return {
        "categories": categories,
        "total_bytes": sum(item["bytes"] for item in categories.values()),
        "cleanup_preview": {
            "files": [str(path) for path in candidates],
            "bytes": sum(path.stat().st_size for path in candidates),
            "confirmation_token": _cleanup_token(candidates),
            "scope": "incomplete retry and temporary files only",
        },
    }


@app.post("/api/projects/{project_id}/storage/cleanup")
async def cleanup_project_storage(project_id: str, request: CleanupRequest):
    """Delete only an unchanged, previewed set of incomplete temp artifacts."""
    _require_job(project_id)
    candidates = _cleanup_candidates(project_id)
    expected = _cleanup_token(candidates)
    if request.confirmation_token != expected:
        raise HTTPException(
            status_code=409,
            detail="Cleanup preview changed; refresh storage and confirm again",
        )
    removed = []
    removed_bytes = 0
    for path in candidates:
        size = path.stat().st_size
        path.unlink()
        removed.append(str(path))
        removed_bytes += size
    return {"removed": removed, "removed_bytes": removed_bytes}


@app.get("/api/projects/{project_id}/support-bundle")
async def download_support_bundle(project_id: str):
    """Create a small diagnostic bundle with secrets redacted and no audio."""
    _require_job(project_id)
    project_dir = _project_dir(project_id)
    bundle_dir = project_dir / "support"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = bundle_dir / f"{project_id}-support.zip"
    brain_config = load_config()
    voice_config = (
        yaml.safe_load(Path("voice/config.yaml").read_text(encoding="utf-8"))
        if Path("voice/config.yaml").is_file()
        else {}
    ) or {}
    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "job-state.json",
            json.dumps(job_queue.get_job(project_id), indent=2, default=str),
        )
        archive.writestr(
            "runtime-preflight.json",
            json.dumps(collect_runtime_report(), indent=2, default=str),
        )
        archive.writestr(
            "metrics-summary.json",
            json.dumps(
                summarize_metrics(
                    read_metrics(project_dir / "performance_metrics.jsonl")
                ),
                indent=2,
                default=str,
            ),
        )
        archive.writestr(
            "brain-config-redacted.json",
            json.dumps(_redact_config(brain_config), indent=2, default=str),
        )
        archive.writestr(
            "voice-config-redacted.json",
            json.dumps(_redact_config(voice_config), indent=2, default=str),
        )
        for name in ("pipeline.log", "performance_metrics.jsonl"):
            path = project_dir / name
            if path.is_file():
                data = path.read_bytes()
                archive.writestr(name, data[-1_000_000:])
        managed_log = Path("brain/projects/voice-server-managed.log")
        if managed_log.is_file():
            archive.writestr(
                "voice-server-managed.log", managed_log.read_bytes()[-1_000_000:]
            )
    return FileResponse(
        bundle_path,
        filename=bundle_path.name,
        media_type="application/zip",
    )


@app.get("/api/projects/{project_id}/segments/{line_id}/audio")
async def get_segment_audio(project_id: str, line_id: str):
    """Stream one generated segment for quality review."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", line_id):
        raise HTTPException(status_code=400, detail="Invalid line ID")
    path = _workspace_project_dir(project_id) / "segments" / f"{line_id}.wav"
    if not path.is_file():
        path = _project_dir(project_id) / "segments" / f"{line_id}.wav"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Segment audio not found")
    return FileResponse(path=path, filename=path.name, media_type="audio/wav")


@app.get("/api/projects/{project_id}/quality/review")
async def get_quality_review(project_id: str):
    """Return join diagnostics and persisted human review dispositions."""
    _require_job(project_id)
    persisted = {
        (item["item_type"], item["item_id"]): item
        for item in job_queue.get_review_items(project_id)
    }
    joins = _join_review_items(project_id)
    for item in joins:
        review = persisted.get(("join", item["item_id"]), {})
        item["disposition"] = review.get("disposition", "unreviewed")
        item["review_note"] = review.get("note", "")
        item["reviewed_at"] = review.get("updated_at")
    return {
        "join_warnings": joins,
        "review_counts": dict(
            collections.Counter(item["disposition"] for item in joins)
        ),
    }


@app.post("/api/projects/{project_id}/quality/review")
async def update_quality_review(project_id: str, request: ReviewItemRequest):
    """Persist a non-destructive human quality-review disposition."""
    _require_job(project_id)
    return job_queue.set_review_item(
        project_id,
        request.item_type,
        request.item_id,
        request.disposition,
        request.note.strip(),
    )


@app.get("/api/projects/{project_id}/quality")
async def get_quality_report(project_id: str):
    """Get quality report for a project."""
    if not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    logs = job_queue.get_quality_report(project_id)
    
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
    }
    
    if not logs:
        return summary

    # Group by line_id to get the final attempt
    lines = {}
    for log in logs:
        line_id = log["line_id"]
        is_selected = bool(log.get("details", {}).get("selected"))
        current_selected = bool(
            lines.get(line_id, {}).get("details", {}).get("selected")
        )
        if (
            line_id not in lines
            or (is_selected and not current_selected)
            or (
                is_selected == current_selected
                and log["attempt"] > lines[line_id]["attempt"]
            )
        ):
            lines[line_id] = log
        if log["attempt"] > 1:
            summary["retries_triggered"] += 1
        details = log.get("details", {})
        summary["attempts"].append(
            {
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
            }
        )
            
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
        summary["final_attempts"].append(
            {
                "line_id": line["line_id"],
                "chapter_number": line["chapter_number"],
                "attempt": line["attempt"],
                "status": line["status"],
                "wer": line["wer"],
                "quality_score": line["quality_score"],
                "acceptance_reason": details.get("acceptance_reason", ""),
                "transcribed_text": details.get("transcribed_text", ""),
                "duration_seconds": details.get("duration_seconds"),
                "noise_floor_db": details.get("noise_floor_db"),
                "speaker_similarity": details.get("speaker_similarity"),
                "prosody_warning": details.get("prosody_warning", False),
                "audio_url": (
                    f"api/projects/{project_id}/segments/"
                    f"{line['line_id']}/audio"
                ),
            }
        )
            
    if summary["total_segments"] > 0:
        summary["average_wer"] = total_wer / summary["total_segments"]
        
    return summary


@app.post("/api/system/release-gpu")
async def release_gpu():
    """Release all app-owned GPU resources."""
    await _release_gpu_resources()
    return {"status": "success", "message": "GPU resources released"}


@app.post("/api/system/restart")
async def restart_dashboard_server():
    """Safely release GPU resources and restart the Dashboard process."""
    global _dashboard_shutdown_task
    if _dashboard_shutdown_task is None or _dashboard_shutdown_task.done():
        _dashboard_shutdown_task = asyncio.create_task(_shutdown_dashboard_process())
    return {"status": "restarting", "message": "Dashboard process is restarting"}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


@app.websocket("/ws/updates")
async def websocket_updates(websocket: WebSocket):
    """WebSocket for real-time pipeline updates."""
    token = configured_dashboard_token(_dashboard_cfg)
    presented_token = (
        websocket.headers.get("X-API-Token")
        or websocket.query_params.get("token")
    )
    client_host = websocket.client.host if websocket.client else None
    if not dashboard_request_authorized(
        client_host=client_host,
        configured_token=token,
        presented_token=presented_token,
    ):
        await websocket.close(
            code=1013 if not token and not is_loopback_client(client_host) else 1008
        )
        return
    await websocket.accept()
    ws_connections.append(websocket)
    logger.info("Dashboard WebSocket client connected")

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        try:
            ws_connections.remove(websocket)
        except ValueError:
            pass
        logger.info("Dashboard WebSocket client disconnected")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main():
    """Run the Brain Dashboard server."""
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Crazy Audiobook Creator — Brain Dashboard")
    parser.add_argument("--config", default="brain/config.yaml", help="Config file path")
    parser.add_argument("--host", default=None, help="Override host")
    parser.add_argument("--port", type=int, default=None, help="Override port")
    args = parser.parse_args()

    config = load_config(args.config)
    dashboard_cfg = config.get("dashboard", {})

    host = args.host or dashboard_cfg.get("host", "127.0.0.1")
    port = args.port or dashboard_cfg.get("port", 8000)
    token = configured_dashboard_token(dashboard_cfg)
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise RuntimeError(
            "Refusing to bind Dashboard beyond loopback without dashboard.api_token"
        )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    logger.info("Starting Brain Dashboard on %s:%d", host, port)
    uvicorn.run(
        "brain.dashboard.api.main:app",
        host=host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
