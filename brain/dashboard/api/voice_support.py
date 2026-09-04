"""Voice-cast helpers shared by `main.py` and the voice-cast router.

These were module-level helpers in `main.py`. They are here rather than
in `runtime` because they are voice-cast domain logic, not process
infrastructure -- `runtime` is deliberately limited to the objects and
path resolution that every router needs.

`main.py` still uses four of them for `approve_voice_cast`, which stays
there because it calls `start_pipeline`.
"""

from __future__ import annotations

import array
import json
import logging
import math
import re
import unicodedata
import wave
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from brain.dashboard.api import runtime
from brain.orchestrator.delivery_manager import DeliveryManager
from shared import paths as shared_paths
from shared.artifacts import atomic_write_json
from shared.cache import cache_service
from shared.models import ScriptChapter
from shared.voice_casting import build_voice_cast, required_voice_character_ids

logger = logging.getLogger(__name__)


def _voice_project_dir(project_id: str) -> Path:
    config = shared_paths.voice_config()
    root = shared_paths.repo_path(str(config.get("storage", {}).get("voice_library_dir", "voice_library"))).resolve()
    candidate = (root / project_id).resolve()
    if not candidate.is_relative_to(root) or candidate == root:
        raise HTTPException(status_code=400, detail="Invalid project ID")
    return candidate


def _load_character_registry(project_id: str) -> tuple[Path, dict[str, Any]]:
    chars_path = runtime.project_dir(project_id) / "characters.json"
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
            json.loads(registry_path.read_text(encoding="utf-8")).get("voices", {}) if registry_path.exists() else {}
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
    cast_path = runtime.project_dir(project_id) / "voice_cast.json"
    if registry_data is None:
        _, registry_data = _load_character_registry(project_id)
    registry_characters = registry_data.get("characters", {})
    if cast_path.is_file():
        try:
            cast = json.loads(cast_path.read_text(encoding="utf-8"))
            has_narrator = any(
                profile.get("owner_character_id") == "narrator" for profile in cast.get("voices", {}).values()
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
    voice_config = shared_paths.voice_config()
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
            "test_sentences": tts_config.get("voice_design_test_sentences", {}),
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
    atomic_write_json(runtime.project_dir(project_id) / "voice_cast.json", payload)


def _mark_cast_distinctness_stale(
    cast: dict[str, Any],
    *voice_ids: str,
) -> None:
    """Invalidate pair evidence whenever a reference or assignment changes."""
    quality = cast.setdefault("quality", {})
    stale = set(quality.get("stale_voice_ids", []))
    stale.update(voice_id for voice_id in voice_ids if voice_id)
    quality["distinctness_status"] = "stale"
    quality["stale_voice_ids"] = sorted(stale)
    quality["cast_pair_diagnostics"] = [
        item
        for item in quality.get("cast_pair_diagnostics", [])
        if item.get("left_voice_id") not in stale and item.get("right_voice_id") not in stale
    ]
    quality["similar_pairs"] = sum(item.get("status") == "similar" for item in quality["cast_pair_diagnostics"])


def _cast_distinctness_review(
    cast: dict[str, Any],
    required_voice_ids: set[str],
) -> tuple[list[dict[str, Any]], bool]:
    quality = cast.get("quality", {})
    pairs = [
        diagnostic
        for diagnostic in quality.get("cast_pair_diagnostics", [])
        if diagnostic.get("status") == "similar"
        and not diagnostic.get("warning_suppressed", False)
        and diagnostic.get("left_voice_id") in required_voice_ids
        and diagnostic.get("right_voice_id") in required_voice_ids
    ]
    return pairs, quality.get("distinctness_status") == "stale"


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
    rms = math.sqrt(sum(float(sample) * float(sample) for sample in samples) / len(samples)) / 32768.0
    duration = frame_count / sample_rate
    clipped = sum(abs(sample) >= 32760 for sample in samples) / len(samples)
    if duration < 3.0 or duration > 30.0:
        raise ValueError("Reference audio must be between 3 and 30 seconds after conversion")
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


def _voice_review_approval_update(approved_at: str, cast_revision: str) -> dict:
    return {
        "voice_review_status": "approved",
        "voice_review_approved_at": approved_at,
        "voice_review_approved_revision": cast_revision,
        "voice_review_approved": True,
        "pause_reason": None,
    }


def _uploaded_transcript_error(result, threshold: float = 0.20) -> str | None:
    """Return a safe user-facing mismatch error, or None when ASR agrees."""
    import re

    effective_error = float(
        result.effective_text_error if getattr(result, "effective_text_error", None) is not None else result.wer
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
    if not runtime.job_queue:
        raise HTTPException(status_code=503, detail="Server not initialized")
    try:
        state = runtime.job_queue.get_job(project_id)
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
    affected: list[int] = []
    script_dir = runtime.project_dir(project_id) / "script"
    for path in sorted(script_dir.glob("chapter_*.json")):
        try:
            chapter = ScriptChapter.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not parse %s; a recast of these speakers will not reach this chapter: %s", path, exc)
            continue
        if any(line.speaker in speaker_ids for line in chapter.lines):
            affected.append(chapter.chapter_number)
    return sorted(set(affected))


def _mark_voice_chapters_stale(
    project_id: str,
    affected_chapters: list[int],
) -> None:
    if not runtime.job_queue or not affected_chapters:
        return
    state = runtime.job_queue.get_job(project_id)
    affected = set(affected_chapters)
    pending = set(state.get("voice_revision_pending_chapters", [])) | affected
    DeliveryManager(runtime.project_dir(project_id)).mark_stale_for_chapters(
        affected,
        "Voice assignment changed",
    )
    runtime.job_queue.update_job(
        project_id,
        {
            "generated_chapters": [number for number in state.get("generated_chapters", []) if number not in affected],
            "mastered_chapters": [number for number in state.get("mastered_chapters", []) if number not in affected],
            "voice_revision_pending_chapters": sorted(pending),
        },
    )


def _script_chapters(project_id: str) -> list[Any]:
    script_dir = runtime.project_dir(project_id) / "script"
    if not script_dir.is_dir():
        return []

    try:
        file_mtimes = {
            p.name: p.stat().st_mtime
            for p in script_dir.glob("chapter_*.json")
            if re.fullmatch(r"chapter_\d{3,}\.json", p.name)
        }
    except OSError:
        file_mtimes = {}

    cache_key = f"script_chapters:{project_id}"
    cached = cache_service.get(cache_key)
    if cached and isinstance(cached, dict) and cached.get("file_mtimes") == file_mtimes:
        return cached.get("chapters", [])

    chapters = []
    for path in sorted(script_dir.glob("chapter_*.json")):
        if not re.fullmatch(r"chapter_\d{3,}\.json", path.name):
            continue
        try:
            chapters.append(ScriptChapter.model_validate_json(path.read_text(encoding="utf-8")))
        except Exception:
            logger.warning("Ignoring invalid script while building cast: %s", path)

    cache_service.set(
        cache_key,
        {"file_mtimes": file_mtimes, "chapters": chapters},
        ttl_seconds=1800,
    )
    return chapters
