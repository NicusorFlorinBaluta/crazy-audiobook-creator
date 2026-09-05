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

from dotenv import load_dotenv

load_dotenv()

import asyncio
import collections
import csv
import hashlib
import io
import json
import logging
import logging.handlers
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
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

# Imported before the environment setup below because it must run ahead of any
# import that pulls in torch. `shared.constants` itself imports only `enum`.
from shared.constants import TORCH_ALLOC_CONF, TORCH_ALLOC_ENV_VARS

os.environ.setdefault("ROCM_SDK_TARGET_FAMILY", "custom")
# Inherited by the managed Voice Server subprocess. `Pipeline` sets it on the
# child environment explicitly as well; this covers a Voice Server started by
# other means from a dashboard process. See `shared.constants` for the
# crash-log evidence behind the value.
for _alloc_var in TORCH_ALLOC_ENV_VARS:
    os.environ.setdefault(_alloc_var, TORCH_ALLOC_CONF)

from brain.dashboard.api import runtime
from brain.dashboard.api.mobile import _chapter_duration
from brain.dashboard.api.mobile import router as mobile_router
from brain.dashboard.api.routers.external_validation import (
    router as external_validation_router,
)
from brain.dashboard.api.routers.pronunciations import (
    router as pronunciations_router,
)
from brain.dashboard.api.routers.quality import router as quality_router
from brain.dashboard.api.routers.voice_cast import router as voice_cast_router
from brain.dashboard.api.security import (
    TOKEN_ENV_VAR,
    configured_dashboard_token,
    configured_trusted_lan_cidrs,
    dashboard_request_authorized,
    is_cross_site_mutation,
    is_loopback_client,
)
from brain.dashboard.api.voice_support import (
    _cast_distinctness_review,
    _download_name_component,
    _ensure_voice_editable,
    _load_or_build_voice_cast,
    _voice_project_dir,
    _voice_review_approval_update,
)
from brain.orchestrator.audio_candidates import list_candidates
from brain.orchestrator.delivery_manager import DeliveryError, DeliveryManager
from brain.orchestrator.job_queue import JobQueue
from brain.orchestrator.nas_syncer import NASSyncer
from brain.orchestrator.pipeline import Pipeline
from brain.orchestrator.review_gate import collect_review_gate
from brain.orchestrator.stage_runner import PipelineResumePlan
from shared import paths as shared_paths
from shared.artifacts import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    fingerprint,
    hash_file,
)
from shared.constants import PipelineStage
from shared.models import ScriptChapter
from shared.performance import read_metrics, summarize_metrics
from shared.runtime_preflight import collect_runtime_report

logger = logging.getLogger(__name__)


def _frontend_build() -> str:
    """Identify the frontend that is actually on disk.

    This is what `/api/health` and the `X-Crazy-Audiobook-UI-Version` header
    report, and Home Assistant reads it to tell whether a restart picked up a
    deployment. It used to be a hand-edited literal, which meant it answered
    that question wrongly: it still said 2026.09.02.1 after two days of
    frontend changes, so a stale server and a fresh one were indistinguishable.

    Hashing the files removes the step a human has to remember. The date comes
    from the newest asset so the string stays readable at a glance; the digest
    is what actually makes it unique, because two edits on the same day would
    otherwise collide.

    Note this is a *version string*, not the cache buster -- `serve_dashboard`
    rewrites every asset query to a fresh timestamp on each request, so assets
    can never go stale regardless of this value.
    """
    if not FRONTEND_DIR.is_dir():
        return "unknown"
    digest = hashlib.sha256()
    newest = 0.0
    for path in sorted(FRONTEND_DIR.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".html", ".css", ".js"}:
            continue
        digest.update(path.relative_to(FRONTEND_DIR).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
        newest = max(newest, path.stat().st_mtime)
    stamp = datetime.fromtimestamp(newest, UTC).strftime("%Y.%m.%d") if newest else "unknown"
    return f"{stamp}.{digest.hexdigest()[:8]}"


FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
FRONTEND_BUILD = _frontend_build()

# ffmpeg is invoked inline while serving a chapter stream. A hung encode must
# not hold a request thread forever, so every streaming transcode is bounded.
FFMPEG_STREAM_TIMEOUT_SECONDS = 300


class AsyncioConnectionResetFilter(logging.Filter):
    """Filter out benign Windows asyncio socket connection reset errors."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "_call_connection_lost" in msg or "10054" in msg:
            return False
        return True


logging.getLogger("asyncio").addFilter(AsyncioConnectionResetFilter())

# Global state
#
# `pipeline` and `job_queue` are also mirrored onto `runtime` at startup so the
# extracted routers can reach them; see brain/dashboard/api/runtime.py for why
# they cannot simply be imported by value.
pipeline: Pipeline | None = None
job_queue: JobQueue | None = None
ws_connections: list[WebSocket] = []
# The one instance, shared with the routers rather than copied.
running_tasks: dict[str, asyncio.Task] = runtime.running_tasks
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

    active_projects = [project_id for project_id, task in running_tasks.items() if not task.done()]
    if active_projects:
        # These runs are about to be interrupted, and the pipeline will record
        # them as "Interrupted by user" with no pause_reason -- which is what
        # the operator sees. Say here that a GPU release did it, or the pause
        # is indistinguishable from someone pressing stop. Two e2e runs were
        # lost to exactly that ambiguity on 2026-09-04.
        logger.warning(
            "Releasing GPU resources; interrupting %d active run(s): %s",
            len(active_projects),
            ", ".join(active_projects),
        )
    for project_id in active_projects:
        pipeline.stop(project_id)

    voice_available = False
    try:
        await asyncio.to_thread(pipeline.voice_client.health_check_once)
        voice_available = True
    except Exception as exc:
        logger.debug("Voice service did not answer the pre-shutdown health check; treating it as already down: %s", exc)

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

    active_tasks = [task for task in running_tasks.values() if not task.done()]
    if active_tasks:
        await asyncio.wait(active_tasks, timeout=wait_seconds)

    try:
        await asyncio.to_thread(pipeline.ollama.unload_model)
    except Exception as exc:
        logger.warning("Ollama model unload failed during release: %s", exc)
    if voice_available:
        try:
            await asyncio.to_thread(pipeline.voice_client.unload_models)
        except Exception as exc:
            # A 409 here means a GPU job did not reach its cancellation
            # boundary inside `wait_seconds`. That is expected under a long
            # segment; the Voice server's idle-unload loop releases VRAM once
            # the job finishes. Log it so a stuck job is visible rather than
            # silently indistinguishable from a clean unload.
            logger.warning(
                "Voice model unload declined or failed during release (a GPU job may still be finishing): %s",
                exc,
            )
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
        sentinel_path = shared_paths.PROJECTS_DIR / ".dashboard_shutdown"
        sentinel_path.parent.mkdir(parents=True, exist_ok=True)
        sentinel_path.write_text("shutdown", encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "Could not write the shutdown sentinel; the next start will report this clean shutdown as an unexpected drop: %s",
            exc,
        )
    try:
        await _release_gpu_resources()
    finally:
        logging.shutdown()
        os._exit(0)


def _launch_dashboard_restart_helper() -> str:
    """Trigger the independently registered, fixed-command restart task."""
    if os.name != "nt":
        raise RuntimeError("Dashboard self-restart is currently supported on Windows only")
    task_name = "Crazy Audiobook Dashboard Restart"
    result = subprocess.run(
        ["schtasks.exe", "/Run", "/TN", task_name],
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Registered restart task is unavailable: {detail or result.returncode}")
    return task_name


class ChapterSelectionRequest(BaseModel):
    chapters: list[int] | None = None


class ScriptLineUpdate(BaseModel):
    speaker: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
    )


class VoiceApprovalRequest(BaseModel):
    continue_pipeline: bool = True
    acknowledge_similar_pairs: bool = False


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


def _require_project_stopped(project_id: str) -> dict[str, Any]:
    """Reject mutations that could race a live pipeline worker."""
    state = _require_job(project_id)
    task = running_tasks.get(project_id)
    if bool(state.get("running")) or (task is not None and not task.done()):
        raise HTTPException(
            status_code=409,
            detail="Stop the pipeline before changing this project",
        )
    return state


def _validation_reset_targets(state) -> list[int]:
    """Return the current audio selection for a validation-only rerun."""
    selection = state.get("generation_chapter_selection")
    if selection is None:
        selection = state.get("scripted_chapters") or []
    return sorted({int(chapter) for chapter in selection if int(chapter) > 0})


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


from shared.cache import cache_service


def _get_script_summary(project_id: str) -> dict[int, tuple[bool, int, str]]:
    """Return fast cached chapter valid/count/title mapping by per-file mtimes."""
    script_dir = _project_dir(project_id) / "script"
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

    cache_key = f"script_summary:{project_id}"
    cached = cache_service.get(cache_key)
    if cached and isinstance(cached, dict) and cached.get("file_mtimes") == file_mtimes:
        return cached.get("summary", {})

    result: dict[int, tuple[bool, int, str]] = {}
    for path in sorted(script_dir.glob("chapter_*.json")):
        if not re.fullmatch(r"chapter_\d{3,}\.json", path.name):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            ch_num = data.get("chapter_number")
            if ch_num is not None:
                lines = data.get("lines", [])
                result[int(ch_num)] = (len(lines) > 0, len(lines), str(data.get("chapter_title") or ""))
        except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Could not read %s; the dashboard will show this chapter as unscripted: %s", path, exc)

    cache_service.set(
        cache_key,
        {"file_mtimes": file_mtimes, "summary": result},
        ttl_seconds=1800,
    )
    return result


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
            if resolved.is_relative_to(root) and resolved.is_file() and not resolved.is_symlink():
                candidates.append(resolved)
    return sorted(set(candidates))


def _cleanup_token(paths: list[Path]) -> str:
    payload = "\n".join(f"{path}|{path.stat().st_size}|{path.stat().st_mtime_ns}" for path in paths if path.exists())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _redact_config(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "<redacted>"
                if any(term in key.lower() for term in ("token", "secret", "password")) and item
                else _redact_config(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_config(item) for item in value]
    return value


async def _interrupt_pipeline_worker(
    project_id: str,
    *,
    reason: str,
    wait_seconds: float = 20.0,
) -> None:
    """Stop a running project and wait for its worker to actually finish.

    NOTE: nothing in production calls this. It is exercised only by
    `test_immediate_interrupt_waits_for_worker_and_marks_paused`, which makes
    it look maintained while no code path reaches it. Kept rather than deleted
    because it does something the live stop paths do not -- it waits for the
    worker and records a specific `pause_reason` -- and `/api/projects/{id}/stop`
    plus `_release_gpu_resources` both open-code a weaker version. Wiring them
    to this is a behaviour change worth making deliberately, not as a drive-by.
    """
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
        except Exception as exc:
            logger.debug(
                "Project %s did not finish stopping within %ss; continuing with shutdown: %s",
                project_id,
                wait_seconds,
                exc,
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
                    raise ValueError(f"Expanded EPUB exceeds {max_expanded_mb} MB limit")
                if info.compress_size > 0 and info.file_size / info.compress_size > 1000:
                    raise ValueError(f"Suspicious compression ratio in EPUB member: {info.filename}")
    except zipfile.BadZipFile as exc:
        raise ValueError("Uploaded file is not a valid EPUB/ZIP archive") from exc


def _purge_project_cache(
    project_id: str,
    voice_project: Path,
    reference_hashes: list[str] | None = None,
) -> None:
    """Remove project-scoped cache rows and clone prompts for its references."""
    cache_path = shared_paths.repo_path("voice_cache.db")
    if not cache_path.is_file():
        return
    if reference_hashes is None:
        reference_hashes = [
            digest for digest in (hash_file(path) for path in voice_project.glob("*.wav") if path.is_file()) if digest
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
                f"DELETE FROM voice_clone_prompts WHERE ref_audio_hash IN ({placeholders})",
                reference_hashes,
            )
            connection.execute(
                f"DELETE FROM fx_prompt_cache WHERE source_audio_hash IN ({placeholders})",
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
    # Dropped outright: these say nothing about the book being produced.
    _SUPPRESS = {
        "uvicorn.access",
        "uvicorn.error",
    }

    # Dropped only below WARNING. The dashboard's routine chatter does not
    # belong in a book's log, but its warnings are about things happening to
    # that book -- a GPU release interrupting the run, a shutdown, a restart.
    # Filtering those out is how three unexplained pauses stayed unexplained.
    _SUPPRESS_BELOW_WARNING = {
        "brain.dashboard.api.main",
    }

    def __init__(self, project_id: str):
        super().__init__()
        self.project_id = project_id
        self._loop: asyncio.AbstractEventLoop | None = None
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(name)s | %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        # Skip dashboard / uvicorn noise
        if record.name in self._SUPPRESS:
            return
        if record.name in self._SUPPRESS_BELOW_WARNING and record.levelno < logging.WARNING:
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
                log_file = shared_paths.PROJECTS_DIR / pid / "pipeline.log"
                log_file.parent.mkdir(parents=True, exist_ok=True)
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                # Never log from inside a log handler -- it re-enters the
                # handler that just failed. handleError reports to stderr.
                self.handleError(record)

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
    """Load configuration from YAML. See `runtime.load_config`."""
    return runtime.load_config(config_path)


def _schedule_resume_after_reviews(project_id: str) -> bool:
    """See `runtime.schedule_resume_after_reviews`."""
    return runtime.schedule_resume_after_reviews(project_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global pipeline, job_queue

    from shared.single_instance import SingleInstanceLock

    lock = SingleInstanceLock("dashboard_service.lock")
    if not lock.acquire():
        logger.error("Another Dashboard API instance is already running! Exiting.")
        import sys

        sys.exit(1)

    config = load_config()
    pipeline = Pipeline(config_path="brain/config.yaml")
    job_queue = pipeline.job_queue
    runtime.bind(pipeline_obj=pipeline, job_queue_obj=job_queue)
    app.state.pipeline = pipeline
    app.state.job_queue = job_queue
    app.state.running_tasks = running_tasks

    logging.getLogger().setLevel(logging.INFO)
    logger.info("Brain Dashboard starting...")
    # An elevated run leaves every artifact it writes owned by Administrators,
    # which the normal unelevated service can then no longer replace. The
    # failure appears much later, somewhere unrelated, as WinError 5.
    shared_paths.warn_if_elevated(logger)

    # Check shutdown sentinel to distinguish intentional shutdown from unexpected drops
    sentinel_path = shared_paths.PROJECTS_DIR / ".dashboard_shutdown"
    was_graceful_shutdown = sentinel_path.exists()
    if was_graceful_shutdown:
        try:
            sentinel_path.unlink()
        except OSError as exc:
            logger.warning(
                "Could not clear the shutdown sentinel; the next unexpected crash will be reported as a clean shutdown: %s",
                exc,
            )

    auto_resume = config.get("dashboard", {}).get("auto_resume_in_flight", True)
    jobs_to_resume: list[str] = []

    for stale_job in job_queue.list_jobs():
        if not stale_job.get("running"):
            continue
        stale_status = stale_job.get("status")
        if stale_status == PipelineStage.WAITING_FOR_REVIEW.value:
            # Jobs waiting for review should remain in waiting_for_review and not auto-run
            job_queue.update_job(
                stale_job["project_id"],
                {
                    "running": False,
                    "pause_reason": None,
                },
            )
            continue
        if auto_resume and not was_graceful_shutdown:
            jobs_to_resume.append(stale_job["project_id"])
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
                "resume_after_restart": True,
            },
        )

    if jobs_to_resume:
        for pid in jobs_to_resume:
            logger.info("Auto-resuming in-flight project after unexpected restart: %s", pid)
            asyncio.create_task(start_pipeline(pid))

    def _warmup_caches():
        for job in job_queue.list_jobs():
            pid = job.get("project_id")
            if pid:
                try:
                    _get_script_summary(pid)
                    collect_review_gate(pid, _project_dir(pid), job_queue)
                except Exception as exc:
                    logger.debug(
                        "Cache warmup failed for %s; the values will be computed on first request instead: %s", pid, exc
                    )

    asyncio.create_task(asyncio.to_thread(_warmup_caches))

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
                                    await ws.send_json({"type": "status_update", "project_id": pid, "status": st})
                                except Exception as exc:
                                    logger.debug("Dropped a status update to one websocket client: %s", exc)
                except Exception as exc:
                    logger.warning(
                        "Status broadcast cycle failed; connected dashboards will not update until the next tick: %s",
                        exc,
                    )

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
                if scheduled_job.get("status") != PipelineStage.PAUSED_SCHEDULED.value:
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
        except Exception as exc:
            logger.debug("Ollama client did not close cleanly: %s", exc)
        try:
            pipeline.voice_client.close()
        except Exception as exc:
            logger.debug("Voice client did not close cleanly: %s", exc)
    lock.release()


app = FastAPI(
    title="Crazy Audiobook Creator — Brain Dashboard",
    description="Pipeline orchestration and monitoring dashboard",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(mobile_router)
# Route modules split out of this file. See brain/dashboard/api/routers/.
app.include_router(pronunciations_router)
app.include_router(voice_cast_router)
app.include_router(external_validation_router)
app.include_router(quality_router)

_import_config = load_config()
_dashboard_cfg = _import_config.get("dashboard", {})
_TRUSTED_LAN_CIDRS = configured_trusted_lan_cidrs(_dashboard_cfg)
_cors_origins = _dashboard_cfg.get(
    "cors_origins",
    ["*"],
)


def _assert_safe_bind(dashboard_config: dict[str, Any]) -> None:
    """Refuse a token-free non-loopback bind, however the app was started.

    This check used to live only in ``main()``. Nothing starts the dashboard
    through ``main()`` -- ``start_app.pyw``, ``desktop/main.js`` and the
    documented quick-start command all run
    ``uvicorn brain.dashboard.api.main:app`` and import ``app`` directly, so the
    guard never executed. Running it at import time makes it apply to every
    launch path.

    Binding beyond loopback is still allowed; it just requires either an
    application token or an explicitly narrowed
    ``dashboard.trusted_lan_cidrs``. A deployment that is reachable from a
    reverse proxy should set a token, because the proxy's own LAN address is
    otherwise inside the default trust boundary.
    """
    host = str(dashboard_config.get("host", "127.0.0.1")).strip()
    if host in ("127.0.0.1", "localhost", "::1", ""):
        return
    if configured_dashboard_token(dashboard_config):
        return
    if "trusted_lan_cidrs" in dashboard_config:
        # The operator has explicitly declared the boundary; that is the
        # documented way to run token-free on a LAN.
        return
    raise RuntimeError(
        f"Refusing to bind the Dashboard to {host!r} without either "
        f"{TOKEN_ENV_VAR} / dashboard.api_token or an explicit "
        "dashboard.trusted_lan_cidrs list. Set one of them in brain/config.yaml "
        "or the environment. Binding to 0.0.0.0 with neither would trust every "
        "RFC1918 and Tailscale CGNAT peer without authentication."
    )


_assert_safe_bind(_dashboard_cfg)

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
    if (
        not request.url.path.endswith("/stream")
        and "/download" not in request.url.path
        and not request.url.path.endswith("/cover")
    ):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.middleware("http")
async def require_dashboard_token(request: Request, call_next):
    # Compatibility discovery contains no catalog, media, logs, or mutable state.
    if request.url.path == "/api/mobile/v1/server-info":
        return await call_next(request)

    if request.url.path.startswith("/api/") and is_cross_site_mutation(
        request.method,
        request.headers.get("Sec-Fetch-Site"),
    ):
        return JSONResponse(
            {"detail": "Cross-site state changes are not allowed"},
            status_code=403,
        )
    token = configured_dashboard_token(_dashboard_cfg)
    client_host = request.client.host if request.client else None
    if request.url.path.startswith("/api/") and not dashboard_request_authorized(
        client_host=client_host,
        configured_token=token,
        presented_token=request.headers.get("X-API-Token"),
        trusted_lan_cidrs=_TRUSTED_LAN_CIDRS,
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
        response.headers["Cache-Control"] = "no-store, no-cache, max-age=0, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["X-Crazy-Audiobook-UI-Version"] = FRONTEND_BUILD
    return response


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Static files (frontend)
# ---------------------------------------------------------------------------

frontend_dir = FRONTEND_DIR
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
            rf"\1?v={timestamp}\2",
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
@app.get("/api/health", include_in_schema=False)
async def dashboard_health():
    """Return minimal readiness information for HA and the reverse proxy."""
    return {
        "status": "ok",
        "ready": pipeline is not None and job_queue is not None,
        "pipeline_running": any(not task.done() for task in running_tasks.values()),
        "timestamp": datetime.now(UTC).isoformat(),
        "version": FRONTEND_BUILD,
    }


# ---------------------------------------------------------------------------
# Project management
# ---------------------------------------------------------------------------


@app.get("/api/projects")
async def list_projects():
    """List all projects."""
    if not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    projects = job_queue.list_jobs()

    def _enrich():
        for project in projects:
            try:
                gate = collect_review_gate(
                    project["project_id"], _project_dir(project["project_id"]), job_queue
                ).to_dict()
                project["attention_count"] = gate["total_count"]
                project["blocking_review_count"] = gate["blocking_count"]
            except Exception:
                project["attention_count"] = 0
                project["blocking_review_count"] = 0
        return projects

    return await asyncio.to_thread(_enrich)


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
    temp_dir = shared_paths.PROJECTS_DIR / "_uploads"
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
        logger.info(
            "[DashboardAPI] Created project '%s' (%d chapters detected)", status.project_id, status.total_chapters
        )

        if cfg.get("metadata", {}).get("auto_fetch_external", False):
            asyncio.create_task(asyncio.to_thread(_auto_fetch_metadata_sync, status.project_id))

        return {
            "project_id": status.project_id,
            "title": status.title,
            "author": status.author,
            "chapters_detected": status.total_chapters,
            "status": status.status,
        }

    except Exception as e:
        logger.exception("[DashboardAPI] Failed to create project from '%s': %s", file.filename, e)
        raise HTTPException(status_code=400, detail=str(e)) from e

    finally:
        if temp_path.exists():
            temp_path.unlink()
        for extracted_cover in temp_path.parent.glob(f"{temp_path.stem}_cover.*"):
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
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc


def _safe_delete_tree(path: Path) -> bool:
    """Safely delete a directory tree on Windows, clearing read-only attributes."""
    import stat

    if not path.exists() and not path.is_symlink():
        return True

    def _remove_readonly(func, file_path, exc_info):
        try:
            os.chmod(file_path, stat.S_IWRITE | stat.S_IREAD)
            func(file_path)
        except Exception as exc:
            logger.debug(
                "Could not clear the read-only attribute on %s; the delete will fail and report it: %s", file_path, exc
            )

    if path.is_dir():
        try:
            shutil.rmtree(path, onexc=_remove_readonly)
        except OSError:
            try:
                shutil.rmtree(path, ignore_errors=True)
            except OSError as exc:
                logger.warning("Could not fully delete directory %s: %s", path, exc)
    else:
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not delete file %s: %s", path, exc)
    return not path.exists() and not path.is_symlink()


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, delete_from_nas: bool = False):
    """Delete a stopped project's state and local artifacts, with optional NAS removal."""
    if not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    if project_id in running_tasks and not running_tasks[project_id].done():
        raise HTTPException(
            status_code=409,
            detail="Pause the pipeline before deleting this project",
        )

    project_exists = False
    try:
        job_queue.get_job(project_id)
        project_exists = True
    except KeyError:
        pass

    roots = [
        _project_dir(project_id),
        _workspace_project_dir(project_id),
        _voice_project_dir(project_id),
    ]
    if any(r.exists() for r in roots):
        project_exists = True

    if not project_exists:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    voice_project = _voice_project_dir(project_id)
    reference_hashes = [
        digest for digest in (hash_file(path) for path in voice_project.glob("*.wav") if path.is_file()) if digest
    ]
    remaining = [root for root in roots if not _safe_delete_tree(root)]
    if remaining:
        logger.error(
            "Project deletion incomplete for %s; retained job state. Remaining: %s",
            project_id,
            remaining,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "message": ("Project cleanup is incomplete; retry after releasing locked files"),
                "remaining_roots": [str(path) for path in remaining],
            },
        )

    try:
        _purge_project_cache(project_id, voice_project, reference_hashes)
    except Exception as exc:
        logger.warning(
            "Could not purge cache rows for deleted project %s: %s",
            project_id,
            exc,
        )

    try:
        job_queue.delete_job(project_id)
    except KeyError:
        pass
    except Exception as exc:
        logger.warning("Error deleting job %s from database: %s", project_id, exc)

    # Clean up from NAS if requested
    try:
        from brain.orchestrator.nas_syncer import NASSyncer

        nas_syncer = NASSyncer()
        if nas_syncer.is_configured:
            nas_syncer.delete_project(project_id, delete_from_nas=delete_from_nas)
    except Exception as exc:
        logger.warning("Error during NAS project deletion check for %s: %s", project_id, exc)

    running_tasks.pop(project_id, None)
    _project_logs.pop(project_id, None)
    _log_subscribers.pop(project_id, None)
    return {"status": "deleted", "project_id": project_id, "deleted_from_nas": delete_from_nas}


# ---------------------------------------------------------------------------
# NAS Storage & Sync endpoints
# ---------------------------------------------------------------------------


@app.get("/api/nas/status")
async def get_nas_status():
    """Return NAS configuration and connectivity status."""
    syncer = NASSyncer()
    return {
        "configured": syncer.is_configured,
        "host": syncer.host if syncer.is_configured else None,
        "shared_folder": syncer.shared_folder,
        "auto_sync": syncer.auto_sync,
        "prune_parts_on_full": syncer.prune_parts_on_full,
    }


@app.post("/api/nas/test-connection")
async def test_nas_connection():
    """Test SSH/SFTP connection and write access to the NAS."""
    syncer = NASSyncer()
    if not syncer.is_configured:
        raise HTTPException(status_code=400, detail="NAS is not configured in .env or settings.")
    try:
        result = syncer.test_connection()
        return result
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"NAS connection failed: {e}") from e


@app.post("/api/nas/sync-all")
async def sync_all_to_nas():
    """Scan and synchronize all ready books/parts to the NAS."""
    syncer = NASSyncer()
    if not syncer.is_configured:
        raise HTTPException(status_code=400, detail="NAS is not configured in .env or settings.")
    try:
        result = syncer.sync_all_projects()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to sync projects to NAS: {e}") from e


@app.get("/api/notifications/settings")
async def get_notification_settings():
    """Return Home Assistant notification engine configuration and status."""
    from brain.orchestrator.notifier import notifier

    return {
        "configured": notifier.is_configured,
        "base_url": notifier.base_url,
        "notify_service": notifier.notify_service,
        "dashboard_url": notifier.dashboard_url,
    }


@app.post("/api/notifications/test")
async def test_notification():
    """Send a test notification to Home Assistant to verify mobile delivery."""
    from brain.orchestrator.notifier import NotificationEventType, NotificationPayload, notifier

    if not notifier.is_configured:
        raise HTTPException(status_code=400, detail="Home Assistant notification engine is not configured.")
    payload = NotificationPayload(
        event_type=NotificationEventType.TEST,
        project_id="test",
        project_title="Test Notification",
        title="🔔 Crazy Audiobook Creator Test",
        message="Your Home Assistant notification integration is working! Tap to open the creator dashboard.",
        importance="high",
    )
    result = notifier.send_notification_sync(payload)
    if result.get("status") == "error":
        raise HTTPException(status_code=502, detail=f"Failed to send notification: {result.get('error')}")
    return {"status": "success", "detail": "Notification sent successfully to Home Assistant!", "result": result}


# ---------------------------------------------------------------------------
# Pipeline control
# ---------------------------------------------------------------------------


@app.post("/api/projects/{project_id}/start")
async def start_pipeline(
    project_id: str,
    override_schedule: bool = False,
):
    """Start the pipeline for a project."""
    if not pipeline or not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")

    if project_id in running_tasks and not running_tasks[project_id].done():
        current = job_queue.get_job(project_id)
        if current.get("status") in (
            PipelineStage.PAUSED_SCHEDULED.value,
            PipelineStage.DEPLOY_PAUSED.value,
        ):
            if override_schedule and current.get("status") == PipelineStage.PAUSED_SCHEDULED.value:
                job_queue.update_job(
                    project_id,
                    {
                        "schedule_override_active": True,
                        "pause_reason": None,
                    },
                )
                return {
                    "status": "resumed",
                    "project_id": project_id,
                    "stage": current.get("active_stage") or current.get("status"),
                    "schedule_overridden": True,
                }
        raise HTTPException(status_code=409, detail="Pipeline already running")
    active_project = next(
        (pid for pid, task in running_tasks.items() if pid != project_id and not task.done()),
        None,
    )
    if active_project:
        raise HTTPException(
            status_code=409,
            detail=(
                f"GPU pipeline is serialized; wait for or pause project '{active_project}' before starting another"
            ),
        )

    current = job_queue.get_job(project_id)
    if current.get("generation_chapter_selection") == []:
        raise HTTPException(
            status_code=409,
            detail="Select at least one chapter before starting generation",
        )

    resume_stage = PipelineResumePlan.from_state(current).stage
    if resume_stage not in (
        PipelineStage.CREATED,
        PipelineStage.EXTRACTING,
        PipelineStage.SCRIPTING,
    ):
        gate = collect_review_gate(project_id, _project_dir(project_id), job_queue)
        if gate.blocking_items:
            raise HTTPException(
                status_code=409,
                detail=(f"Resolve {len(gate.blocking_items)} blocking review item(s) before resuming the pipeline."),
            )
    if (
        current.get("voice_review_policy", "grandfathered") == "required_once"
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
            "schedule_override_active": override_schedule,
            "review_blocking_item_ids": [],
        },
    )
    # Progress for this transition is carried by the `update_job` call above and
    # by the versioned progress snapshot written in `Pipeline._update_stage`.
    # A second `emit_progress`/`ProgressEvent` mechanism was referenced here but
    # never implemented, so the block was unreachable dead code.

    # Clear old logs for this project on a fresh start
    _project_logs[project_id] = collections.deque(maxlen=500)

    async def run_in_background():
        handler = _attach_project_logger(project_id)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, pipeline.run, project_id)
        except Exception as e:
            logger.exception("Pipeline failed for %s: %s", project_id, e)
            for ws in ws_connections:
                try:
                    await ws.send_json(
                        {
                            "type": "error",
                            "project_id": project_id,
                            "message": str(e),
                        }
                    )
                except Exception as exc:
                    logger.debug("Dropped a pipeline-failure notice to one websocket client: %s", exc)
        finally:
            # The stop endpoint deliberately persists running=True while a
            # cooperative stop is in flight.  A worker can finish at a natural
            # boundary (for example voice review) before observing that stop
            # flag, so always reconcile the durable flag when its task exits.
            try:
                job_queue.update_job(
                    project_id,
                    {
                        "running": False,
                        "schedule_override_active": False,
                    },
                )
            except KeyError:
                pass
            _detach_project_logger(handler)
            # Send sentinel to all SSE subscribers so they know the run ended
            for q in list(_log_subscribers.get(project_id, [])):
                try:
                    q.put_nowait(None)  # None = stream done
                except Exception as exc:
                    logger.debug(
                        "Could not send the end-of-run sentinel to a log subscriber; that stream will idle until the client reconnects: %s",
                        exc,
                    )

    task = asyncio.create_task(run_in_background())
    running_tasks[project_id] = task

    return {
        "status": "started",
        "project_id": project_id,
        "schedule_overridden": override_schedule,
    }


# Hand the starter to runtime so the extracted routers, the scheduler and
# the review-resume path can start a run without importing this module.
runtime.register_pipeline_starter(start_pipeline)


def _automatic_extraction_review_pending(
    project_id: str,
    blocking_items: list[Any],
) -> bool:
    project_dir = _project_dir(project_id)
    audit_path = project_dir / "extraction_audit.json"
    if not audit_path.exists():
        return False
    try:
        data = json.loads(audit_path.read_text(encoding="utf-8"))
        for section in data.get("sections", []):
            if section.get("review_required") and not section.get("external_validation_attempted"):
                return True
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        logger.warning(
            "Could not read %s; external validation will look unattempted for this project: %s", audit_path, exc
        )
    return False


def _automatic_pipeline_review_pending(
    project_id: str,
    blocking_items: list[Any],
    state: dict[str, Any],
    stage: PipelineStage,
) -> bool:
    if stage == PipelineStage.SCRIPTING and not state.get("script_completed", False):
        return any(getattr(item, "category", "") == "attribution" for item in blocking_items)
    return False


@app.post("/api/projects/{project_id}/stop")
async def stop_pipeline(
    project_id: str,
    resume_on_schedule: bool = False,
):
    """Request a cooperative stop and report a transitional PAUSING state."""
    if not pipeline or not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")

    try:
        state = job_queue.get_job(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc

    will_resume_on_schedule = bool(resume_on_schedule or (pipeline and not pipeline.schedule_is_open()))
    if will_resume_on_schedule:
        new_status = PipelineStage.PAUSED_SCHEDULED.value
        pause_reason = "waiting for configured working hours"
    else:
        new_status = PipelineStage.PAUSED.value
        pause_reason = "user requested stop"

    job_queue.update_job(
        project_id,
        {
            "status": new_status,
            "active_stage": state.get("active_stage") or state.get("status"),
            "pause_reason": pause_reason,
            "running": True,
            "schedule_override_active": False,
        },
    )

    pipeline.stop(project_id)

    active_stage = str(state.get("active_stage") or state.get("status") or "")
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

    return {
        "status": "stopping",
        "project_id": project_id,
        "will_resume_on_schedule": will_resume_on_schedule,
    }

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
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid stage: {stage_value}") from exc
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

    packaging_guard = None
    try:
        project_dir = _project_dir(project_id)
        workspace_dir = _workspace_project_dir(project_id)
        packaging_guard = DeliveryManager(project_dir).packaging_lock(wait=False)
        try:
            packaging_guard.__enter__()
        except DeliveryError as exc:
            packaging_guard = None
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        import shutil

        update: dict[str, Any] = {
            "status": "paused",
            "active_stage": stage.value,
            "pause_reason": "voice_review" if stage == PipelineStage.VOICE_REVIEW else "pipeline reset",
            "error_message": None,
            "running": False,
            "reset_target_stage": stage.value,
        }

        def _safe_unlink(p: Path) -> None:
            try:
                p.unlink(missing_ok=True)
            except OSError as e:
                raise RuntimeError(f"Could not remove {p} during reset: {e}") from e

        if stage in {PipelineStage.EXTRACTING, PipelineStage.SCRIPTING}:
            job_queue.clear_review_items(project_id)
            update["review_blocking_item_ids"] = []
            for p in [
                project_dir / "characters.json",
                project_dir / "characters.checkpoint.json",
                project_dir / "characters.meta.json",
                project_dir / "voice_cast.json",
                project_dir / ".fingerprints.json",
                project_dir / "book_script.json",
                project_dir / "attribution_audit.json",
                project_dir / "character_augmentation_audit.json",
                project_dir / "character_reference_audit.json",
                project_dir / "extraction_audit.json",
            ]:
                _safe_unlink(p)
            for d in [
                project_dir / "script",
                project_dir / "segments",
                project_dir / "mastered",
                project_dir / "external_validation",
                workspace_dir / "segments",
                workspace_dir / "mastered",
            ]:
                if d.exists() and d.is_dir():
                    shutil.rmtree(d)
            cache_service.delete(
                f"script_chapters:{project_id}",
                f"script_line_index:{project_id}",
                f"script_summary:{project_id}",
                f"script_review:{project_dir.resolve()}",
            )
            if stage == PipelineStage.EXTRACTING:
                try:
                    await asyncio.to_thread(pipeline.reextract_project, project_id)
                except FileNotFoundError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
            update.update(
                {
                    "script_completed": False,
                    "bootstrapping_completed": False,
                    "voice_review_approved": False,
                    "voice_review_status": "pending",
                    "voice_review_approved_at": None,
                    "scripted_chapters": [],
                    "generated_chapters": [],
                    "mastered_chapters": [],
                    "force_character_analysis": True,
                }
            )

        elif stage == PipelineStage.BOOTSTRAPPING:
            job_queue.clear_review_items(project_id, item_type="segment")
            update.update(
                {
                    "bootstrapping_completed": False,
                    "voice_review_approved": False,
                    "voice_review_status": "pending",
                    "voice_review_approved_at": None,
                    "bootstrapping_fingerprint": None,
                    "generated_chapters": [],
                    "mastered_chapters": [],
                    "force_voice_regeneration": True,
                }
            )
            _safe_unlink(project_dir / "voice_cast.json")
            for d in [
                project_dir / "segments",
                project_dir / "mastered",
                workspace_dir / "segments",
                workspace_dir / "mastered",
            ]:
                if d.exists() and d.is_dir():
                    shutil.rmtree(d)

        elif stage == PipelineStage.VOICE_REVIEW:
            job_queue.clear_review_items(project_id, item_type="segment")
            update.update(
                {
                    "status": "paused",
                    "active_stage": "voice_review",
                    "pause_reason": "voice_review",
                    "voice_review_status": "pending",
                    "voice_review_policy": "required_once",
                    "voice_review_approved": False,
                    "voice_review_approved_at": None,
                    "generated_chapters": [],
                    "mastered_chapters": [],
                }
            )
            for d in [
                project_dir / "segments",
                project_dir / "mastered",
                workspace_dir / "segments",
                workspace_dir / "mastered",
            ]:
                if d.exists() and d.is_dir():
                    shutil.rmtree(d)

        elif stage == PipelineStage.GENERATING:
            job_queue.clear_review_items(project_id, item_type="segment")
            update.update(
                {
                    "generated_chapters": [],
                    "mastered_chapters": [],
                }
            )
            for d in [
                project_dir / "segments",
                project_dir / "mastered",
                workspace_dir / "segments",
                workspace_dir / "mastered",
            ]:
                if d.exists() and d.is_dir():
                    shutil.rmtree(d)

        elif stage == PipelineStage.VALIDATING:
            job_queue.clear_review_items(project_id, item_type="segment")
            update.update(
                {
                    "generated_chapters": [],
                    "mastered_chapters": [],
                    "validation_revision": uuid.uuid4().hex,
                }
            )
            _safe_unlink(project_dir / "long_form_audio_quality.json")
            manifests_dir = project_dir / "manifests"
            if manifests_dir.is_dir():
                for manifest in manifests_dir.glob("chapter_*.segments.json"):
                    manifest.unlink(missing_ok=True)
                for manifest in manifests_dir.glob("chapter_*.master.json"):
                    manifest.unlink(missing_ok=True)
            for d in [project_dir / "mastered", workspace_dir / "mastered"]:
                if d.exists() and d.is_dir():
                    shutil.rmtree(d)

        elif stage == PipelineStage.MASTERING:
            update.update({"mastered_chapters": []})
            for d in [project_dir / "mastered", workspace_dir / "mastered"]:
                if d.exists() and d.is_dir():
                    shutil.rmtree(d)
            (project_dir / f"{project_id}.m4b").unlink(missing_ok=True)
            (workspace_dir / "output" / f"{project_id}.m4b").unlink(missing_ok=True)

        elif stage == PipelineStage.EXPORTING:
            (project_dir / f"{project_id}.m4b").unlink(missing_ok=True)
            (workspace_dir / "output" / f"{project_id}.m4b").unlink(missing_ok=True)

        if stage in {PipelineStage.EXTRACTING, PipelineStage.SCRIPTING}:
            deliveries_dir = project_dir / "deliveries"
            if deliveries_dir.is_dir():
                history_root = project_dir / "delivery_history"
                history_root.mkdir(parents=True, exist_ok=True)
                archive = history_root / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                os.replace(deliveries_dir, archive)
                DeliveryManager(project_dir).prune_delivery_history(retain=2)
            update.update(
                {
                    "published_delivery_count": 0,
                    "latest_published_delivery_id": None,
                    "active_delivery_id": None,
                    "active_delivery_chapters": [],
                }
            )
        elif stage != PipelineStage.EXPORTING:
            DeliveryManager(project_dir).mark_all_stale(f"Pipeline reset to {stage.value}")
        if stage != PipelineStage.EXPORTING:
            update["export_stale"] = True
        update["progress"] = {
            "schema_version": 1,
            "stage": stage.value,
            "phase": "idle",
            "message": f"Reset to {stage.value}",
            "chapter": 0,
            "chapter_position": 0,
            "chapter_total": 0,
            "line_id": "",
            "line_position": 0,
            "line_total": 0,
            "attempt": 1,
            "cache_hit": None,
            "completed_units": 0.0,
            "total_units": 0.0,
            "percent": 0.0,
            "elapsed_seconds": 0.0,
            "eta_seconds": None,
            "eta_confidence": None,
            "started_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        job_queue.update_job(project_id, update)
        # The reset itself is the progress signal; the `update` above already
        # clears elapsed/eta and stamps `updated_at`. See the note in
        # `start_pipeline` about the unimplemented `emit_progress` path.
        packaging_guard.__exit__(None, None, None)
        packaging_guard = None
        return {"status": "success", "project_id": project_id, "stage": stage.value}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc
    finally:
        if packaging_guard is not None:
            packaging_guard.__exit__(None, None, None)


@app.get("/api/projects/{project_id}/download")
async def download_audiobook(project_id: str, delivery_id: str | None = None):
    """Download the final mastered audiobook."""
    project_dir = _project_dir(project_id)
    if delivery_id:
        dm = DeliveryManager(project_dir)
        try:
            part, m4b_path = dm.resolve_published_artifact(delivery_id)
        except DeliveryError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(path=m4b_path, filename=part.artifact, media_type="audio/mp4")
    try:
        state = job_queue.get_job(project_id) if job_queue else {}
    except KeyError:
        state = {}
    if state.get("export_stale") and state.get("status") != PipelineStage.COMPLETE.value:
        raise HTTPException(
            status_code=409,
            detail="The full audiobook is stale and must be exported again",
        )
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
            title = json.loads(book_json.read_text(encoding="utf-8")).get("metadata", {}).get("title") or project_id
        except (OSError, json.JSONDecodeError):
            pass
    return FileResponse(
        path=m4b_path, filename=f"{_download_name_component(str(title), project_id)}.m4b", media_type="audio/mp4"
    )


@app.get("/api/projects/{project_id}/stream")
async def stream_audiobook(project_id: str):
    """Stream the mastered audiobook file with byte-range support."""
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

    return FileResponse(
        path=m4b_path,
        media_type="audio/mp4",
        headers={
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'inline; filename="{m4b_path.name}"',
        },
    )


@app.get("/api/projects/{project_id}/download/chapter/{chapter_num}")
async def download_chapter_audio(project_id: str, chapter_num: int):
    """Download the mastered WAV file for a specific chapter."""
    if chapter_num < 1:
        raise HTTPException(status_code=422, detail="Chapter number must be positive")
    ch_file = _workspace_project_dir(project_id) / "chapters" / f"chapter_{chapter_num:03d}.wav"
    if not ch_file.exists():
        ch_file = _project_dir(project_id) / "chapters" / f"chapter_{chapter_num:03d}.wav"
    if not ch_file.exists():
        raise HTTPException(status_code=404, detail=f"Chapter {chapter_num} mastered audio not found")
    return FileResponse(path=ch_file, filename=f"{project_id}_chapter_{chapter_num:03d}.wav", media_type="audio/wav")


@app.get("/api/projects/{project_id}/stream/chapter/{chapter_num}")
async def stream_chapter_audio(project_id: str, chapter_num: int, format: str = "aac"):
    """Stream a specific chapter with byte ranges and optional transparent 128k AAC compression."""
    if chapter_num < 1:
        raise HTTPException(status_code=422, detail="Chapter number must be positive")

    ch_file = _workspace_project_dir(project_id) / "chapters" / f"chapter_{chapter_num:03d}.wav"
    if not ch_file.exists():
        m4b_path = _project_dir(project_id) / f"{project_id}.m4b"
        if not m4b_path.exists():
            m4b_path = _workspace_project_dir(project_id) / "output" / f"{project_id}.m4b"
        if m4b_path.exists():
            book_json = _project_dir(project_id) / "book.json"
            chapters = []
            if book_json.exists():
                try:
                    bdata = json.loads(book_json.read_text(encoding="utf-8"))
                    chapters = bdata.get("chapters", [])
                except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
                    logger.warning(
                        "Could not read %s; the finished book will be listed with no chapters: %s", book_json, exc
                    )

            start_sec = 0.0
            dur_sec = None
            for idx, ch in enumerate(chapters, 1):
                ch_dur = _chapter_duration(_project_dir(project_id), _workspace_project_dir(project_id), idx) or 300.0
                if idx == chapter_num:
                    dur_sec = ch_dur
                    break
                start_sec += ch_dur

            transcodes_dir = _workspace_project_dir(project_id) / "transcodes"
            transcodes_dir.mkdir(parents=True, exist_ok=True)
            aac_file = transcodes_dir / f"chapter_{chapter_num:03d}.m4a"

            if not aac_file.exists() or aac_file.stat().st_mtime < m4b_path.stat().st_mtime:
                cmd = ["ffmpeg", "-y", "-ss", f"{start_sec:.2f}"]
                if dur_sec:
                    cmd.extend(["-t", f"{dur_sec:.2f}"])
                cmd.extend(["-i", str(m4b_path), "-c", "copy", "-movflags", "+faststart", str(aac_file)])
                try:
                    subprocess.run(
                        cmd,
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=FFMPEG_STREAM_TIMEOUT_SECONDS,
                    )
                except subprocess.TimeoutExpired:
                    logger.warning(
                        "ffmpeg m4b chapter slice timed out after %ss for %s chapter %d",
                        FFMPEG_STREAM_TIMEOUT_SECONDS,
                        project_id,
                        chapter_num,
                    )
                except (OSError, subprocess.SubprocessError, ValueError) as e:
                    logger.warning("Could not extract chapter slice from m4b: %s", e)

            if aac_file.exists():
                return FileResponse(
                    path=aac_file,
                    media_type="audio/mp4",
                    headers={
                        "Accept-Ranges": "bytes",
                        "Content-Disposition": f'inline; filename="{project_id}_chapter_{chapter_num:03d}.m4a"',
                    },
                )
        raise HTTPException(status_code=404, detail=f"Chapter {chapter_num} mastered audio not found")

    if format.lower() in ("aac", "m4a", "mp4"):
        transcodes_dir = _workspace_project_dir(project_id) / "transcodes"
        transcodes_dir.mkdir(parents=True, exist_ok=True)
        aac_file = transcodes_dir / f"chapter_{chapter_num:03d}.m4a"

        if not aac_file.exists() or aac_file.stat().st_mtime < ch_file.stat().st_mtime:
            try:
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(ch_file),
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-movflags",
                    "+faststart",
                    str(aac_file),
                ]
                subprocess.run(
                    cmd,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=FFMPEG_STREAM_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                logger.warning(
                    "AAC transcoding timed out after %ss, falling back to WAV",
                    FFMPEG_STREAM_TIMEOUT_SECONDS,
                )
                aac_file = ch_file
            except (OSError, subprocess.SubprocessError, ValueError) as e:
                logger.warning("AAC transcoding failed, falling back to WAV: %s", e)
                aac_file = ch_file

        if aac_file.exists() and aac_file.suffix == ".m4a":
            return FileResponse(
                path=aac_file,
                media_type="audio/mp4",
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Disposition": f'inline; filename="{project_id}_chapter_{chapter_num:03d}.m4a"',
                },
            )

    return FileResponse(
        path=ch_file,
        media_type="audio/wav",
        headers={
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'inline; filename="{project_id}_chapter_{chapter_num:03d}.wav"',
        },
    )


@app.get("/api/projects/{project_id}/status")
async def get_pipeline_status(project_id: str):
    """Get the current pipeline status with detailed per-chapter metrics."""
    if not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    try:
        state = job_queue.get_job(project_id)
        state["running"] = project_id in running_tasks and not running_tasks[project_id].done()

        project_dir = _project_dir(project_id)
        cached_cast = state.get("voice_cast")
        cast_revision = str(state.get("voice_cast_revision") or "")
        persisted_cast = None
        cast_path = project_dir / "voice_cast.json"
        if cast_path.is_file():
            try:
                persisted_cast = json.loads(cast_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, AttributeError):
                persisted_cast = None
        if persisted_cast and str(persisted_cast.get("fingerprint") or "") == cast_revision:
            active_cast = persisted_cast
        elif isinstance(cached_cast, dict):
            active_cast = cached_cast
        else:
            active_cast = persisted_cast

        if active_cast:
            state["voice_cast_summary"] = {
                "fingerprint": str(active_cast.get("fingerprint") or ""),
                "schema_version": active_cast.get("schema_version"),
                "voice_count": len(active_cast.get("voices", {})),
            }
        if "voice_cast" in state:
            state.pop("voice_cast", None)
            if cached_cast is not None:
                job_queue.update_job(project_id, {"voice_cast": None})

        workspace_dir = _workspace_project_dir(project_id)
        script_dir = project_dir / "script"
        manifests_dir = project_dir / "manifests"
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
                    state["cover_url"] = f"api/projects/{project_id}/cover?v={cover.stat().st_mtime_ns}"

                calc_total = len(b_chaps) or meta.get("total_chapters", 0)
                if total_chapters == 0 and calc_total > 0:
                    total_chapters = calc_total
                state["total_chapters"] = total_chapters

                for idx, ch in enumerate(b_chaps, 1):
                    if isinstance(ch, dict) and ch.get("title"):
                        book_chapter_titles[idx] = ch["title"]
            except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
                logger.warning(
                    "Could not read %s; the dashboard will show placeholder metadata and no chapter titles: %s",
                    book_json_path,
                    exc,
                )

        stage = str(state.get("active_stage") or state.get("status") or "").lower()
        scripted_chapters = set(state.get("scripted_chapters", []))
        # Chapters queued for voice re-generation should not be shown as generated/mastered
        # even if their audio segment files / master manifests are still on disk (they are stale).
        voice_revision_pending = set(state.get("voice_revision_pending_chapters", []))

        mastered_chapters = set(state.get("mastered_chapters", [])) - voice_revision_pending
        # Re-add from disk only for chapters NOT pending voice revision
        if manifests_dir.is_dir():
            for master_path in manifests_dir.glob("chapter_*.master.json"):
                match = re.match(r"chapter_(\d+)\.master\.json", master_path.name)
                if match:
                    c = int(match.group(1))
                    if c not in voice_revision_pending:
                        mastered_chapters.add(c)

        script_summary = _get_script_summary(project_id)

        def _has_valid_script(c_num: int) -> tuple[bool, int, str]:
            return script_summary.get(c_num, (False, 0, ""))

        generated_chapters = set(state.get("generated_chapters", [])) - voice_revision_pending
        for c_num, count in segment_counts.items():
            if c_num in voice_revision_pending:
                continue
            has_s, t_lines, _ = _has_valid_script(c_num)
            if has_s and t_lines > 0 and count >= t_lines:
                generated_chapters.add(c_num)

        # If the pipeline is actively re-scripting a chapter, demote it from generated/mastered
        # regardless of what files are on disk — the pipeline has declared it stale.
        active_script_ch = state.get("current_script_chapter")
        if active_script_ch and stage and "script" in stage:
            generated_chapters.discard(int(active_script_ch))
            mastered_chapters.discard(int(active_script_ch))

        state["generated_chapters"] = sorted(generated_chapters)
        state["mastered_chapters"] = sorted(mastered_chapters)

        # Dynamically infer the next active scripting chapter if existing valid scripts exist
        existing_scripted = [
            c for c in range(1, total_chapters + 1) if _has_valid_script(c)[0] or c in scripted_chapters
        ]
        if existing_scripted:
            current_script_ch = min(max(existing_scripted) + 1, total_chapters)
        else:
            current_script_ch = state.get("current_script_chapter") or 1

        work_prog = state.get("work_progress") or {}

        for ch_num in range(1, total_chapters + 1):
            has_script, total_lines, script_title = _has_valid_script(ch_num)
            raw_title = (book_chapter_titles.get(ch_num) or script_title or "").strip()
            title = raw_title if raw_title else f"Chapter {ch_num}"

            gen_count = segment_counts[ch_num] if total_lines > 0 else 0
            if ch_num in generated_chapters and total_lines > 0:
                gen_count = max(gen_count, total_lines)
            validated_count = (
                total_lines
                if ch_num in generated_chapters
                else min(int(state.get("lines_validated") or 0), gen_count)
                if ch_num == state.get("current_gen_chapter")
                else min(gen_count, total_lines)
            )

            # Compute stage-aware progress percentage
            if ch_num in mastered_chapters or ch_num in generated_chapters:
                pct = 100
            elif "script" in stage or stage in ["voice_review", "bootstrapping"]:
                if has_script or ch_num in scripted_chapters:
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
                            except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
                                logger.debug(
                                    "Could not read %s for chunk progress; the chapter will show no chunk counter: %s",
                                    log_file,
                                    exc,
                                )
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
            elif has_script or ch_num in scripted_chapters:
                pct = 100
            else:
                pct = 0

            chapter_details.append(
                {
                    "number": ch_num,
                    "title": title,
                    "total_lines": total_lines,
                    "lines_generated": gen_count,
                    "lines_validated": min(validated_count, total_lines),
                    "progress_percent": min(max(pct, 0), 100),
                }
            )

        state["chapter_details"] = chapter_details
        return state
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc


# ---------------------------------------------------------------------------
# Feature Expansion Endpoints (Schedule, Metadata, Deploy, Selective)
# ---------------------------------------------------------------------------


@app.post("/api/schedule")
async def update_schedule(request: Request):
    """Update schedule config in brain/config.yaml."""
    data = await request.json()
    enabled = bool(data.get("enabled", False))
    timezone_name = str(data.get("timezone", "Europe/Bucharest")).strip()
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown timezone: {timezone_name}",
        ) from exc
    valid_day_order = (
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
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
        if not isinstance(days, list) or not days or any(day not in valid_days for day in days):
            raise HTTPException(
                status_code=422,
                detail=(f"Window {index + 1} must select at least one valid weekday"),
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
    config_path = shared_paths.BRAIN_CONFIG_PATH
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
    block = (
        yaml.safe_dump(
            {section: value},
            sort_keys=False,
            allow_unicode=True,
        ).rstrip()
        + "\n"
    )
    section_pattern = re.compile(rf"(?ms)^{re.escape(section)}:\s*\n.*?(?=^[A-Za-z_][\w-]*:\s*(?:#.*)?$|\Z)")
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


def _caller(request: Request) -> str:
    """Describe who asked for a lifecycle action.

    The shutdown, restart and release-gpu endpoints all interrupt running work
    and are reachable from the UI, Home Assistant, the desktop shell and two
    PowerShell helpers. When one of them fires unexpectedly the logs show only
    the effect, never the origin, which makes the cause guesswork.
    """
    client = request.client.host if request.client else "unknown"
    agent = request.headers.get("user-agent", "no user-agent")
    return f"{client} ({agent[:80]})"


@app.post("/api/system/shutdown", status_code=202)
async def shutdown_dashboard(request: Request):
    """Gracefully stop the dashboard after normal API authentication."""
    global _dashboard_shutdown_task

    logger.warning("Shutdown requested by %s", _caller(request))

    if _dashboard_shutdown_task and not _dashboard_shutdown_task.done():
        return {
            "status": "already_shutting_down",
            "pid": os.getpid(),
        }

    _dashboard_shutdown_task = asyncio.create_task(_shutdown_dashboard_process())
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


def _pronunciation_llm():
    """Return the pipeline's Ollama client, if one is available.

    Routing recommendation lookups through it gives them the retry budget,
    repetition-loop detection and cooperative cancellation that a bare HTTP
    call to Ollama does not have.
    """
    return getattr(pipeline, "ollama", None) if pipeline else None


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
        cache_hours = float(load_config().get("metadata", {}).get("cache_hours", 24))
        age_seconds = (datetime.now(UTC) - fetched_at).total_seconds()
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
            "fetched_at": datetime.now(UTC).isoformat(),
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
    payload["existing_cover"] = _existing_project_cover(_project_dir(project_id), metadata) is not None
    payload["cover_preview_url"] = (
        f"api/projects/{project_id}/metadata-candidate/cover" if candidate.get("has_cover") else None
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


def _refresh_exported_audiobook_metadata_unlocked(
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
            "Book details were saved, but FFmpeg is unavailable to refresh the existing audiobook package"
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
                "-map_metadata",
                "0",
                "-map_chapters",
                "0",
                "-c",
                "copy",
                "-metadata",
                f"title={metadata.get('title') or ''}",
                "-metadata",
                f"artist={metadata.get('author') or ''}",
                "-metadata",
                f"album={metadata.get('title') or ''}",
                "-metadata",
                f"genre={metadata.get('genre') or ''}",
                "-metadata",
                f"date={metadata.get('year') or ''}",
                "-metadata",
                f"comment={metadata.get('description') or ''}",
            ]
        )
        isbn = str(metadata.get("isbn") or "").strip()
        if isbn:
            # M4B has no universally supported ISBN atom. The standard
            # grouping atom survives across players without hiding cover art.
            command.extend(
                [
                    "-metadata",
                    f"isbn={isbn}",
                    "-metadata",
                    f"grouping=ISBN {isbn}",
                ]
            )
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


def _refresh_exported_audiobook_metadata(
    project_id: str,
    metadata: dict[str, Any],
) -> list[str]:
    """Serialize remuxing and invalidate immutable incremental publications."""
    project_dir = _project_dir(project_id)
    manager = DeliveryManager(project_dir)
    with manager.packaging_lock(wait=True):
        refreshed = _refresh_exported_audiobook_metadata_unlocked(
            project_id,
            metadata,
        )
        manager.mark_all_stale("Book metadata or cover changed; republish this delivery")
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
        candidate = None if refresh or provider_id else _read_metadata_candidate(project_dir, cache_key)
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
            refreshed_exports = _refresh_exported_audiobook_metadata(project_id, metadata)
        except Exception:
            if job_queue:
                try:
                    job_queue.update_job(project_id, {"export_metadata_stale": True})
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
                        "export_metadata_refreshed_at": datetime.now(UTC).isoformat(),
                    },
                )
            except KeyError:
                pass
        response = _metadata_response(project_id, candidate, metadata, applied=True)
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
        invalid = [chapter for chapter in selection if chapter < 1 or chapter > total]
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
                with open(log_file, encoding="utf-8") as f:
                    all_lines = [line.rstrip() for line in f if line.strip()]
                    lines = all_lines[-500:]
                    # Hydrate RAM buffer
                    _project_logs[project_id] = collections.deque(lines, maxlen=500)
            except Exception as exc:
                logger.warning("Could not read %s; the log view will come back empty: %s", log_file, exc)
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
                except TimeoutError:
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
    """Get the generated script for a project, auto-syncing from chapter scripts if newer."""
    from shared.artifacts import atomic_write_text
    from shared.models import BookScript, CharacterRegistry, ExtractedBook

    project_dir = _project_dir(project_id)
    script_path = project_dir / "book_script.json"
    scripts_dir = project_dir / "script"

    if scripts_dir.exists():
        chapter_files = [p for p in sorted(scripts_dir.glob("chapter_*.json")) if not p.name.endswith(".meta.json")]
        if chapter_files:
            needs_resync = not script_path.exists()
            if not needs_resync:
                bs_mtime = script_path.stat().st_mtime
                needs_resync = any(cf.stat().st_mtime > bs_mtime for cf in chapter_files)
            if needs_resync:
                try:
                    book_file = project_dir / "book.json"
                    char_file = project_dir / "characters.json"
                    if book_file.exists() and char_file.exists():
                        book = ExtractedBook.model_validate_json(book_file.read_text(encoding="utf-8"))
                        registry = CharacterRegistry.model_validate_json(char_file.read_text(encoding="utf-8"))
                        chapter_scripts = [
                            ScriptChapter.model_validate_json(cf.read_text(encoding="utf-8")) for cf in chapter_files
                        ]
                        book_script = BookScript(
                            metadata=book.metadata,
                            character_registry=registry,
                            chapters=chapter_scripts,
                        )
                        atomic_write_text(
                            script_path,
                            book_script.model_dump_json(indent=2),
                        )
                except Exception as exc:
                    logger.warning("Could not auto-sync book_script.json: %s", exc)

    if not script_path.exists():
        raise HTTPException(status_code=404, detail="Script not generated yet")
    return FileResponse(str(script_path), media_type="application/json")


def _invalidate_chapter_after_script_change(
    project_id: str,
    project_dir: Path,
    chapter_number: int,
    reason: str,
) -> None:
    """Invalidate all durable outputs that depend on one chapter script."""
    state = job_queue.get_job(project_id)
    updates: dict[str, Any] = {"export_stale": True}
    for key in ("generated_chapters", "mastered_chapters"):
        updates[key] = [number for number in state.get(key, []) if number != chapter_number]
    job_queue.update_job(project_id, updates)

    manifests_dir = project_dir / "manifests"
    for manifest in (
        manifests_dir / f"chapter_{chapter_number:03d}.segments.json",
        manifests_dir / f"chapter_{chapter_number:03d}.master.json",
    ):
        manifest.unlink(missing_ok=True)
    DeliveryManager(project_dir).mark_stale_for_chapters(
        {chapter_number},
        reason,
    )


@app.patch("/api/projects/{project_id}/script/chapter/{chapter_number}/line/{line_id}")
async def update_script_line(
    project_id: str,
    chapter_number: int,
    line_id: str,
    request: ScriptLineUpdate,
):
    """Update a specific line in a chapter's script."""
    _require_project_stopped(project_id)
    project_dir = _project_dir(project_id)
    script_dir = project_dir / "script"
    if not script_dir.exists():
        raise HTTPException(status_code=404, detail="Script directory not found")

    chapter_file = script_dir / f"chapter_{chapter_number:03d}.json"
    if not chapter_file.exists():
        raise HTTPException(status_code=404, detail=f"Chapter {chapter_number} script not found")

    characters_path = project_dir / "characters.json"
    if not characters_path.is_file():
        raise HTTPException(status_code=409, detail="Character registry is missing")
    characters = json.loads(characters_path.read_text(encoding="utf-8")).get("characters", {})
    if request.speaker not in characters:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown character speaker '{request.speaker}'",
        )

    original_chapter_text = chapter_file.read_text(encoding="utf-8")
    data = json.loads(original_chapter_text)
    updated = False
    for line in data.get("lines", []):
        if str(line.get("id")) == line_id or str(line.get("line_id")) == line_id:
            line["speaker"] = request.speaker
            line["speaker_confidence"] = 1.0
            line["speaker_evidence"] = "Human-reviewed speaker assignment."
            line["attribution_resolver"] = "human"
            line.setdefault("attribution_confidence_history", []).append(
                {
                    "resolver": "human",
                    "decision": "resolved",
                    "speaker_id": request.speaker,
                    "confidence": 1.0,
                    "reason": "Human-reviewed speaker assignment.",
                }
            )
            line["attribution_review_required"] = False
            line["attribution_review_reason"] = ""
            updated = True
            break

    if not updated:
        raise HTTPException(status_code=404, detail=f"Line {line_id} not found in chapter {chapter_number}")

    atomic_write_json(chapter_file, data)

    # We must also update the merged book_script.json if it exists
    merged_file = project_dir / "book_script.json"
    if merged_file.exists():
        try:
            merged_data = json.loads(merged_file.read_text(encoding="utf-8"))
            chapter = next(
                (
                    candidate
                    for candidate in merged_data.get("chapters", [])
                    if candidate.get("chapter_number") == chapter_number
                ),
                None,
            )
            merged_updated = False
            if chapter is not None:
                for merged_line in chapter.get("lines", []):
                    if str(merged_line.get("id")) == line_id or str(merged_line.get("line_id")) == line_id:
                        merged_line["speaker"] = request.speaker
                        merged_line["speaker_confidence"] = 1.0
                        merged_line["speaker_evidence"] = "Human-reviewed speaker assignment."
                        merged_line["attribution_resolver"] = "human"
                        merged_line.setdefault("attribution_confidence_history", []).append(
                            {
                                "resolver": "human",
                                "decision": "resolved",
                                "speaker_id": request.speaker,
                                "confidence": 1.0,
                                "reason": "Human-reviewed speaker assignment.",
                            }
                        )
                        merged_line["attribution_review_required"] = False
                        merged_line["attribution_review_reason"] = ""
                        merged_updated = True
                        break
            if not merged_updated:
                atomic_write_text(chapter_file, original_chapter_text)
                raise HTTPException(
                    status_code=409,
                    detail="Merged script does not contain the selected line",
                )
            atomic_write_json(merged_file, merged_data)
        except (OSError, ValueError, TypeError) as exc:
            atomic_write_text(chapter_file, original_chapter_text)
            raise HTTPException(
                status_code=500,
                detail=f"Could not update merged script: {exc}",
            ) from exc

    _invalidate_chapter_after_script_change(
        project_id,
        project_dir,
        chapter_number,
        "Speaker attribution changed",
    )
    (project_dir / "attribution_audit.json").unlink(missing_ok=True)
    job_queue.reconcile_external_validation(project_id, "attribution", line_id, "resolved", request.speaker)
    auto_resuming = _schedule_resume_after_reviews(project_id)
    return {
        "status": "success",
        "message": "Script line updated",
        "auto_resuming": auto_resuming,
    }


@app.post("/api/projects/{project_id}/chapters/{chapter_number}/regenerate")
async def regenerate_chapter(project_id: str, chapter_number: int):
    """Delete a chapter's script and fingerprint to force regeneration."""
    state = _require_project_stopped(project_id)
    project_dir = _project_dir(project_id)

    # 1. Delete script file
    script_file = project_dir / "script" / f"chapter_{chapter_number:03d}.json"
    try:
        script_file.unlink(missing_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Could not remove chapter script: {exc}",
        ) from exc

    # 2. Delete from fingerprints
    fingerprint_file = project_dir / ".fingerprints.json"
    if fingerprint_file.exists():
        try:
            data = json.loads(fingerprint_file.read_text(encoding="utf-8"))
            if "chapters" in data and str(chapter_number) in data["chapters"]:
                del data["chapters"][str(chapter_number)]
                atomic_write_json(fingerprint_file, data)
        except (OSError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    # 3. Remove from mastered/generated chapters in pipeline state
    state_file = project_dir / "pipeline.json"
    if state_file.exists():
        try:
            state_data = json.loads(state_file.read_text(encoding="utf-8"))
            for key in ["scripted_chapters", "generated_chapters", "mastered_chapters"]:
                if key in state_data and chapter_number in state_data[key]:
                    state_data[key].remove(chapter_number)
            state_data["export_stale"] = True
            atomic_write_json(state_file, state_data)
        except (OSError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    updates = {
        key: [number for number in state.get(key, []) if number != chapter_number]
        for key in ["scripted_chapters", "generated_chapters", "mastered_chapters"]
    }
    updates["export_stale"] = True
    job_queue.update_job(project_id, updates)

    # 3.5 Remove from merged book_script.json so the UI doesn't show stale lines
    merged_script = project_dir / "book_script.json"
    if merged_script.exists():
        merged_data = json.loads(merged_script.read_text(encoding="utf-8"))
        if "chapters" in merged_data:
            merged_data["chapters"] = [
                chapter for chapter in merged_data["chapters"] if chapter.get("chapter_number") != chapter_number
            ]
            atomic_write_json(merged_script, merged_data)

    # 4. Delete audio folder if it exists
    audio_dir = project_dir / "audio" / f"chapter_{chapter_number:03d}"
    if audio_dir.exists():
        shutil.rmtree(audio_dir)

    _invalidate_chapter_after_script_change(
        project_id,
        project_dir,
        chapter_number,
        "Chapter script queued for regeneration",
    )

    return {"status": "success", "message": f"Chapter {chapter_number} queued for regeneration"}


@app.get("/api/projects/{project_id}/characters")
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
        voice_id for voice_id, profile in cast.get("voices", {}).items() if profile.get("assigned_characters")
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
                f"Every speaking voice needs a valid preview before approval. Missing: {', '.join(sorted(missing))}"
            ),
        )

    required_pairs, distinctness_stale = _cast_distinctness_review(
        cast,
        required_voice_ids,
    )
    if (required_pairs or distinctness_stale) and not request.acknowledge_similar_pairs:
        pair_labels = [f"{item['left_voice_id']} / {item['right_voice_id']}" for item in required_pairs[:8]]
        raise HTTPException(
            status_code=409,
            detail={
                "code": "similar_cast_pairs_require_acknowledgement",
                "message": (
                    "Cast distinctness needs explicit review because required "
                    "voices are acoustically similar or changed after the last "
                    "comparison. Preview or replace them, or explicitly "
                    "acknowledge the cast before continuing."
                ),
                "pairs": pair_labels,
                "distinctness_stale": distinctness_stale,
            },
        )

    approved_at = datetime.now(UTC).isoformat()
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
    summary = summarize_metrics(read_metrics(project_dir / "performance_metrics.jsonl"))
    if format == "json":
        return summary
    buffer = io.StringIO()
    fields = [
        "chapter_number",
        "segments",
        "synthesis_cache_hits",
        "synthesis_cache_misses",
        "validation_cache_hits",
        "validation_cache_misses",
        "retries",
        "accepted_with_warning",
        "failed_validation",
        "audio_duration_seconds",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(summary["chapters"])
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{project_id}-metrics.csv"'},
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
    voice_config = shared_paths.voice_config()
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
                summarize_metrics(read_metrics(project_dir / "performance_metrics.jsonl")),
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
        managed_log = shared_paths.PROJECTS_DIR / "voice-server-managed.log"
        if managed_log.is_file():
            archive.writestr("voice-server-managed.log", managed_log.read_bytes()[-1_000_000:])
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


@app.get("/api/projects/{project_id}/segments/{line_id}/candidates")
async def get_segment_candidates(project_id: str, line_id: str):
    """Return the retained A/B candidates, ranked best first."""
    _require_job(project_id)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", line_id):
        raise HTTPException(status_code=400, detail="Invalid line ID")
    rows = list_candidates(_project_dir(project_id), line_id)
    for row in rows:
        row["audio_url"] = f"api/projects/{project_id}/segments/{line_id}/candidates/{quote(row.pop('filename'))}"
    return {"candidates": rows}


@app.get("/api/projects/{project_id}/segments/{line_id}/candidates/{filename}")
async def get_segment_candidate_audio(project_id: str, line_id: str, filename: str):
    """Stream one bounded retained candidate."""
    _require_job(project_id)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", line_id):
        raise HTTPException(status_code=400, detail="Invalid line ID")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.wav", filename):
        raise HTTPException(status_code=400, detail="Invalid candidate filename")
    root = (_project_dir(project_id) / "review_candidates" / line_id).resolve()
    path = (root / filename).resolve()
    if path.parent != root or not path.is_file():
        raise HTTPException(status_code=404, detail="Candidate audio not found")
    return FileResponse(path=path, filename=path.name, media_type="audio/wav")


@app.post("/api/system/release-gpu")
async def release_gpu(request: Request):
    """Release all app-owned GPU resources."""
    logger.warning("GPU release requested by %s", _caller(request))
    await _release_gpu_resources()
    return {"status": "success", "message": "GPU resources released"}


@app.get("/api/projects/{project_id}/external-validation/status")
@app.post("/api/projects/{project_id}/external-validation/retry")
@app.post("/api/system/restart")
async def restart_dashboard_server(request: Request):
    """Start the controlled restart helper, then release this API process."""
    global _dashboard_shutdown_task

    logger.warning("Restart requested by %s", _caller(request))
    if _dashboard_shutdown_task is not None and not _dashboard_shutdown_task.done():
        return {
            "status": "already_restarting",
            "message": "Dashboard process is already restarting",
        }
    try:
        restart_task = _launch_dashboard_restart_helper()
    except Exception as exc:
        logger.exception("Could not launch dashboard restart helper")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "status": "restarting",
        "message": "Dashboard process is restarting",
        "restart_task": restart_task,
    }


# ---------------------------------------------------------------------------
# Incremental Delivery
# ---------------------------------------------------------------------------


class DeliverySettingsRequest(BaseModel):
    enabled: bool
    batch_size: int = Field(default=5, ge=1, le=20)


@app.patch("/api/projects/{project_id}/delivery-settings")
async def update_delivery_settings(project_id: str, request: DeliverySettingsRequest):
    _require_job(project_id)
    project_dir = _project_dir(project_id)
    state = job_queue.get_job(project_id)
    dm = DeliveryManager(project_dir)
    index = dm.load_index()

    is_running = bool(state.get("running")) or (project_id in running_tasks and not running_tasks[project_id].done())
    if index.batch_size != request.batch_size and not is_running:
        index.batch_size = request.batch_size
        for part in index.deliveries:
            part.status = "stale"
            part.stale_reason = f"Batch size changed to {request.batch_size}"
        dm.save_index(index)

    settings = dict(state.get("incremental_delivery") or {})
    settings["enabled"] = request.enabled
    settings["batch_size"] = request.batch_size

    # Also update pipeline.json on disk if present to persist settings across restarts
    state_file = project_dir / "pipeline.json"
    if state_file.is_file():
        try:
            persisted = json.loads(state_file.read_text(encoding="utf-8"))
            persisted["incremental_delivery"] = settings
            atomic_write_json(state_file, persisted)
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Could not persist delivery settings to pipeline.json: %s", exc)

    job_queue.update_job(project_id, {"incremental_delivery": settings})

    return {
        "status": "success",
        "settings": settings,
        "applies_after_current_run": is_running,
        "active_batch_size": index.batch_size if is_running else request.batch_size,
    }


@app.get("/api/projects/{project_id}/deliveries")
async def get_deliveries(project_id: str):
    _require_job(project_id)
    project_dir = _project_dir(project_id)
    state = job_queue.get_job(project_id)
    dm = DeliveryManager(project_dir)
    index = dm.load_index()

    return {
        "settings": state.get("incremental_delivery") or {"enabled": False, "batch_size": 5},
        "active_delivery_id": state.get("active_delivery_id"),
        "active_delivery_chapters": state.get("active_delivery_chapters") or [],
        "pause_after_delivery_requested": bool(state.get("pause_after_delivery_requested")),
        "published_count": sum(part.status == "published" for part in index.deliveries),
        "deliveries": [p.model_dump() for p in index.deliveries],
    }


@app.get("/api/projects/{project_id}/deliveries/{delivery_id}/download")
async def download_delivery(project_id: str, delivery_id: str):
    _require_job(project_id)
    return await download_audiobook(project_id, delivery_id=delivery_id)


@app.post("/api/projects/{project_id}/pause-after-delivery")
async def request_pause_after_delivery(project_id: str):
    state = _require_job(project_id)
    settings = state.get("incremental_delivery") or {}
    if not isinstance(settings, dict) or not settings.get("enabled"):
        raise HTTPException(status_code=409, detail="Incremental delivery is not enabled")
    if not state.get("running"):
        raise HTTPException(status_code=409, detail="Pipeline is not running")
    job_queue.update_job(project_id, {"pause_after_delivery_requested": True})
    return {"status": "success"}


@app.delete("/api/projects/{project_id}/pause-after-delivery")
async def cancel_pause_after_delivery(project_id: str):
    state = _require_job(project_id)
    settings = state.get("incremental_delivery") or {}
    if not isinstance(settings, dict) or not settings.get("enabled"):
        raise HTTPException(status_code=409, detail="Incremental delivery is not enabled")
    job_queue.update_job(project_id, {"pause_after_delivery_requested": False})
    return {"status": "success"}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


@app.websocket("/ws/updates")
async def websocket_updates(websocket: WebSocket):
    """WebSocket for real-time pipeline updates."""
    token = configured_dashboard_token(_dashboard_cfg)
    presented_token = websocket.headers.get("X-API-Token") or websocket.query_params.get("token")
    client_host = websocket.client.host if websocket.client else None
    if not dashboard_request_authorized(
        client_host=client_host,
        configured_token=token,
        presented_token=presented_token,
        trusted_lan_cidrs=_TRUSTED_LAN_CIDRS,
    ):
        await websocket.close(code=1013 if not token and not is_loopback_client(client_host) else 1008)
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
    # `_assert_safe_bind` already ran at import against the configured host.
    # Re-check here so an explicit `--host` override cannot widen exposure.
    _assert_safe_bind({**dashboard_cfg, "host": host})

    # Console plus a rotating file. Under Task Scheduler the console goes
    # nowhere, so until now anything the dashboard logged outside a project's
    # own handler was simply lost -- which is why three interrupted runs on
    # 2026-09-04 could not be traced to whatever asked for them.
    log_path = shared_paths.PROJECTS_DIR / "dashboard.log"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_path,
                maxBytes=8 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
        )
    except OSError as exc:
        # Console-only is a working dashboard; refusing to start over a log
        # file would not be.
        print(f"Could not open {log_path} for logging; console only: {exc}")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        handlers=handlers,
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
