"""Pronunciation inventory, overrides and audio previews.

Split out of `main.py`. Shared runtime state and path helpers come from
`..runtime`; nothing here imports `main`, so the dependency runs one way only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from brain.dashboard.api import runtime
from brain.orchestrator.delivery_manager import DeliveryManager
from shared.artifacts import atomic_write_json
from shared.models import GenerateLineRequest, ScriptLine
from shared.pronunciation import (
    apply_pronunciations,
    build_pronunciation_inventory,
    normalize_phonetic_text,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class PronunciationRequest(BaseModel):
    term: str = Field(min_length=1, max_length=120)
    spoken_text: str = Field(default="", max_length=240)


class PronunciationBatchRequest(BaseModel):
    entries: dict[str, str] = Field(default_factory=dict)


class PronunciationPreviewRequest(BaseModel):
    term: str = Field(default="", max_length=120)
    spoken_text: str = Field(default="", max_length=240)
    voice_id: str | None = Field(default=None, max_length=120)
    in_sentence: bool = True
    context_sentence: str | None = Field(default=None, max_length=500)


@router.get("/api/projects/{project_id}/pronunciations")
async def get_pronunciations(project_id: str):
    """Return the book pronunciation inventory and custom mappings."""
    runtime.require_job(project_id)
    project_dir = runtime.project_dir(project_id)
    return build_pronunciation_inventory(project_dir, client=runtime.pronunciation_llm())


@router.post("/api/projects/{project_id}/pronunciations")
async def update_pronunciation(project_id: str, request: PronunciationRequest):
    """Save or delete a custom pronunciation mapping and mark affected chapters stale."""
    runtime.require_job(project_id)
    project_dir = runtime.project_dir(project_id)
    dict_path = project_dir / "pronunciation_dict.json"

    current_dict: dict[str, str] = {}
    if dict_path.is_file():
        try:
            current_dict = json.loads(dict_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current_dict = {}

    raw_term = request.term.strip()
    term = re.sub(r"^(?:pronunciation\s*:\s*)+", "", raw_term, flags=re.IGNORECASE).strip()
    raw_spoken = request.spoken_text.strip()
    spoken = re.sub(r"^(?:pronunciation\s*:\s*)+", "", raw_spoken, flags=re.IGNORECASE).strip()
    if not term:
        raise HTTPException(status_code=400, detail="Pronunciation term cannot be empty")
    if spoken:
        current_dict[term] = normalize_phonetic_text(spoken)
    else:
        current_dict.pop(term, None)

    atomic_write_json(dict_path, current_dict)

    affected_chapters: set[int] = set()
    term_pattern = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)
    for chapter_path in sorted((project_dir / "script").glob("chapter_*.json")):
        if chapter_path.name.endswith(".meta.json"):
            continue
        try:
            chapter = json.loads(chapter_path.read_text(encoding="utf-8"))
            ch_num = int(chapter.get("chapter_number") or 0)
            for line in chapter.get("lines", []):
                txt = line.get("text", "")
                if term_pattern.search(txt):
                    affected_chapters.add(ch_num)
                    break
        except Exception:
            pass

    if affected_chapters:
        DeliveryManager(project_dir).mark_stale_for_chapters(
            affected_chapters,
            f"Pronunciation updated for '{term}'",
        )

    return {
        "status": "success",
        "inventory": build_pronunciation_inventory(project_dir, client=runtime.pronunciation_llm()),
        "affected_chapters": sorted(affected_chapters),
    }


@router.post("/api/projects/{project_id}/pronunciations/batch")
async def batch_update_pronunciations(project_id: str, request: PronunciationBatchRequest):
    """Save multiple custom pronunciation mappings in a single batch."""
    runtime.require_job(project_id)
    project_dir = runtime.project_dir(project_id)
    dict_path = project_dir / "pronunciation_dict.json"

    current_dict: dict[str, str] = {}
    if dict_path.is_file():
        try:
            current_dict = json.loads(dict_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current_dict = {}

    affected_terms: set[str] = set()
    for raw_term, raw_spoken in request.entries.items():
        term = re.sub(r"^(?:pronunciation\s*:\s*)+", "", raw_term.strip(), flags=re.IGNORECASE).strip()
        spoken = re.sub(r"^(?:pronunciation\s*:\s*)+", "", raw_spoken.strip(), flags=re.IGNORECASE).strip()
        if not term:
            continue
        if spoken:
            current_dict[term] = normalize_phonetic_text(spoken)
            affected_terms.add(term)
        else:
            current_dict.pop(term, None)

    atomic_write_json(dict_path, current_dict)

    affected_chapters: set[int] = set()
    if affected_terms:
        term_pattern = re.compile(
            rf"(?<!\w)(?:{'|'.join(re.escape(t) for t in affected_terms)})(?!\w)",
            re.IGNORECASE,
        )
        for chapter_path in sorted((project_dir / "script").glob("chapter_*.json")):
            if chapter_path.name.endswith(".meta.json"):
                continue
            try:
                chapter = json.loads(chapter_path.read_text(encoding="utf-8"))
                ch_num = int(chapter.get("chapter_number") or 0)
                for line in chapter.get("lines", []):
                    txt = line.get("text", "")
                    if term_pattern.search(txt):
                        affected_chapters.add(ch_num)
                        break
            except Exception:
                pass

        if affected_chapters:
            DeliveryManager(project_dir).mark_stale_for_chapters(
                affected_chapters,
                f"Batch pronunciation updated for {len(affected_terms)} terms",
            )

    return {
        "status": "success",
        "inventory": build_pronunciation_inventory(project_dir, client=runtime.pronunciation_llm()),
        "affected_chapters": sorted(affected_chapters),
    }


@router.post("/api/projects/{project_id}/pronunciations/preview")
async def preview_pronunciation(project_id: str, request: PronunciationPreviewRequest):
    """Generate a high-quality native Qwen3-TTS audio preview for a pronunciation candidate."""
    runtime.require_job(project_id)
    project_dir = runtime.project_dir(project_id)
    workspace_dir = runtime.workspace_project_dir(project_id)
    raw_spoken = request.spoken_text.strip() or request.term.strip()
    spoken = re.sub(r"^(?:pronunciation\s*:\s*)+", "", raw_spoken, flags=re.IGNORECASE).strip()
    if not spoken:
        raise HTTPException(status_code=400, detail="Text to preview cannot be empty")

    clean_spoken = normalize_phonetic_text(spoken)
    if request.in_sentence:
        if request.context_sentence and request.term.strip():
            text_to_speak = apply_pronunciations(request.context_sentence, {request.term.strip(): clean_spoken})
        else:
            text_to_speak = f"The word is {clean_spoken}."
    else:
        text_to_speak = clean_spoken

    voice_id = request.voice_id
    if not voice_id:
        cast_path = project_dir / "voice_cast.json"
        if cast_path.is_file():
            try:
                cast_data = json.loads(cast_path.read_text(encoding="utf-8"))
                voices = cast_data.get("voices", {})
                voice_id = next(
                    (vid for vid in voices if "narrator" in vid.lower()),
                    next(iter(voices.keys()), None),
                )
            except Exception:
                voice_id = None
    voice_id = voice_id or "narrator"

    preview_hash = hashlib.sha256(f"{voice_id}_{text_to_speak}".encode()).hexdigest()[:16]
    previews_dir = workspace_dir / "previews"
    previews_dir.mkdir(parents=True, exist_ok=True)
    audio_path = previews_dir / f"pron_{preview_hash}.wav"

    # If already generated and cached, return immediately
    if audio_path.is_file() and audio_path.stat().st_size > 44:
        return {
            "status": "success",
            "audio_url": f"api/projects/{project_id}/pronunciations/preview/{preview_hash}/audio",
            "spoken_text": clean_spoken,
            "text_spoken": text_to_speak,
            "has_tts": True,
            "cached": True,
        }

    # Ensure TTS voice server is running and generate via native Qwen3-TTS
    has_tts = False
    if runtime.pipeline:
        try:
            # Check if voice server responds; start if needed
            is_healthy = False
            if getattr(runtime.pipeline, "voice_client", None):
                try:
                    await asyncio.to_thread(runtime.pipeline.voice_client.health_check_once, 0.8)
                    is_healthy = True
                except Exception:
                    is_healthy = False

            if not is_healthy:
                await asyncio.to_thread(runtime.pipeline._start_voice_server)

            line_req = GenerateLineRequest(
                project_id=project_id,
                line=ScriptLine(
                    line_id=f"preview_pron_{preview_hash}",
                    speaker=voice_id,
                    voice_id=voice_id,
                    text=text_to_speak,
                ),
            )
            await asyncio.to_thread(runtime.pipeline.voice_client.generate_line, line_req)
            seg_path = workspace_dir / "segments" / f"preview_pron_{preview_hash}.wav"
            if seg_path.is_file() and seg_path.stat().st_size > 44:
                shutil.copyfile(seg_path, audio_path)
                has_tts = True
        except Exception as exc:
            logger.warning("TTS native preview generation failed: %s", exc)
            has_tts = False

    if has_tts and audio_path.is_file():
        return {
            "status": "success",
            "audio_url": f"api/projects/{project_id}/pronunciations/preview/{preview_hash}/audio",
            "spoken_text": clean_spoken,
            "text_spoken": text_to_speak,
            "has_tts": True,
        }

    return {
        "status": "fallback_webspeech",
        "audio_url": None,
        "spoken_text": clean_spoken,
        "text_spoken": text_to_speak,
        "has_tts": False,
        "message": "TTS server offline. Playing preview via Web Speech.",
    }


@router.get("/api/projects/{project_id}/pronunciations/preview/{preview_id}/audio")
async def get_pronunciation_preview_audio(project_id: str, preview_id: str):
    """Serve the generated pronunciation preview audio."""
    runtime.require_job(project_id)
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", preview_id)
    audio_path = runtime.workspace_project_dir(project_id) / "previews" / f"pron_{safe_id}.wav"
    if not audio_path.is_file():
        raise HTTPException(status_code=404, detail="Preview audio not found")
    return FileResponse(
        path=audio_path,
        media_type="audio/wav",
        headers={
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'inline; filename="preview_{safe_id}.wav"',
        },
    )
