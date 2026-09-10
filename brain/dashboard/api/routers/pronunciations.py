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
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from brain.dashboard.api import runtime
from brain.orchestrator.delivery_manager import DeliveryManager
from brain.orchestrator.voice_client import VoiceClientError
from shared.artifacts import atomic_write_json
from shared.models import GenerateLineRequest, ScriptLine
from shared.pronunciation import (
    apply_pronunciations,
    build_pronunciation_inventory,
    extract_concise_sentence,
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


class PreviewModeRequest(BaseModel):
    enabled: bool = True
    resume_pipeline: bool = False
    voice_id: str | None = Field(default=None, max_length=120)


_active_preview_modes: dict[str, dict[str, Any]] = {}


def exit_preview_mode_sync(project_id: str) -> bool:
    """Exit preview mode synchronously for project_id."""
    prev = _active_preview_modes.pop(project_id, None)
    if prev:
        logger.info("Auto-exited pronunciation preview mode for %s", project_id)
        return True
    return False


runtime.register_preview_mode_exiter(exit_preview_mode_sync)


@router.get("/api/projects/{project_id}/pronunciations/preview-mode")
async def get_preview_mode_status(project_id: str):
    """Return whether preview mode is active for this project."""
    runtime.require_job(project_id)
    mode = _active_preview_modes.get(project_id, {})
    return {
        "preview_mode": mode.get("active", False),
        "paused_pipeline": mode.get("paused_by_us", False),
        "voice_id": mode.get("voice_id"),
        "warmup": mode.get("warmup", {}),
    }


@router.post("/api/projects/{project_id}/pronunciations/preview-mode")
async def toggle_preview_mode(project_id: str, request: PreviewModeRequest):
    """Enter or exit pronunciation preview mode.

    Entering preview mode:
    - Pauses active generation to release GPU lock contention
    - Boots TTS server if not running
    - Warms up Qwen3-TTS model and primes narrator voice prompt cache

    Exiting preview mode:
    - Restores normal state
    - Optionally resumes pipeline if it was paused by preview mode
    """
    runtime.require_job(project_id)
    project_dir = runtime.project_dir(project_id)
    job = runtime.job_queue.get_job(project_id)
    running = bool(job.get("running"))

    if request.enabled:
        paused_by_us = False
        if running:
            from shared.constants import PipelineStage

            runtime.job_queue.update_job(
                project_id,
                {
                    "status": PipelineStage.PAUSED.value,
                    "active_stage": job.get("active_stage") or job.get("status"),
                    "pause_reason": "paused for pronunciation preview mode",
                    "running": False,
                    "paused_by_preview_mode": True,
                },
            )
            if runtime.pipeline:
                runtime.pipeline.stop(project_id)
                try:
                    await asyncio.to_thread(runtime.pipeline.voice_client.cancel_project, project_id)
                except (VoiceClientError, RuntimeError, OSError) as exc:
                    logger.debug("Voice cancel project during preview mode entry: %s", exc)
            paused_by_us = True
            await asyncio.sleep(0.5)

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
                except (OSError, ValueError, UnicodeDecodeError):
                    voice_id = None
        voice_id = voice_id or "narrator"

        warmup_info = {}
        if runtime.pipeline:
            try:
                is_healthy = False
                if getattr(runtime.pipeline, "voice_client", None):
                    try:
                        await asyncio.to_thread(runtime.pipeline.voice_client.health_check_once, 0.8)
                        is_healthy = True
                    except (VoiceClientError, RuntimeError, OSError):
                        is_healthy = False
                if not is_healthy:
                    await asyncio.to_thread(runtime.pipeline._start_voice_server)

                res = await asyncio.to_thread(
                    runtime.pipeline.voice_client.warmup_voice,
                    project_id=project_id,
                    voice_id=voice_id,
                    timeout=120,
                )
                warmup_info = res.model_dump()
            except Exception as exc:  # noqa: BLE001 - spawns the voice server; surface is open
                logger.warning("Preview mode warmup error: %s", exc)
                warmup_info = {"error": str(exc)}

        _active_preview_modes[project_id] = {
            "active": True,
            "paused_by_us": paused_by_us or job.get("paused_by_preview_mode", False),
            "voice_id": voice_id,
            "warmup": warmup_info,
        }

        return {
            "status": "success",
            "preview_mode": True,
            "paused_pipeline": paused_by_us,
            "voice_id": voice_id,
            "warmup": warmup_info,
        }
    prev_state = _active_preview_modes.pop(project_id, {})
    was_paused_by_us = prev_state.get("paused_by_us") or job.get("paused_by_preview_mode", False)
    resumed = False

    if request.resume_pipeline and was_paused_by_us and runtime._pipeline_starter:
        runtime.job_queue.update_job(project_id, {"paused_by_preview_mode": False})
        try:
            await runtime.start_pipeline(project_id)
            resumed = True
        except Exception as exc:  # noqa: BLE001 - starts the whole pipeline; surface is open
            logger.warning("Could not auto-resume pipeline after preview mode: %s", exc)

    return {
        "status": "success",
        "preview_mode": False,
        "can_resume": bool(was_paused_by_us),
        "pipeline_resumed": resumed,
    }


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
    for existing_key in list(current_dict.keys()):
        if existing_key.casefold() == term.casefold():
            current_dict.pop(existing_key, None)
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
        except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
            logger.warning(
                "Could not read %s; its chapter will be missing from the affected-chapter list: %s", chapter_path, exc
            )

    if affected_chapters:
        DeliveryManager(project_dir).mark_stale_for_chapters(
            affected_chapters,
            f"Pronunciation updated for '{term}'",
        )

    # Trigger background preview generation for the updated phonetic text
    if spoken:
        sample_ctx = None
        inv_path = project_dir / "pronunciation_inventory.json"
        if inv_path.is_file():
            try:
                inv_data = json.loads(inv_path.read_text(encoding="utf-8"))
                for c in inv_data.get("candidates", []):
                    if c.get("term", "").casefold() == term.casefold():
                        sample_ctx = (c.get("contexts") or [None])[0]
                        break
            except (OSError, ValueError, TypeError) as exc:
                logger.debug("Could not read sample context for %r: %s", term, exc)
        asyncio.create_task(
            generate_preview_audio(
                project_id=project_id,
                term=term,
                spoken_text=spoken,
                context_sentence=sample_ctx,
                in_sentence=True,
            )
        )

    return {
        "status": "success",
        "inventory": build_pronunciation_inventory(project_dir, client=runtime.pronunciation_llm(), force=True),
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
        for existing_key in list(current_dict.keys()):
            if existing_key.casefold() == term.casefold():
                current_dict.pop(existing_key, None)
        if spoken:
            current_dict[term] = normalize_phonetic_text(spoken)
            affected_terms.add(term)
        else:
            affected_terms.add(term)

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
            except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
                logger.warning(
                    "Could not read %s; its chapter will be missing from the affected-chapter list: %s",
                    chapter_path,
                    exc,
                )

        if affected_chapters:
            DeliveryManager(project_dir).mark_stale_for_chapters(
                affected_chapters,
                f"Batch pronunciation updated for {len(affected_terms)} terms",
            )

    # In background, trigger preview generation for up to 20 updated items
    for t in list(affected_terms)[:20]:
        asyncio.create_task(
            generate_preview_audio(
                project_id=project_id,
                term=t,
                spoken_text=current_dict.get(t, ""),
                in_sentence=True,
            )
        )

    return {
        "status": "success",
        "inventory": build_pronunciation_inventory(project_dir, client=runtime.pronunciation_llm(), force=True),
        "affected_chapters": sorted(affected_chapters),
    }


@router.get("/api/projects/{project_id}/pronunciations/export")
async def export_pronunciations(project_id: str, scope: str = "all"):
    """Export lexicon mappings in JSON format with filter support.

    Scopes:
    - 'all': Verified mappings plus active default recommendations
    - 'verified' / 'custom': Only user-defined / project-verified mappings
    - 'defaults': Only default recommendations
    """
    runtime.require_job(project_id)
    project_dir = runtime.project_dir(project_id)
    dict_path = project_dir / "pronunciation_dict.json"
    recs_path = project_dir / "pronunciation_recommendations.json"

    # Build canonical casing map from inventory if available
    canonical_map: dict[str, str] = {}
    try:
        inventory = build_pronunciation_inventory(project_dir, use_llm=False)
        for c in inventory.get("candidates", []):
            t = (c.get("term") or "").strip()
            if t:
                canonical_map[t.casefold()] = t
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.debug("Could not build inventory for canonical casing in export: %s", exc)

    project_dict: dict[str, str] = {}
    if dict_path.is_file():
        try:
            project_dict = json.loads(dict_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            project_dict = {}

    defaults_dict: dict[str, str] = {}
    if recs_path.is_file():
        try:
            recs_raw = json.loads(recs_path.read_text(encoding="utf-8"))
            for k, v in recs_raw.items():
                if isinstance(v, dict) and v.get("default"):
                    defaults_dict[k] = v["default"]
                elif isinstance(v, str) and v.strip():
                    defaults_dict[k] = v.strip()
        except (OSError, ValueError, UnicodeDecodeError):
            defaults_dict = {}

    # Index by casefold to guarantee case-insensitive reconciliation and canonical display casing
    defaults_folded: dict[str, tuple[str, str]] = {}
    for k, v in defaults_dict.items():
        k_clean = k.strip()
        v_clean = v.strip()
        if not k_clean or not v_clean:
            continue
        display_term = canonical_map.get(k_clean.casefold(), k_clean)
        defaults_folded[k_clean.casefold()] = (display_term, v_clean)

    project_folded: dict[str, tuple[str, str]] = {}
    for k, v in project_dict.items():
        k_clean = k.strip()
        v_clean = v.strip()
        if not k_clean or not v_clean:
            continue
        display_term = canonical_map.get(k_clean.casefold(), k_clean)
        project_folded[k_clean.casefold()] = (display_term, v_clean)

    scope_lower = (scope or "all").lower()
    if scope_lower in ("verified", "custom"):
        export_entries = dict(project_folded.values())
    elif scope_lower == "defaults":
        export_entries = dict(defaults_folded.values())
    else:  # 'all'
        # Base on defaults, strictly overwritten by project-specific overrides case-insensitively
        merged_folded = dict(defaults_folded)
        merged_folded.update(project_folded)
        export_entries = dict(merged_folded.values())

    payload = {
        "format": "crazy-audiobook-lexicon-v1",
        "project_id": project_id,
        "exported_at": datetime.now(UTC).isoformat(),
        "scope": scope_lower,
        "count": len(export_entries),
        "lexicon": export_entries,
    }
    return JSONResponse(
        content=payload,
        headers={
            "Content-Disposition": f'attachment; filename="{project_id}_lexicon_{scope_lower}.json"'
        },
    )


async def generate_preview_audio(
    project_id: str,
    term: str,
    spoken_text: str,
    context_sentence: str | None = None,
    in_sentence: bool = True,
    voice_id: str | None = None,
) -> dict[str, Any]:
    """Generate or retrieve a cached Qwen3-TTS audio preview for a pronunciation term."""
    project_dir = runtime.project_dir(project_id)
    workspace_dir = runtime.workspace_project_dir(project_id)
    raw_spoken = spoken_text.strip() or term.strip()
    spoken = re.sub(r"^(?:pronunciation\s*:\s*)+", "", raw_spoken, flags=re.IGNORECASE).strip()
    if not spoken:
        return {"status": "error", "message": "Text to preview cannot be empty", "has_tts": False}

    clean_spoken = normalize_phonetic_text(spoken)
    if in_sentence:
        if context_sentence and term.strip():
            concise_context = extract_concise_sentence(context_sentence, term.strip(), max_chars=100)
            text_to_speak = apply_pronunciations(concise_context, {term.strip(): clean_spoken})
        else:
            text_to_speak = f"The word is {clean_spoken}."
    else:
        text_to_speak = clean_spoken

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
            except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError):
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
            "preview_hash": preview_hash,
            "audio_url": f"api/projects/{project_id}/pronunciations/preview/{preview_hash}/audio",
            "spoken_text": clean_spoken,
            "text_spoken": text_to_speak,
            "has_tts": True,
            "cached": True,
        }

    # Ensure TTS voice server is running and generate via native Qwen3-TTS
    has_tts = False
    tts_error = ""
    if runtime.pipeline:
        try:
            is_healthy = False
            if getattr(runtime.pipeline, "voice_client", None):
                try:
                    await asyncio.to_thread(runtime.pipeline.voice_client.health_check_once, 0.8)
                    is_healthy = True
                except (VoiceClientError, RuntimeError, OSError):
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
            await asyncio.to_thread(
                runtime.pipeline.voice_client.generate_line,
                line_req,
                timeout=60,
            )
            seg_path = workspace_dir / "segments" / f"preview_pron_{preview_hash}.wav"
            if seg_path.is_file() and seg_path.stat().st_size > 44:
                shutil.copyfile(seg_path, audio_path)
                has_tts = True
        except (VoiceClientError, RuntimeError, OSError, shutil.Error) as exc:
            logger.warning("TTS native preview generation failed: %s", exc)
            tts_error = str(exc)
            has_tts = False

    if has_tts and audio_path.is_file():
        return {
            "status": "success",
            "preview_hash": preview_hash,
            "audio_url": f"api/projects/{project_id}/pronunciations/preview/{preview_hash}/audio",
            "spoken_text": clean_spoken,
            "text_spoken": text_to_speak,
            "has_tts": True,
            "cached": False,
        }

    return {
        "status": "fallback_webspeech",
        "audio_url": None,
        "preview_hash": preview_hash,
        "spoken_text": clean_spoken,
        "text_spoken": text_to_speak,
        "has_tts": False,
        "message": (
            f"TTS generation error: {tts_error}. Playing preview via Web Speech fallback."
            if tts_error
            else "TTS server offline. Playing preview via Web Speech."
        ),
    }


@router.post("/api/projects/{project_id}/pronunciations/preview")
async def preview_pronunciation(project_id: str, request: PronunciationPreviewRequest):
    """Generate a high-quality native Qwen3-TTS audio preview for a pronunciation candidate."""
    runtime.require_job(project_id)
    return await generate_preview_audio(
        project_id=project_id,
        term=request.term,
        spoken_text=request.spoken_text,
        context_sentence=request.context_sentence,
        in_sentence=request.in_sentence,
        voice_id=request.voice_id,
    )


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

