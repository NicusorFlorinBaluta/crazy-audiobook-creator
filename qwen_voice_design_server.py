"""Qwen3-TTS VoiceDesign microservice used during voice bootstrapping."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException
from huggingface_hub import snapshot_download
from pydantic import BaseModel
from qwen_tts import Qwen3TTSModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("qwen_voice_design_server")

app = FastAPI(title="Qwen3-TTS VoiceDesign Microservice")


class VoiceDesignRequest(BaseModel):
    prompt: str
    text: str
    output_path: str
    duration_seconds: float = 10.0


model: Qwen3TTSModel | None = None


@app.on_event("startup")
def load_model() -> None:
    global model
    model_name = os.environ.get(
        "QWEN_VOICE_DESIGN_MODEL",
        "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    )
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if device != "cpu" else torch.float32
    logger.info("Loading %s on %s...", model_name, device)
    try:
        model_path = snapshot_download(
            repo_id=model_name,
            local_files_only=True,
        )
        model = Qwen3TTSModel.from_pretrained(
            model_path,
            device_map=device,
            dtype=dtype,
            attn_implementation="eager",
        )
        logger.info("Qwen VoiceDesign loaded successfully")
    except Exception:
        logger.exception("Failed to load Qwen VoiceDesign")
        model = None


@app.post("/voices/design")
def design_voice(request: VoiceDesignRequest) -> dict[str, str]:
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    try:
        wavs, sample_rate = model.generate_voice_design(
            text=request.text,
            language="English",
            instruct=request.prompt,
        )
        output_path = Path(request.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(output_path), wavs[0], sample_rate)
        logger.info("Saved designed voice to %s", output_path)
        return {"status": "success", "file": str(output_path)}
    except Exception as exc:
        logger.exception("Voice design failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/health")
def health() -> dict[str, object]:
    return {"status": "ok", "model_loaded": model is not None}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8101)
