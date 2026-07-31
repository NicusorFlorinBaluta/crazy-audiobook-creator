from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Iterable
import numpy as np
import soundfile as sf
import scipy.signal as signal

import hashlib
import re
from typing import Any, Iterable

from shared.artifacts import fingerprint
from shared.constants import Gender, VOICE_CAST_SCHEMA_VERSION
from shared.models import CharacterRegistry


_CONTRAST_CLAUSES = (
    "Dry, lightly textured resonance with crisp consonants and restrained energy.",
    "Rounded resonance, soft consonant edges, and an unhurried conversational rhythm.",
    "Bright forward resonance, precise articulation, and alert controlled energy.",
    "Breathy texture, intimate projection, and deliberate pauses between phrases.",
    "Firm chest resonance, compact phrasing, and clean decisive articulation.",
    "Light nasal resonance, quick phrasing, and a curious animated cadence.",
    "Smooth dark resonance, relaxed articulation, and a reflective measured cadence.",
    "Clear open resonance, energetic phrasing, and a gently rising melodic cadence.",
)

_AUDIBLE_TERMS = {
    "alto",
    "baritone",
    "bass",
    "breathy",
    "bright",
    "cadence",
    "clear",
    "contralto",
    "crisp",
    "deep",
    "gravelly",
    "high",
    "low",
    "measured",
    "nasal",
    "pace",
    "pitch",
    "raspy",
    "resonance",
    "rough",
    "slow",
    "soft",
    "soprano",
    "tenor",
    "texture",
    "warm",
}

_SOURCE_SIMILARITY_THRESHOLD = 0.50
_PROMPT_SIMILARITY_THRESHOLD = 0.68


def speaking_character_ids(script_chapters: Iterable[Any]) -> set[str]:
    """Return only registry IDs that own at least one completed script line."""
    return {
        str(line.speaker)
        for chapter in script_chapters
        for line in chapter.lines
        if str(line.speaker).strip()
    }


def compile_effective_voice_prompt(
    *,
    gender: Gender | str,
    age_range: str,
    source_description: str,
    speaking_style: str = "",
) -> tuple[str, list[str]]:
    """Compile an explicit, audible Qwen VoiceDesign instruction.

    The LLM description is treated as input, not authority.  Contradictory
    gendered register words are repaired before an explicit gender/age prefix
    is added.
    """
    gender_value = gender.value if isinstance(gender, Gender) else str(gender)
    gender_value = gender_value.lower().strip()
    age = re.sub(r"\s+", " ", str(age_range or "adult")).strip()
    description = re.sub(
        r"\s+",
        " ",
        str(source_description or "").strip(" ."),
    )
    warnings: list[str] = []

    if gender_value == Gender.FEMALE.value:
        replacements = (
            (r"\bdeep baritone\b", "low contralto"),
            (r"\bbaritone\b", "contralto"),
            (r"\bdeep bass\b", "very low contralto"),
            (r"\bbass\b", "low contralto"),
            (r"\bmedium tenor\b", "medium alto"),
            (r"\btenor\b", "alto"),
        )
        repaired = description
        for pattern, replacement in replacements:
            repaired = re.sub(pattern, replacement, repaired, flags=re.IGNORECASE)
        if repaired != description:
            warnings.append(
                "The analyzed description used a male-coded register; it was "
                "rewritten to match the female character metadata."
            )
        description = repaired
        identity = f"A clearly female {age} speaker"
    elif gender_value == Gender.MALE.value:
        replacements = (
            (r"\bdeep contralto\b", "low baritone"),
            (r"\bcontralto\b", "baritone"),
            (r"\bsoprano\b", "high tenor"),
            (r"\bmedium alto\b", "medium tenor"),
            (r"\balto\b", "tenor"),
        )
        repaired = description
        for pattern, replacement in replacements:
            repaired = re.sub(pattern, replacement, repaired, flags=re.IGNORECASE)
        if repaired != description:
            warnings.append(
                "The analyzed description used a female-coded register; it was "
                "rewritten to match the male character metadata."
            )
        description = repaired
        identity = f"A clearly male {age} speaker"
    else:
        identity = f"An androgynous or non-gendered {age} speaker"

    if not description:
        description = "clear mid-range pitch, natural resonance, and measured pacing"
        warnings.append(
            "The analyzed voice description was empty; a neutral audible profile "
            "was supplied."
        )

    description_words = set(re.findall(r"[a-z]+", description.lower()))
    if not (description_words & _AUDIBLE_TERMS):
        warnings.append(
            "The source description contained few audible properties; explicit "
            "clarity, resonance, and pacing guidance was added."
        )
        description = (
            f"{description}; clear articulation, natural resonance, and measured pacing"
        )

    style = re.sub(r"\s+", " ", str(speaking_style or "").strip(" ."))
    prompt = f"{identity}. {description}."
    if style:
        prompt += f" Speaking style: {style}."
    prompt += (
        " Maintain this vocal identity consistently and prioritize intelligible "
        "natural audiobook speech."
    )
    return prompt, warnings


def _normalized_signature(value: str) -> str:
    return " ".join(re.findall(r"[a-z]+", value.lower()))


_PROMPT_BOILERPLATE = {
    "a", "an", "clearly", "speaker", "speaking", "style", "maintain", "this",
    "vocal", "identity", "consistently", "and", "prioritize", "intelligible",
    "natural", "audiobook", "speech", "distinguishing", "direction", "adult",
    "speaker", "with", "a", "hint", "of", "emotional", "baseline"
}


def _token_similarity(left: str, right: str) -> float:
    left_tokens = set(_normalized_signature(left).split()) - _PROMPT_BOILERPLATE
    right_tokens = set(_normalized_signature(right).split()) - _PROMPT_BOILERPLATE
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


_AUDIO_SIMILARITY_THRESHOLD = 0.88


def extract_acoustic_embedding(audio_path: str | Path) -> np.ndarray | None:
    """Extract a normalized acoustic spectral feature vector from audio."""
    p = Path(audio_path)
    if not p.exists() or p.stat().st_size < 100:
        return None
    try:
        data, sample_rate = sf.read(str(p))
        if data.size == 0:
            return None
        if data.ndim > 1:
            data = data.mean(axis=1)
        if sample_rate != 16000:
            num_samples = int(len(data) * 16000 / sample_rate)
            data = signal.resample(data, num_samples)
            sample_rate = 16000
        f, t, Sxx = signal.spectrogram(data, fs=sample_rate, nperseg=512, noverlap=256)
        log_spec = np.log(Sxx + 1e-6)
        mean_feat = log_spec.mean(axis=1)
        std_feat = log_spec.std(axis=1)
        embedding = np.concatenate([mean_feat, std_feat])
        norm = np.linalg.norm(embedding)
        return embedding / norm if norm > 0 else embedding
    except Exception:
        return None


def compute_audio_similarity(path_a: str | Path, path_b: str | Path) -> float | None:
    """Compute acoustic cosine similarity between two voice audio files (0.0 to 1.0)."""
    emb_a = extract_acoustic_embedding(path_a)
    emb_b = extract_acoustic_embedding(path_b)
    if emb_a is None or emb_b is None:
        return None
    return max(0.0, min(1.0, float(np.dot(emb_a, emb_b))))


def _contrast_for(voice_id: str, used: set[str]) -> str:
    start = int(hashlib.sha256(voice_id.encode("utf-8")).hexdigest()[:8], 16)
    for offset in range(len(_CONTRAST_CLAUSES)):
        clause = _CONTRAST_CLAUSES[(start + offset) % len(_CONTRAST_CLAUSES)]
        if clause not in used:
            used.add(clause)
            return clause
    return _CONTRAST_CLAUSES[start % len(_CONTRAST_CLAUSES)]


def build_voice_cast(
    *,
    project_id: str,
    registry: CharacterRegistry,
    speaking_ids: set[str],
    design_model: str,
    design_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a speaking-only cast and deterministic effective prompts."""
    missing = speaking_ids - set(registry.characters)
    if missing:
        raise ValueError(
            "Scripts reference speakers absent from character registry: "
            f"{sorted(missing)}"
        )

    owner_to_speakers: dict[str, list[str]] = {}
    for speaker_id in sorted(speaking_ids):
        character = registry.characters[speaker_id]
        owner_id = character.voice_id or speaker_id
        
        # Ensure owner_id points to a valid character in the registry
        if owner_id not in registry.characters:
            # Check if speaker_id matches any registered character's explicit aliases
            matched_owner = next(
                (
                    cid
                    for cid, c in registry.characters.items()
                    if speaker_id in getattr(c, "aliases", [])
                    or speaker_id.lower() in [a.lower() for a in getattr(c, "aliases", [])]
                ),
                speaker_id,
            )
            owner_id = matched_owner

        owner_to_speakers.setdefault(owner_id, []).append(speaker_id)

    voices: dict[str, dict[str, Any]] = {}
    previous_prompts: list[tuple[str, str]] = []
    used_contrasts: set[str] = set()
    for voice_id in sorted(owner_to_speakers):
        owner = registry.characters[voice_id]
        prompt, warnings = compile_effective_voice_prompt(
            gender=owner.gender,
            age_range=owner.age_range,
            source_description=owner.voice_description,
            speaking_style=owner.speaking_style,
        )
        source_signature = _normalized_signature(owner.voice_description)
        similar_to = [
            previous_id
            for previous_id, previous_prompt in previous_prompts
            if (
                source_signature
                and source_signature == _normalized_signature(
                    registry.characters[previous_id].voice_description
                )
            )
            or (
                _token_similarity(
                    owner.voice_description,
                    registry.characters[previous_id].voice_description,
                )
                >= _SOURCE_SIMILARITY_THRESHOLD
            )
            or (
                _token_similarity(prompt, previous_prompt)
                >= _PROMPT_SIMILARITY_THRESHOLD
            )
        ]
        if similar_to:
            contrast = _contrast_for(voice_id, used_contrasts)
            prompt += f" Distinguishing direction: {contrast}"
            warnings.append(
                "The initial profile was too similar to "
                f"{', '.join(similar_to)}; deterministic contrast was added."
            )

        assigned = owner_to_speakers[voice_id]
        profile_payload = {
            "schema": VOICE_CAST_SCHEMA_VERSION,
            "voice_id": voice_id,
            "owner_character_id": voice_id,
            "name": owner.name,
            "gender": owner.gender.value,
            "age_range": owner.age_range,
            "source_description": owner.voice_description,
            "effective_prompt": prompt,
            "warnings": warnings,
            "assigned_characters": assigned,
            "design_model": design_model,
            "design_config": design_config or {},
        }
        profile_payload["design_fingerprint"] = fingerprint(
            {
                "schema": VOICE_CAST_SCHEMA_VERSION,
                "voice_id": voice_id,
                "gender": owner.gender.value,
                "age_range": owner.age_range,
                "effective_prompt": prompt,
                "design_model": design_model,
                "design_config": design_config or {},
            }
        )
        voices[voice_id] = profile_payload
        previous_prompts.append((voice_id, prompt))

    cast_payload: dict[str, Any] = {
        "schema": VOICE_CAST_SCHEMA_VERSION,
        "project_id": project_id,
        "speaking_characters": sorted(speaking_ids),
        "non_speaking_characters": sorted(
            set(registry.characters) - speaking_ids
        ),
        "voices": voices,
    }
    cast_payload["fingerprint"] = fingerprint(cast_payload)
    return cast_payload
