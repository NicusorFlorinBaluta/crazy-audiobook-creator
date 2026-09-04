"""Character profiles and voice references.

Split out of `main.py`. Shared helpers come from `..voice_support` and
`..runtime`; nothing here imports `main`.

`approve_voice_cast` is deliberately NOT here. It calls `start_pipeline`,
so moving it would make this module import `main` and recreate the cycle
the split exists to remove.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from brain.dashboard.api import runtime, voice_support
from shared.artifacts import atomic_write_json, fingerprint, hash_file
from shared.constants import VOICE_CAST_SCHEMA_VERSION
from shared.voice_casting import compile_effective_voice_prompt
from voice.tts_server.voice_library import VoiceLibraryManager

logger = logging.getLogger(__name__)

router = APIRouter()


class VoiceAssignmentRequest(BaseModel):
    voice_id: str = Field(min_length=1, max_length=128)


class VoiceRegenerationRequest(BaseModel):
    voice_description: str = Field(min_length=12, max_length=1000)


class CharacterProfileUpdate(BaseModel):
    """Human correction for voice-relevant character metadata."""

    gender: Literal["male", "female", "other"] | None = None
    age_range: str | None = Field(default=None, min_length=1, max_length=80)
    voice_description: str | None = Field(default=None, min_length=12, max_length=1000)
    speaking_style: str | None = Field(default=None, max_length=500)


async def get_characters(project_id: str):
    """Get the character registry for a project."""
    chars_path = runtime.project_dir(project_id) / "characters.json"
    if not chars_path.exists():
        raise HTTPException(status_code=404, detail="Characters not analyzed yet")
    return FileResponse(str(chars_path), media_type="application/json")


@router.get("/api/projects/{project_id}/voices")
async def get_project_voices(project_id: str):
    """List only speaking cast members and their assignable references."""
    _, registry = voice_support._load_character_registry(project_id)
    characters = registry["characters"]
    cast = voice_support._load_or_build_voice_cast(project_id, registry)
    speaking_ids = set(cast.get("speaking_characters", []))
    voice_dir = voice_support._voice_project_dir(project_id)
    voice_registry_path = voice_dir / "voices.json"
    try:
        registered = (
            json.loads(voice_registry_path.read_text(encoding="utf-8")).get("voices", {})
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
        assigned_raw = profile.get("assigned_characters", [])
        assigned_characters = sorted(
            (item.get("id") or item.get("character_id") if isinstance(item, dict) else str(item))
            for item in assigned_raw
            if (item.get("id") or item.get("character_id") if isinstance(item, dict) else str(item)) in speaking_ids
        )
        voices.append(
            {
                "voice_id": voice_id,
                "name": profile.get("name") or owner.get("name") or info.get("name") or voice_id,
                "gender": profile.get("gender") or owner.get("gender", "other"),
                "age_range": profile.get("age_range") or owner.get("age_range", "unknown"),
                "source_description": profile.get("source_description", ""),
                "description": profile.get("effective_prompt") or info.get("description") or "",
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
                    f"api/projects/{project_id}/voices/{voice_id}/download" if preview_path.is_file() else None
                ),
                "assigned_characters": assigned_characters,
                "required": bool(assigned_characters),
                "owner_character_id": profile.get("owner_character_id") or voice_id,
            }
        )

    running = False
    if runtime.job_queue:
        try:
            running = bool(runtime.job_queue.get_job(project_id).get("running"))
        except KeyError:
            pass
    state: dict[str, Any] = {}
    if runtime.job_queue:
        try:
            state = runtime.job_queue.get_job(project_id)
        except KeyError:
            pass
    speaking_characters = [
        {
            "character_id": character_id,
            "name": characters[character_id].get("name") or character_id,
            "gender": characters[character_id].get("gender", "other"),
            "age_range": characters[character_id].get("age_range", "unknown"),
            "voice_description": characters[character_id].get("voice_description", ""),
            "speaking_style": characters[character_id].get("speaking_style", ""),
            "voice_id": (characters[character_id].get("voice_id") or character_id),
        }
        for character_id in sorted(speaking_ids)
        if character_id in characters
    ]
    narrator_options = [voice for voice in voices if voice.get("owner_character_id") == "narrator"]
    narrator_selected = next(
        (voice["voice_id"] for voice in narrator_options if voice["assigned_characters"]),
        None,
    )
    return {
        "cast_schema": VOICE_CAST_SCHEMA_VERSION,
        "voices": voices,
        "quality": cast.get("quality", {}),
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
            "status": state.get("voice_review_status", "grandfathered"),
            "approved_at": state.get("voice_review_approved_at"),
            "required": (
                (
                    state.get("voice_review_policy", "grandfathered") == "required_once"
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


@router.get("/api/projects/{project_id}/voices/download-all")
async def download_all_project_voices(
    project_id: str,
    all_variants: bool = False,
):
    """Download selected cast voice references as one reusable ZIP bundle."""
    state = runtime.require_job(project_id)
    try:
        _, registry = voice_support._load_character_registry(project_id)
    except HTTPException:
        registry = {}
    cast = voice_support._load_or_build_voice_cast(project_id, registry if registry else None)
    speaking_ids = set(cast.get("speaking_characters", []))
    book_name = voice_support._download_name_component(
        str(state.get("title") or project_id),
        "Untitled book",
    )
    archive = io.BytesIO()
    manifest: list[dict[str, Any]] = []
    used_names: set[str] = set()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for voice_id, profile in sorted(cast.get("voices", {}).items()):
            assigned_raw = profile.get("assigned_characters", [])
            assigned_characters = [
                (item.get("id") or item.get("character_id") if isinstance(item, dict) else str(item))
                for item in assigned_raw
                if not speaking_ids
                or (item.get("id") or item.get("character_id") if isinstance(item, dict) else str(item)) in speaking_ids
                or (item == "narrator" or (isinstance(item, dict) and item.get("id") == "narrator"))
            ]
            if not all_variants and not assigned_characters:
                continue

            try:
                voice_path, info = voice_support._registered_voice_path(project_id, voice_id)
            except HTTPException as exc:
                if exc.status_code == 404:
                    continue
                raise
            character_label = voice_support._download_name_component(
                voice_support._voice_download_label(cast, voice_id, info),
                "Narrator" if voice_id.startswith("narrator") else "Character",
            )
            archive_name = f"{book_name} - {character_label} - voice-reference.wav"
            if archive_name.casefold() in used_names:
                safe_voice_id = voice_support._download_name_component(voice_id, "voice")
                archive_name = f"{book_name} - {character_label} - {safe_voice_id} - voice-reference.wav"
            used_names.add(archive_name.casefold())
            bundle.write(voice_path, arcname=archive_name)
            manifest.append(
                {
                    "voice_id": voice_id,
                    "character": character_label,
                    "filename": archive_name,
                    "source_type": info.get("source_type", "generated"),
                    "reference_text": info.get("ref_text", ""),
                    "assigned_characters": assigned_characters,
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


@router.get("/api/projects/{project_id}/voices/{voice_id}/preview")
async def preview_project_voice(project_id: str, voice_id: str):
    """Stream an existing voice-reference WAV for dashboard preview."""
    cast = voice_support._load_or_build_voice_cast(project_id)
    voice_dir = voice_support._voice_project_dir(project_id)
    voice_registry_path = voice_dir / "voices.json"
    try:
        registered = (
            json.loads(voice_registry_path.read_text(encoding="utf-8")).get("voices", {})
            if voice_registry_path.exists()
            else {}
        )
    except (OSError, json.JSONDecodeError):
        registered = {}

    if voice_id not in cast.get("voices", {}) and voice_id not in registered:
        raise HTTPException(status_code=404, detail="Voice not found")
    actual_file = registered.get(voice_id, {}).get("file", f"{voice_id}.wav")
    voice_path = (voice_support._voice_project_dir(project_id) / actual_file).resolve()
    if not voice_path.is_relative_to(voice_support._voice_project_dir(project_id)):
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


@router.get("/api/projects/{project_id}/voices/{voice_id}/download")
async def download_project_voice(project_id: str, voice_id: str):
    """Download a reusable reference WAV named for its book and character."""
    state = runtime.require_job(project_id)
    cast = voice_support._load_or_build_voice_cast(project_id)
    voice_path, info = voice_support._registered_voice_path(project_id, voice_id)
    book_name = voice_support._download_name_component(
        str(state.get("title") or project_id),
        "Untitled book",
    )
    character_name = voice_support._download_name_component(
        voice_support._voice_download_label(cast, voice_id, info),
        "Narrator" if voice_id.startswith("narrator") else "Character",
    )
    return FileResponse(
        voice_path,
        media_type="audio/wav",
        filename=f"{book_name} - {character_name} - voice-reference.wav",
        content_disposition_type="attachment",
    )


@router.patch("/api/projects/{project_id}/characters/{character_id}/voice")
async def assign_character_voice(
    project_id: str,
    character_id: str,
    request: VoiceAssignmentRequest,
):
    """Assign a character to an existing or pending reference-voice owner."""
    voice_support._ensure_voice_editable(project_id)
    chars_path, registry = voice_support._load_character_registry(project_id)
    characters = registry["characters"]
    if character_id not in characters:
        raise HTTPException(status_code=404, detail="Character not found")
    cast = voice_support._load_or_build_voice_cast(project_id, registry)
    if character_id not in set(cast.get("speaking_characters", [])):
        raise HTTPException(
            status_code=422,
            detail="Only characters with spoken script lines can receive voices",
        )

    assignable_ids = set(cast.get("voices", {}))
    if request.voice_id not in assignable_ids:
        raise HTTPException(status_code=422, detail="Selected voice is not assignable")

    previous_voice_id = characters[character_id].get("voice_id") or character_id
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
        assigned = [candidate for candidate in profile.get("assigned_characters", []) if candidate != character_id]
        if profile.get("voice_id") == request.voice_id:
            assigned.append(character_id)
        profile["assigned_characters"] = sorted(set(assigned))
    voice_support._mark_cast_distinctness_stale(cast, previous_voice_id, request.voice_id)
    voice_support._save_voice_cast(project_id, cast)
    affected = voice_support._chapters_for_speakers(project_id, {character_id})
    voice_support._mark_voice_chapters_stale(project_id, affected)
    logger.info(
        "Character voice reassigned: project=%s character=%s %s -> %s; affected_chapters=%s",
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


@router.patch("/api/projects/{project_id}/characters/{character_id}/profile")
async def update_character_profile(
    project_id: str,
    character_id: str,
    request: CharacterProfileUpdate,
):
    """Persist a human character correction and invalidate dependent audio."""
    voice_support._ensure_voice_editable(project_id)
    updates = request.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=422, detail="Provide at least one profile field")
    updates = {key: value.strip() if isinstance(value, str) else value for key, value in updates.items()}
    if any(value == "" for value in updates.values()):
        raise HTTPException(status_code=422, detail="Profile fields cannot be blank")

    chars_path, registry = voice_support._load_character_registry(project_id)
    character = registry["characters"].get(character_id)
    if not character:
        raise HTTPException(status_code=404, detail="Character not found")
    changed = {key: value for key, value in updates.items() if character.get(key) != value}
    if not changed:
        return {
            "status": "unchanged",
            "character_id": character_id,
            "affected_chapters": [],
        }

    character.update(changed)
    atomic_write_json(chars_path, registry)
    project_dir = runtime.project_dir(project_id)
    overrides_path = project_dir / "character_overrides.json"
    try:
        overrides = (
            json.loads(overrides_path.read_text(encoding="utf-8"))
            if overrides_path.is_file()
            else {"schema": 1, "characters": {}}
        )
    except (OSError, json.JSONDecodeError):
        overrides = {"schema": 1, "characters": {}}
    overrides.setdefault("characters", {}).setdefault(character_id, {}).update(changed)
    atomic_write_json(overrides_path, overrides)

    merged_path = project_dir / "book_script.json"
    if merged_path.is_file():
        try:
            merged = json.loads(merged_path.read_text(encoding="utf-8"))
            merged_character = merged.get("character_registry", {}).get("characters", {}).get(character_id)
            if isinstance(merged_character, dict):
                merged_character.update(changed)
                atomic_write_json(merged_path, merged)
        except (OSError, json.JSONDecodeError):
            logger.warning("Could not update merged character profile for %s", project_id)

    cast = voice_support._load_or_build_voice_cast(project_id, registry)
    profile_updated = False
    for profile in cast.get("voices", {}).values():
        if profile.get("owner_character_id", profile.get("voice_id")) != character_id:
            continue
        for key in ("gender", "age_range", "voice_description", "speaking_style"):
            if key not in changed:
                continue
            target_key = "source_description" if key == "voice_description" else key
            profile[target_key] = changed[key]
        profile["design_fingerprint"] = ""
        warnings = list(profile.get("warnings", []))
        warning = "Character profile changed; regenerate this voice preview."
        if warning not in warnings:
            warnings.append(warning)
        profile["warnings"] = warnings
        profile_updated = True
    if profile_updated:
        voice_support._mark_cast_distinctness_stale(
            cast,
            *[
                str(profile.get("voice_id") or "")
                for profile in cast.get("voices", {}).values()
                if profile.get("owner_character_id", profile.get("voice_id")) == character_id
            ],
        )
    voice_support._save_voice_cast(project_id, cast)

    affected = voice_support._chapters_for_speakers(project_id, {character_id})
    voice_support._mark_voice_chapters_stale(project_id, affected)
    if runtime.job_queue:
        runtime.job_queue.update_job(
            project_id,
            {
                "voice_review_status": "pending",
                "voice_review_approved": False,
                "voice_review_approved_at": None,
                "voice_review_approved_revision": None,
            },
        )
    return {
        "status": "updated",
        "character_id": character_id,
        "changed_fields": sorted(changed),
        "affected_chapters": affected,
        "requires_voice_regeneration": profile_updated,
    }


@router.post("/api/projects/{project_id}/voices/{voice_id}/regenerate")
async def regenerate_project_voice(
    project_id: str,
    voice_id: str,
    request: VoiceRegenerationRequest,
):
    """Redesign one reference voice and invalidate only its dependent chapters."""
    voice_support._ensure_voice_editable(project_id)
    if not runtime.pipeline:
        raise HTTPException(status_code=503, detail="Server not initialized")
    chars_path, registry_data = voice_support._load_character_registry(project_id)
    from shared.models import BootstrapVoicesRequest, CharacterRegistry, Gender

    registry = CharacterRegistry.model_validate(registry_data)
    cast = voice_support._load_or_build_voice_cast(project_id, registry_data)
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
    managed_before = getattr(runtime.pipeline, "_voice_server_proc", None)
    try:
        await asyncio.to_thread(runtime.pipeline._start_voice_server)
        response = await asyncio.to_thread(
            runtime.pipeline.voice_client.bootstrap_voices,
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
        if managed_before is None and getattr(runtime.pipeline, "_voice_server_proc", None) is not None:
            await asyncio.to_thread(runtime.pipeline._stop_voice_server)

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
    voice_support._mark_cast_distinctness_stale(cast, voice_id)
    voice_support._save_voice_cast(project_id, cast)
    dependent_speakers = set(profile.get("assigned_characters", []))
    affected = voice_support._chapters_for_speakers(project_id, dependent_speakers)
    voice_support._mark_voice_chapters_stale(project_id, affected)
    logger.info(
        "Reference voice regenerated: project=%s voice=%s affected_chapters=%s",
        project_id,
        voice_id,
        affected,
    )
    return {
        "status": "success",
        "voice_id": voice_id,
        "affected_chapters": affected,
        "preview_url": (f"api/projects/{project_id}/voices/{voice_id}/preview?v={int(time.time())}"),
        "result": response.voices_generated.get(voice_id),
    }


@router.post("/api/projects/{project_id}/voices/{voice_id}/upload")
async def upload_project_voice(
    project_id: str,
    voice_id: str,
    file: UploadFile = File(...),
    transcript: str = Form(...),
):
    """Replace a speaking voice with a validated user-supplied reference."""
    voice_support._ensure_voice_editable(project_id)
    transcript = re.sub(r"\s+", " ", transcript).strip()
    if len(transcript) < 3 or len(transcript) > 2000:
        raise HTTPException(
            status_code=422,
            detail="Provide the exact spoken transcript (3-2000 characters)",
        )

    chars_path, registry = voice_support._load_character_registry(project_id)
    cast = voice_support._load_or_build_voice_cast(project_id, registry)
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

    voice_dir = voice_support._voice_project_dir(project_id)
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
                "FFmpeg could not decode the uploaded audio: " + (result.stderr.strip()[-500:] or "unknown format")
            )
        audio_info = voice_support._inspect_pcm_voice(canonical_path)

        # Phase 3.3: Validate uploaded reference sample transcription
        # Run Whisper validation in a thread so the server stays responsive
        # during the 30-90 second model load + inference.
        import json
        import sys
        import tempfile

        val_script = """
import sys, json, os
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

                class DummyResult:
                    wer = val_res["wer"]
                    transcribed_text = val_res["transcribed_text"]
                    effective_text_error = val_res["wer"]

                mismatch_err = voice_support._uploaded_transcript_error(DummyResult())
                if mismatch_err:
                    raise ValueError(mismatch_err)
            except json.JSONDecodeError as exc:
                logger.exception("Could not decode validator output: %s", proc.stdout)
                raise ValueError("Whisper validator returned invalid format.") from exc
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
        voice_support._mark_cast_distinctness_stale(cast, voice_id)
        voice_support._save_voice_cast(project_id, cast)

        dependent_speakers = set(profile.get("assigned_characters", []))
        affected = voice_support._chapters_for_speakers(project_id, dependent_speakers)
        voice_support._mark_voice_chapters_stale(project_id, affected)
        return {
            "status": "success",
            "voice_id": voice_id,
            "source_type": "uploaded",
            "duration_seconds": audio_info["duration_seconds"],
            "affected_chapters": affected,
            "preview_url": (f"api/projects/{project_id}/voices/{voice_id}/preview?v={int(time.time())}"),
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
