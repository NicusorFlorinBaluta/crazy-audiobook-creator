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
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from voice.tts_server.qwen3_engine import Qwen3TTSEngine
from voice.tts_server.voice_designer import VoiceDesigner
from voice.tts_server.voice_library import VoiceLibraryManager
from voice.validator.whisper_validator import WhisperValidator
from voice.validator.audio_analyzer import AudioAnalyzer
from voice.validator.validation_loop import ValidationLoop
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

# WebSocket connections for progress updates
ws_connections: list[WebSocket] = []


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
    global config, start_time

    start_time = time.time()
    config = load_config()

    # Initialize components
    tts_cfg = config.get("tts", {})
    engine = Qwen3TTSEngine(
        model_name=tts_cfg.get("model", "Qwen/Qwen3-TTS-1.7B"),
        device=tts_cfg.get("device", "cuda"),
        dtype=tts_cfg.get("dtype", "float16"),
        sample_rate=tts_cfg.get("sample_rate", 24000),
    )

    storage_cfg = config.get("storage", {})
    library = VoiceLibraryManager(
        library_dir=storage_cfg.get("voice_library_dir", "voice_library"),
    )
    designer = VoiceDesigner(engine=engine, library=library)

    val_cfg = config.get("validation", {})
    whisper_val = WhisperValidator(
        model_name=val_cfg.get("whisper_model", "medium"),
        device=val_cfg.get("whisper_device", "auto"),
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
        wer_threshold=val_cfg.get("wer_threshold", 0.05),
        max_retries=val_cfg.get("max_retries", 3),
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
    )
    exporter = M4BExporter()

    # Load the TTS model
    logger.info("Loading TTS model...")
    engine.load()

    yield

    # Shutdown
    logger.info("Shutting down — unloading model...")
    if engine:
        engine.unload()


# ---------------------------------------------------------------------------
# App creation
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Crazy Audiobook Creator — Voice Server",
    description="TTS generation, validation, and mastering API",
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
        uptime_seconds=time.time() - start_time,
    )


@app.post("/voices/bootstrap")
async def bootstrap_voices(request: BootstrapVoicesRequest) -> BootstrapVoicesResponse:
    """Generate voice reference clips for all characters."""
    if not designer:
        raise HTTPException(status_code=503, detail="Server not initialized")
    return designer.bootstrap_voices(request)


@app.post("/voices/regenerate")
async def regenerate_voice(
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
    result = designer.regenerate_voice(project_id, character_id, character)
    return {"status": "success", "result": result.model_dump()}


@app.get("/voices/{project_id}")
async def list_voices(project_id: str):
    """List all voices for a project."""
    if not library:
        raise HTTPException(status_code=503, detail="Server not initialized")
    return library.list_voices(project_id)


@app.post("/generate/line")
async def generate_line(request: GenerateLineRequest) -> GenerateLineResponse:
    """Generate audio for a single script line."""
    if not engine or not library:
        raise HTTPException(status_code=503, detail="Server not initialized")

    workspace = Path(config.get("storage", {}).get("workspace_dir", "workspace"))
    output_path = workspace / request.project_id / "segments" / f"{request.line.line_id}.wav"

    voice_ref = library.get_voice_path(request.project_id, request.line.speaker)
    if not voice_ref.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Voice reference not found for speaker: {request.line.speaker}",
        )

    audio = engine.generate_speech(
        text=request.line.text,
        voice_reference_path=voice_ref,
        emotion_instruction=request.line.emotion,
        speed=request.line.speed,
        output_path=output_path,
    )

    duration = len(audio) / engine.sample_rate

    return GenerateLineResponse(
        status="success",
        line_id=request.line.line_id,
        audio_file=str(output_path),
        duration_seconds=duration,
        sample_rate=engine.sample_rate,
    )


@app.post("/generate/chapter")
async def generate_chapter(request: GenerateChapterRequest) -> GenerateChapterResponse:
    """Generate audio for an entire chapter with validation."""
    if not validator:
        raise HTTPException(status_code=503, detail="Server not initialized")

    workspace = Path(config.get("storage", {}).get("workspace_dir", "workspace"))
    result = validator.process_chapter(
        project_id=request.project_id,
        chapter_number=request.chapter_number,
        lines=request.lines,
        workspace=workspace,
        validate=request.validate,
        auto_retry=request.auto_retry,
        max_retries=request.max_retries,
        ws_connections=ws_connections,
    )

    return result


@app.post("/validate")
async def validate_segment(request: ValidateRequest) -> dict:
    """Validate a single audio segment."""
    if not validator:
        raise HTTPException(status_code=503, detail="Server not initialized")

    result = validator.validate_single(
        audio_file=request.audio_file,
        expected_text=request.expected_text,
    )
    return result.model_dump()


@app.post("/master/chapter")
async def master_chapter(request: MasterChapterRequest) -> MasterChapterResponse:
    """Master (assemble + normalize) a chapter's audio."""
    if not assembler or not normalizer:
        raise HTTPException(status_code=503, detail="Server not initialized")

    workspace = Path(config.get("storage", {}).get("workspace_dir", "workspace"))

    # Assemble segments
    assembled = assembler.assemble_chapter(
        segments=request.segments,
        workspace=workspace,
    )

    # Normalize loudness
    output_dir = workspace / request.project_id / "chapters"
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
async def export_m4b(request: ExportM4BRequest) -> ExportM4BResponse:
    """Export all chapters as a single M4B audiobook."""
    if not exporter:
        raise HTTPException(status_code=503, detail="Server not initialized")

    workspace = Path(config.get("storage", {}).get("workspace_dir", "workspace"))
    result = exporter.export(
        project_id=request.project_id,
        metadata=request.metadata,
        chapters=request.chapters,
        cover_art=request.cover_art,
        output_config=request.output_config,
        workspace=workspace,
    )

    return result


@app.get("/download/{project_id}/{path:path}")
async def download_file(project_id: str, path: str):
    """Download a file from the workspace."""
    workspace = Path(config.get("storage", {}).get("workspace_dir", "workspace"))
    file_path = workspace / project_id / path

    if not file_path.exists():
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
    await websocket.accept()
    ws_connections.append(websocket)
    logger.info("WebSocket client connected")

    try:
        while True:
            # Keep connection alive, receive any client messages
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_connections.remove(websocket)
        logger.info("WebSocket client disconnected")


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

    host = args.host or server_cfg.get("host", "0.0.0.0")
    port = args.port or server_cfg.get("port", 8100)

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
