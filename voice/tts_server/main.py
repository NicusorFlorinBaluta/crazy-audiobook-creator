"""Voice TTS Server — FastAPI application.

The main entry point for the Ubuntu TTS server that handles:
  - Voice bootstrapping (POST /voices/bootstrap)
  - Single line generation (POST /generate/line)
  - Chapter generation (POST /generate/chapter)
  - Audio validation (POST /validate)
  - Chapter mastering (POST /master/chapter)
  - M4B export (POST /export/m4b)
  - Health check (GET /health)
  - Voice library listing (GET /voices/{project_id})
  - File download (GET /download/{project_id}/{path})
  - WebSocket progress updates (WS /ws/progress)
"""

from __future__ import annotations

import logging
import asyncio
import os
import threading
import time
from contextlib import asynccontextmanager
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

os.environ.setdefault("ROCM_SDK_TARGET_FAMILY", "custom")

from voice.tts_server.qwen3_engine import Qwen3TTSEngine
from voice.tts_server.voice_designer import VoiceDesigner
from voice.tts_server.voice_library import VoiceLibraryManager
from voice.validator.whisper_validator import WhisperValidator
from voice.validator.audio_analyzer import AudioAnalyzer
from voice.validator.validation_loop import GenerationCancelled, ValidationLoop
from voice.mastering.assembler import AudioAssembler
from voice.mastering.normalizer import LoudnessNormalizer
from voice.mastering.m4b_exporter import M4BExporter
from shared.models import (
    BootstrapVoicesRequest,
    BootstrapVoicesResponse,
    GenerateChapterRequest,
    GenerateChapterResponse,
    GenerateLineRequest,
    GenerateLineResponse,
    MasterChapterRequest,
    MasterChapterResponse,
    ExportM4BRequest,
    ExportM4BResponse,
    ValidateRequest,
    QualityResult,
    VoiceHealthResponse,
    ChapterQualityReport,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

engine: Qwen3TTSEngine | None = None
designer: VoiceDesigner | None = None
library: VoiceLibraryManager | None = None
validator: ValidationLoop | None = None
assembler: AudioAssembler | None = None
normalizer: LoudnessNormalizer | None = None
exporter: M4BExporter | None = None
config: dict[str, Any] = {}
start_time: float = 0.0
last_activity: float = 0.0
active_gpu_jobs: int = 0
server_event_loop: asyncio.AbstractEventLoop | None = None
gpu_job_lock = threading.RLock()
cancelled_projects: set[str] = set()

# WebSocket connections for progress updates
ws_connections: list[WebSocket] = []


@contextmanager
def gpu_job():
    """Serialize model lifecycle and GPU work across request threads."""
    global active_gpu_jobs, last_activity
    with gpu_job_lock:
        active_gpu_jobs += 1
        last_activity = time.time()
        try:
            yield
        finally:
            active_gpu_jobs -= 1
            last_activity = time.time()


def _workspace() -> Path:
    return Path(config.get("storage", {}).get("workspace_dir", "workspace")).resolve()


def _directory_size_bytes(root: Path) -> int:
    """Return current storage use without following directory symlinks."""
    if not root.exists():
        return 0
    total = 0
    for entry in os.scandir(root):
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                total += _directory_size_bytes(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                total += entry.stat(follow_symlinks=False).st_size
        except (FileNotFoundError, PermissionError):
            continue
    return total


def _enforce_workspace_quota() -> None:
    max_gb = float(config.get("storage", {}).get("max_workspace_gb", 0) or 0)
    if max_gb <= 0:
        return
    used_bytes = _directory_size_bytes(_workspace())
    limit_bytes = max_gb * 1024 ** 3
    if used_bytes >= limit_bytes:
        raise HTTPException(
            status_code=507,
            detail=(
                f"Voice workspace quota reached "
                f"({used_bytes / 1024 ** 3:.1f}/{max_gb:.1f} GiB). "
                "Remove old intermediates before generating more audio."
            ),
        )


def _safe_storage_project(root: Path, project_id: str) -> Path:
    root = root.resolve()
    candidate = (root / project_id).resolve()
    if not candidate.is_relative_to(root) or candidate == root:
        raise HTTPException(status_code=400, detail="Invalid project ID")
    return candidate


def _safe_workspace_project(project_id: str) -> Path:
    return _safe_storage_project(_workspace(), project_id)


def _safe_project_path(project_id: str, value: str | Path) -> Path:
    project_dir = _safe_workspace_project(project_id)
    candidate = Path(value)
    if not candidate.is_absolute():
        if candidate.parts and candidate.parts[0] == project_id:
            candidate = (_workspace() / candidate).resolve()
        else:
            candidate = (project_dir / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if not candidate.is_relative_to(project_dir):
        raise HTTPException(status_code=403, detail="Path is outside the project workspace")
    return candidate


async def _broadcast_progress(message: dict[str, Any]) -> None:
    for ws in list(ws_connections):
        try:
            await ws.send_json(message)
        except Exception:
            try:
                ws_connections.remove(ws)
            except ValueError:
                pass


def _progress_from_worker(message: dict[str, Any]) -> None:
    if server_event_loop and server_event_loop.is_running():
        asyncio.run_coroutine_threadsafe(
            _broadcast_progress(message),
            server_event_loop,
        )


def load_config(config_path: str = "voice/config.yaml") -> dict[str, Any]:
    """Load configuration from YAML file."""
    path = Path(config_path)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    logger.warning("Config not found: %s — using defaults", path)
    return {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler — load model on startup, unload on shutdown."""
    global engine, designer, library, validator, assembler, normalizer, exporter
    global config, start_time, last_activity, server_event_loop

    start_time = time.time()
    last_activity = start_time
    server_event_loop = asyncio.get_running_loop()
    config = load_config()

    from shared.single_instance import SingleInstanceLock
    lock = SingleInstanceLock("voice_server.lock")
    if not lock.acquire():
        logger.error("Another Voice Server instance is already running! Exiting.")
        import sys
        sys.exit(1)

    # Initialize components
    from voice.tts_server.embedding_store import EmbeddingStore
    embedding_store = EmbeddingStore(db_path="voice_cache.db")

    tts_cfg = config.get("tts", {})
    engine = Qwen3TTSEngine(
        model_name=tts_cfg.get("model", "Qwen/Qwen3-TTS-12Hz-1.7B-Base"),
        device=tts_cfg.get("device", "cuda"),
        dtype=tts_cfg.get("dtype", "float16"),
        sample_rate=tts_cfg.get("sample_rate", 24000),
        embedding_store=embedding_store,
        generation_config=tts_cfg.get("generation", {}),
        max_text_length=tts_cfg.get("max_text_length", 500),
        language=tts_cfg.get("language", "English"),
        attn_implementation=tts_cfg.get("attn_implementation", "sdpa"),
    )

    val_cfg = config.get("validation", {})
    whisper_val = WhisperValidator(
        model_name=val_cfg.get("whisper_model", "large-v3"),
        device=val_cfg.get("whisper_device", "auto"),
    )

    storage_cfg = config.get("storage", {})
    library = VoiceLibraryManager(
        library_dir=storage_cfg.get("voice_library_dir", "voice_library"),
    )
    designer = VoiceDesigner(
        engine=engine,
        library=library,
        validator=whisper_val,
        voice_design_duration=tts_cfg.get("voice_design_duration", 10),
        voice_design_model=tts_cfg.get(
            "voice_design_model",
            "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        ),
        voice_design_test_sentences=tts_cfg.get(
            "voice_design_test_sentences", {}
        ),
        wer_threshold=val_cfg.get("wer_threshold", 0.20),
        similarity_warning_threshold=val_cfg.get(
            "voice_profile_similarity_warning", 0.97
        ),
        acoustic_regeneration_attempts=val_cfg.get(
            "voice_profile_acoustic_regenerations", 1
        ),
    )
    audio_analyzer = AudioAnalyzer(
        noise_threshold=val_cfg.get("artifact_noise_threshold", -50),
        clipping_threshold=val_cfg.get("clipping_threshold", -0.5),
        max_silence_seconds=val_cfg.get("max_silence_seconds", 3.0),
        duration_tolerance=val_cfg.get("duration_tolerance", 0.3),
    )
    validator = ValidationLoop(
        whisper=whisper_val,
        analyzer=audio_analyzer,
        engine=engine,
        library=library,
        wer_threshold=val_cfg.get("wer_threshold", 0.20),
        max_retries=val_cfg.get("max_retries", 3),
        embedding_store=embedding_store,
        speaker_similarity_threshold=val_cfg.get(
            "speaker_similarity_threshold",
            0.55,
        ),
        keep_models_resident=val_cfg.get(
            "keep_tts_and_whisper_resident", False
        ),
    )

    master_cfg = config.get("mastering", {})
    assembler = AudioAssembler(
        crossfade_ms=master_cfg.get("crossfade_ms", 30),
        sample_rate=tts_cfg.get("sample_rate", 24000),
    )
    normalizer = LoudnessNormalizer(
        target_lufs=master_cfg.get("target_lufs", -19),
        peak_limit_dbfs=master_cfg.get("peak_limit_dbfs", -1.0),
        output_sample_rate=master_cfg.get("output_sample_rate", 44100),
        output_bit_depth=master_cfg.get("output_bit_depth", 16),
        noise_gate_enabled=master_cfg.get("noise_gate_enabled", False),
        noise_gate_threshold=master_cfg.get("noise_gate_threshold", -50),
        noise_gate_attack_ms=master_cfg.get("noise_gate_attack_ms", 5),
        noise_gate_release_ms=master_cfg.get("noise_gate_release_ms", 50),
    )
    exporter = M4BExporter()

    # Load the TTS model
    logger.info("Loading TTS model...")
    engine.load()

    async def vram_cleanup_loop():
        """Unload models after a configurable idle period."""
        idle_seconds = int(config.get("server", {}).get("idle_unload_seconds", 300))
        while True:
            await asyncio.sleep(min(30, max(5, idle_seconds)))
            try:
                if (
                    idle_seconds > 0
                    and active_gpu_jobs == 0
                    and time.time() - last_activity >= idle_seconds
                ):
                    with gpu_job_lock:
                        if active_gpu_jobs == 0 and engine and engine.is_loaded:
                            engine.unload()
                            logger.info("Unloaded TTS model after %ds idle", idle_seconds)
                        if validator and validator.whisper.is_loaded:
                            validator.whisper.unload()
            except Exception:
                logger.exception("Idle model cleanup failed")

    cleanup_task = asyncio.create_task(vram_cleanup_loop())

    yield

    # Shutdown
    cleanup_task.cancel()
    logger.info("Shutting down — unloading model...")
    if engine:
        engine.unload()
    lock.release()


# ---------------------------------------------------------------------------
# App creation
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Crazy Audiobook Creator — Voice Server",
    description="TTS generation, validation, and mastering API",
    version="0.1.0",
    lifespan=lifespan,
)

_import_config = load_config()
_cors_origins = _import_config.get("server", {}).get(
    "cors_origins",
    ["http://127.0.0.1:8000", "http://localhost:8000"],
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials="*" not in _cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def require_api_token(request: Request, call_next):
    token = config.get("server", {}).get("api_token", "") or _import_config.get(
        "server", {}
    ).get("api_token", "")
    if token and request.url.path != "/health":
        if request.headers.get("X-API-Token") != token:
            return JSONResponse({"detail": "Invalid API token"}, status_code=401)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health_check() -> VoiceHealthResponse:
    """Health check endpoint."""
    vram = engine.get_vram_info() if engine else {}
    return VoiceHealthResponse(
        status="ok",
        gpu=engine.get_gpu_name() if engine else "Unknown",
        vram_total_gb=vram.get("vram_total_gb", 0.0),
        vram_used_gb=vram.get("vram_used_gb", 0.0),
        model_loaded=engine.model_name if engine and engine.is_loaded else "none",
        attention_backend=engine.attn_implementation if engine else "",
        uptime_seconds=time.time() - start_time,
    )


@app.post("/voices/bootstrap")
def bootstrap_voices(request: BootstrapVoicesRequest) -> BootstrapVoicesResponse:
    """Generate voice reference clips for all characters."""
    if not designer or not engine:
        raise HTTPException(status_code=503, detail="Server not initialized")
    with gpu_job():
        # VoiceDesign and the Qwen Base clone model do not fit safely in VRAM
        # together.
        engine.unload()
        try:
            return designer.bootstrap_voices(request)
        finally:
            if designer.validator and designer.validator.is_loaded:
                designer.validator.unload()
            engine.load()


@app.post("/voices/regenerate")
def regenerate_voice(
    project_id: str,
    character_id: str,
    voice_description: str = "",
):
    """Force-regenerate a character's voice reference clip."""
    if not designer:
        raise HTTPException(status_code=503, detail="Server not initialized")

    from shared.models import Character
    character = Character(
        id=character_id,
        name=character_id.replace("_", " ").title(),
        gender="other",
        age_range="unknown",
        voice_description=voice_description,
    )
    with gpu_job():
        if engine:
            engine.unload()
        try:
            result = designer.regenerate_voice(project_id, character_id, character)
        finally:
            if designer.validator and designer.validator.is_loaded:
                designer.validator.unload()
            if engine:
                engine.load()
    return {"status": "success", "result": result.model_dump()}


@app.get("/voices/{project_id}")
def list_voices(project_id: str):
    """List all voices for a project."""
    if not library:
        raise HTTPException(status_code=503, detail="Server not initialized")
    return library.list_voices(project_id)


@app.post("/generate/line")
def generate_line(request: GenerateLineRequest) -> GenerateLineResponse:
    """Generate audio for a single script line."""
    if not engine or not library:
        raise HTTPException(status_code=503, detail="Server not initialized")

    t0 = time.time()
    output_path = (
        _safe_workspace_project(request.project_id)
        / "segments"
        / f"{request.line.line_id}.wav"
    )

    logger.info(
        "[VoiceServer] Synthesizing line %s (speaker='%s', text_len=%d, emotion='%s')",
        request.line.line_id,
        request.line.speaker,
        len(request.line.text),
        request.line.emotion or "normal",
    )

    voice_id = request.line.voice_id or request.line.speaker
    voice_ref = library.get_voice_path(request.project_id, voice_id)
    if not voice_ref.exists():
        logger.warning("[VoiceServer] Voice ref missing for '%s', falling back to narrator", request.line.speaker)
        voice_ref = library.get_voice_path(request.project_id, "narrator")

    ref_text = library.get_voice_ref_text(request.project_id, voice_id)
    if voice_ref == library.get_voice_path(request.project_id, "narrator"):
        ref_text = library.get_voice_ref_text(request.project_id, "narrator")

    with gpu_job():
        audio = engine.generate_speech(
            text=request.line.text,
            voice_reference_path=voice_ref,
            ref_text=ref_text or "",
            emotion_instruction=request.line.emotion,
            speed=request.line.speed,
            voice_fx=request.line.voice_fx,
            output_path=output_path,
        )

    duration = len(audio) / engine.sample_rate
    elapsed = time.time() - t0
    logger.info(
        "[VoiceServer] Line %s completed: audio_duration=%.2fs, gen_time=%.2fs → %s",
        request.line.line_id,
        duration,
        elapsed,
        output_path.name,
    )

    return GenerateLineResponse(
        status="success",
        line_id=request.line.line_id,
        audio_file=str(output_path),
        duration_seconds=duration,
        sample_rate=engine.sample_rate,
    )


@app.post("/generate/chapter")
def generate_chapter(request: GenerateChapterRequest) -> GenerateChapterResponse:
    """Generate audio for an entire chapter with validation."""
    if not validator:
        raise HTTPException(status_code=503, detail="Server not initialized")

    t0 = time.time()
    logger.info(
        "[VoiceServer] Starting chapter %d generation for '%s' (%d lines, validate=%s)",
        request.chapter_number,
        request.project_id,
        len(request.lines),
        request.validation_enabled,
    )

    workspace = _workspace()
    _safe_workspace_project(request.project_id)
    _enforce_workspace_quota()
    cancelled_projects.discard(request.project_id)
    try:
        with gpu_job():
            result = validator.process_chapter(
                project_id=request.project_id,
                chapter_number=request.chapter_number,
                lines=request.lines,
                workspace=workspace,
                validate=request.validation_enabled,
                auto_retry=request.auto_retry,
                max_retries=request.max_retries,
                progress_callback=_progress_from_worker,
                cancel_check=lambda: request.project_id in cancelled_projects,
                validation_terms=set(request.validation_terms),
            )
    except GenerationCancelled as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        cancelled_projects.discard(request.project_id)

    elapsed = time.time() - t0
    logger.info(
        "[VoiceServer] Chapter %d finished in %.2fs: %d/%d lines generated, %d failed",
        request.chapter_number,
        elapsed,
        result.generated,
        result.total_lines,
        result.failed_validation,
    )

    return result


@app.post("/validate")
def validate_segment(request: ValidateRequest) -> dict:
    """Validate a single audio segment."""
    if not validator:
        raise HTTPException(status_code=503, detail="Server not initialized")

    audio_path = Path(request.audio_file).resolve()
    allowed_roots = [
        _workspace(),
        Path(
            config.get("storage", {}).get("voice_library_dir", "voice_library")
        ).resolve(),
    ]
    if not any(audio_path.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="Audio path is outside allowed storage")
    if not audio_path.is_file():
        raise HTTPException(status_code=404, detail="Audio file not found")
    with gpu_job():
        result = validator.validate_single(
            audio_file=str(audio_path),
            expected_text=request.expected_text,
        )
    return result.model_dump()


@app.post("/master/chapter")
def master_chapter(request: MasterChapterRequest) -> MasterChapterResponse:
    """Master (assemble + normalize) a chapter's audio."""
    if not assembler or not normalizer or not engine or not library:
        raise HTTPException(status_code=503, detail="Server not initialized")

    workspace = _workspace()
    if not request.segments:
        raise HTTPException(status_code=422, detail="No segments supplied")
    for segment in request.segments:
        path = _safe_project_path(request.project_id, segment.file)
        if not path.is_file():
            raise HTTPException(
                status_code=422,
                detail=f"Missing segment: {segment.line_id}",
            )

    announcement_audio = None
    if request.announce_chapter:
        narrator_ref = library.get_voice_path(request.project_id, "narrator")
        if not narrator_ref.is_file():
            raise HTTPException(
                status_code=422,
                detail="Narrator voice is required for chapter announcements",
            )
        announcement_text = request.chapter_title.strip() or (
            f"Chapter {request.chapter_number}"
        )
        with gpu_job():
            announcement_audio = engine.generate_speech(
                text=announcement_text,
                voice_reference_path=narrator_ref,
                ref_text=library.get_voice_ref_text(
                    request.project_id, "narrator"
                )
                or "",
                emotion_instruction="clear chapter announcement",
                speed=1.0,
            )

    with gpu_job():
        assembled = assembler.assemble_chapter(
            segments=request.segments,
            workspace=workspace,
            announcement_audio=announcement_audio,
        )
        if len(assembled["audio"]) == 0:
            raise HTTPException(status_code=422, detail="Assembled chapter is empty")

        output_dir = _safe_workspace_project(request.project_id) / "chapters"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"chapter_{request.chapter_number:03d}.wav"
        mastering_result = normalizer.normalize(
            audio=assembled["audio"],
            sample_rate=assembled["sample_rate"],
            output_path=str(output_path),
        )

    return MasterChapterResponse(
        status="success",
        chapter_number=request.chapter_number,
        output_file=str(output_path),
        duration_seconds=mastering_result["duration_seconds"],
        lufs=mastering_result["lufs"],
        peak_dbfs=mastering_result["peak_dbfs"],
        file_size_mb=output_path.stat().st_size / (1024 * 1024),
    )


@app.post("/export/m4b")
def export_m4b(request: ExportM4BRequest) -> ExportM4BResponse:
    """Export all chapters as a single M4B audiobook."""
    if not exporter:
        raise HTTPException(status_code=503, detail="Server not initialized")

    workspace = _workspace()
    _safe_workspace_project(request.project_id)
    if not request.chapters:
        raise HTTPException(status_code=422, detail="No mastered chapters supplied")
    for chapter in request.chapters:
        path = _safe_project_path(request.project_id, chapter.file)
        if not path.is_file():
            raise HTTPException(
                status_code=422,
                detail=f"Missing mastered chapter {chapter.number}",
            )
    if request.cover_art:
        cover = Path(request.cover_art).resolve()
        project_roots = [
            _safe_workspace_project(request.project_id),
            _safe_storage_project(Path("brain/projects"), request.project_id),
        ]
        if not any(cover.is_relative_to(root) for root in project_roots):
            raise HTTPException(status_code=403, detail="Cover path is outside project storage")
    result = exporter.export(
        project_id=request.project_id,
        metadata=request.metadata,
        chapters=request.chapters,
        cover_art=request.cover_art,
        output_config=request.output_config,
        workspace=workspace,
        output_name=request.output_name,
    )

    return result


@app.get("/download/{project_id}/{path:path}")
def download_file(project_id: str, path: str):
    """Download a file from the workspace."""
    project_dir = _safe_workspace_project(project_id)
    
    file_path = (project_dir / path).resolve()
    if not file_path.is_relative_to(project_dir):
        raise HTTPException(status_code=403, detail="Access denied")

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type="application/octet-stream",
    )


# ---------------------------------------------------------------------------
# WebSocket for progress updates
# ---------------------------------------------------------------------------


@app.websocket("/ws/progress")
async def websocket_progress(websocket: WebSocket):
    """WebSocket endpoint for streaming progress updates."""
    token = config.get("server", {}).get("api_token", "")
    if token and websocket.query_params.get("token") != token:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    ws_connections.append(websocket)
    logger.info("WebSocket client connected")

    try:
        while True:
            # Keep connection alive, receive any client messages
            await websocket.receive_text()
    except WebSocketDisconnect:
        try:
            ws_connections.remove(websocket)
        except ValueError:
            pass
        logger.info("WebSocket client disconnected")


@app.post("/cancel/{project_id}")
async def cancel_project(project_id: str):
    """Request cooperative cancellation at the next segment boundary."""
    cancelled_projects.add(project_id)
    return {"status": "cancelling", "project_id": project_id}


@app.post("/unload")
async def unload_models():
    """Unload all TTS and Whisper models from GPU VRAM instantly."""
    global engine, validator
    unloaded = []
    with gpu_job_lock:
        if active_gpu_jobs:
            raise HTTPException(
                status_code=409,
                detail="Models are busy; cancel the project and wait for acknowledgement",
            )
        if engine:
            engine.unload()
            unloaded.append("qwen3_tts")
        if validator and validator.whisper.is_loaded:
            validator.whisper.unload()
            unloaded.append("whisper")
    logger.info("[VoiceServer] Unloaded models on request: %s", unloaded)
    return {"status": "unloaded", "models": unloaded}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main():
    """Run the Voice server."""
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Crazy Audiobook Creator — Voice Server")
    parser.add_argument("--config", default="voice/config.yaml", help="Config file path")
    parser.add_argument("--host", default=None, help="Override host")
    parser.add_argument("--port", type=int, default=None, help="Override port")
    args = parser.parse_args()

    cfg = load_config(args.config)
    server_cfg = cfg.get("server", {})

    host = args.host or server_cfg.get("host", "127.0.0.1")
    port = args.port or server_cfg.get("port", 8100)
    token = server_cfg.get("api_token", "")
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise RuntimeError(
            "Refusing to bind Voice Server beyond loopback without server.api_token"
        )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    logger.info("Starting Voice server on %s:%d", host, port)
    uvicorn.run(
        "voice.tts_server.main:app",
        host=host,
        port=port,
        workers=1,  # Must be 1 for GPU
        reload=False,
    )


if __name__ == "__main__":
    main()
