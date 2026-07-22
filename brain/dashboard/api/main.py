"""Brain Dashboard API — FastAPI application.

Serves the web dashboard and orchestrates the audiobook pipeline.
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
import collections
import logging
import queue
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from brain.orchestrator.pipeline import Pipeline
from brain.orchestrator.job_queue import JobQueue
from brain.orchestrator.watchdog import ServiceWatchdog
from shared.constants import PipelineStage

logger = logging.getLogger(__name__)

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
watchdog: ServiceWatchdog | None = None
ws_connections: list[WebSocket] = []
running_tasks: dict[str, asyncio.Task] = {}

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
    global pipeline, job_queue, watchdog

    config = load_config()
    pipeline = Pipeline(config_path="brain/config.yaml")
    job_queue = pipeline.job_queue
    
    watchdog = ServiceWatchdog(check_interval_seconds=60)
    watchdog.start()

    logging.getLogger().setLevel(logging.INFO)
    logger.info("Brain Dashboard starting...")

    yield

    # Cleanup
    if watchdog:
        await watchdog.stop()
    if pipeline:
        pipeline.ollama.close()
        pipeline.ubuntu.close()


app = FastAPI(
    title="Crazy Audiobook Creator — Brain Dashboard",
    description="Pipeline orchestration and monitoring dashboard",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Static files (frontend)
# ---------------------------------------------------------------------------

frontend_dir = Path(__file__).parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")


@app.get("/")
async def serve_dashboard():
    """Serve the dashboard home page."""
    index_path = frontend_dir / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return JSONResponse(
        {"message": "Dashboard frontend not found. Place files in brain/dashboard/frontend/"},
        status_code=404,
    )


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

    # Save uploaded file temporarily
    temp_dir = Path("brain/projects/_uploads")
    temp_dir.mkdir(parents=True, exist_ok=True)
    safe_filename = Path(file.filename).name
    temp_path = temp_dir / safe_filename

    try:
        content = await file.read()
        temp_path.write_bytes(content)
        logger.info("[DashboardAPI] Uploaded EPUB '%s' (%d bytes) for project creation", file.filename, len(content))

        status = pipeline.create_project(str(temp_path))
        logger.info("[DashboardAPI] Created project '%s' (%d chapters detected)", status.project_id, status.total_chapters)

        # Automatically fetch metadata and artwork in background
        asyncio.create_task(asyncio.to_thread(_auto_fetch_metadata_sync, status.project_id))

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


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str):
    """Get project details."""
    if not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    try:
        return job_queue.get_job(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found")


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    """Delete a project."""
    if not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    try:
        job_queue.delete_job(project_id)
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

    # Clear old deployment request flag and set running=True immediately
    job_queue.update_job(project_id, {"deployment_requested": False, "running": True})

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
    """Stop a running pipeline."""
    if not pipeline or not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
        
    try:
        job_queue.update_job(project_id, {"status": PipelineStage.PAUSED.value})
    except Exception:
        pass

    pipeline.stop(project_id)
    return {"status": "stopped", "project_id": project_id}


@app.post("/api/projects/{project_id}/reset")
async def reset_pipeline_stage(project_id: str, request: Request):
    """Reset the pipeline to a specific stage."""
    if not job_queue:
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
        
    try:
        job_queue.update_job(project_id, {"status": stage.value})
        return {"status": "success", "project_id": project_id, "stage": stage.value}
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found")


@app.get("/api/projects/{project_id}/download")
async def download_audiobook(project_id: str):
    """Download the final mastered audiobook."""
    m4b_path = Path(f"brain/projects/{project_id}/{project_id}.m4b")
    if not m4b_path.exists():
        m4b_path = Path(f"workspace/{project_id}/output/{project_id}.m4b")
    if not m4b_path.exists():
        project_dir = Path(f"brain/projects/{project_id}")
        partials = sorted(project_dir.glob("*.m4b"), key=lambda p: p.stat().st_mtime)
        if not partials:
            project_dir_ws = Path(f"workspace/{project_id}")
            partials = sorted(project_dir_ws.glob("**/*.m4b"), key=lambda p: p.stat().st_mtime)
        if partials:
            m4b_path = partials[-1]
        else:
            raise HTTPException(status_code=404, detail="Audiobook file not found")
        
    return FileResponse(
        path=m4b_path,
        filename=m4b_path.name,
        media_type="audio/mp4"
    )


@app.get("/api/projects/{project_id}/download/chapter/{chapter_num}")
async def download_chapter_audio(project_id: str, chapter_num: int):
    """Download the mastered WAV file for a specific chapter."""
    ch_file = Path(f"workspace/{project_id}/chapters/chapter_{chapter_num:03d}.wav")
    if not ch_file.exists():
        ch_file = Path(f"brain/projects/{project_id}/chapters/chapter_{chapter_num:03d}.wav")
    if not ch_file.exists():
        raise HTTPException(status_code=404, detail=f"Chapter {chapter_num} mastered audio not found")
    return FileResponse(
        path=ch_file,
        filename=f"{project_id}_chapter_{chapter_num:03d}.wav",
        media_type="audio/wav"
    )


@app.get("/api/projects/{project_id}/status")
async def get_pipeline_status(project_id: str):
    """Get the current pipeline status."""
    if not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    try:
        state = job_queue.get_job(project_id)
        state["running"] = (
            project_id in running_tasks
            and not running_tasks[project_id].done()
        )
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
    config_path = Path("brain/config.yaml")
    cfg = {}
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    cfg["schedule"] = data
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f)
    if pipeline:
        pipeline.config = pipeline._load_config()
    return {"status": "success", "schedule": data}


def _auto_fetch_metadata_sync(project_id: str) -> None:
    """Helper to automatically fetch artwork and description in background."""
    try:
        project_dir = Path("brain/projects") / project_id
        book_json = project_dir / "book.json"
        if not book_json.exists():
            return
        import json
        from brain.extractor.metadata_fetcher import MetadataFetcher
        book_data = json.loads(book_json.read_text(encoding="utf-8"))
        meta = book_data.get("metadata", {})
        fetched = MetadataFetcher.fetch(meta.get("title", ""), meta.get("author", ""))

        if fetched.cover_image_bytes:
            cover_file = project_dir / "cover.jpg"
            cover_file.write_bytes(fetched.cover_image_bytes)
            book_data["metadata"]["cover_image_path"] = str(cover_file)

        if fetched.description:
            book_data["metadata"]["description"] = fetched.description

        book_json.write_text(json.dumps(book_data, indent=2), encoding="utf-8")
        logger.info("Auto-fetched artwork/metadata for project %s", project_id)
    except Exception as e:
        logger.warning("Auto metadata fetch failed for %s: %s", project_id, e)


@app.post("/api/projects/{project_id}/fetch-metadata")
async def fetch_project_metadata(project_id: str):
    """Fetch cover image and metadata from Google Books API."""
    if not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    project_dir = Path("brain/projects") / project_id
    book_json = project_dir / "book.json"
    if not book_json.exists():
        raise HTTPException(status_code=404, detail="Project book.json not found")

    import json
    from brain.extractor.metadata_fetcher import MetadataFetcher
    book_data = json.loads(book_json.read_text(encoding="utf-8"))
    meta = book_data.get("metadata", {})
    fetched = await asyncio.to_thread(MetadataFetcher.fetch, meta.get("title", ""), meta.get("author", ""))

    cover_path = None
    if fetched.cover_image_bytes:
        cover_file = project_dir / "cover.jpg"
        cover_file.write_bytes(fetched.cover_image_bytes)
        cover_path = str(cover_file)
        book_data["metadata"]["cover_image_path"] = cover_path

    if fetched.description:
        book_data["metadata"]["description"] = fetched.description

    book_json.write_text(json.dumps(book_data, indent=2), encoding="utf-8")

    return {
        "status": "success",
        "title": fetched.title,
        "author": fetched.author,
        "description": fetched.description,
        "cover_path": cover_path,
    }


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
async def set_chapter_selection(project_id: str, request: Request):
    """Set which chapters to generate in the next run."""
    if not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    data = await request.json()
    selection = data.get("chapters")
    job_queue.update_job(project_id, {"generation_chapter_selection": selection})
    return {"status": "success", "project_id": project_id, "selection": selection}


@app.post("/api/projects/{project_id}/export-partial")
async def export_partial_m4b(project_id: str):
    """Trigger a partial M4B export with all currently mastered chapters."""
    if not pipeline:
        raise HTTPException(status_code=503, detail="Server not initialized")
    project_dir = Path("brain/projects") / project_id
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
        log_file = Path("brain/projects") / project_id / "pipeline.log"
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
    script_path = Path(f"brain/projects/{project_id}/book_script.json")
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="Script not generated yet")
    return FileResponse(str(script_path), media_type="application/json")


@app.get("/api/projects/{project_id}/characters")
async def get_characters(project_id: str):
    """Get the character registry for a project."""
    chars_path = Path(f"brain/projects/{project_id}/characters.json")
    if not chars_path.exists():
        raise HTTPException(status_code=404, detail="Characters not analyzed yet")
    return FileResponse(str(chars_path), media_type="application/json")


@app.get("/api/projects/{project_id}/quality")
async def get_quality_report(project_id: str):
    """Get quality report for a project."""
    if not job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    logs = job_queue.get_quality_report(project_id)
    
    summary = {
        "total_segments": 0,
        "passed_segments": 0,
        "retries_triggered": 0,
        "average_wer": 0.0,
        "failed_silence": 0,
        "failed_clipping": 0
    }
    
    if not logs:
        return summary

    # Group by line_id to get the final attempt
    lines = {}
    for log in logs:
        line_id = log["line_id"]
        if line_id not in lines or log["attempt"] > lines[line_id]["attempt"]:
            lines[line_id] = log
        if log["attempt"] > 1:
            summary["retries_triggered"] += 1
            
    summary["total_segments"] = len(lines)
    total_wer = 0.0
    
    for line in lines.values():
        if line["status"] == "pass":
            summary["passed_segments"] += 1
        total_wer += line["wer"] or 0.0
        
        details = line.get("details", {})
        if details.get("silence_issues", False):
            summary["failed_silence"] += 1
        if details.get("clipping_issues", False):
            summary["failed_clipping"] += 1
            
    if summary["total_segments"] > 0:
        summary["average_wer"] = total_wer / summary["total_segments"]
        
    return summary


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


@app.websocket("/ws/updates")
async def websocket_updates(websocket: WebSocket):
    """WebSocket for real-time pipeline updates."""
    await websocket.accept()
    ws_connections.append(websocket)
    logger.info("Dashboard WebSocket client connected")

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_connections.remove(websocket)
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

    host = args.host or dashboard_cfg.get("host", "0.0.0.0")
    port = args.port or dashboard_cfg.get("port", 8000)

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
