from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import scipy.signal as signal
import soundfile as sf

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
    "Low-set smoky resonance, sparse melody, and slow deliberate sentence endings.",
    "Lean reedy resonance, exact consonants, and a brisk formal cadence.",
    "Velvety mid-range resonance, flowing phrasing, and calm downward inflection.",
    "Husky close-mic texture, restrained volume, and thoughtful broken phrasing.",
    "Bell-like upper resonance, buoyant projection, and lively rhythmic phrasing.",
    "Broad grounded resonance, relaxed tempo, and gently rounded vowels.",
    "Taut focused resonance, clipped phrasing, and a level unsentimental cadence.",
    "Airy open texture, light projection, and a smooth lyrical cadence.",
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

    # Text entered from an older dashboard can be a previously compiled
    # prompt rather than the original short design description.  Strip our
    # generated wrapper before compiling again; otherwise every reset repeats
    # identity, speaking style, consistency boilerplate, and palette clauses.
    compiled_markers = (
        "Maintain this vocal identity consistently",
        "Distinguishing direction:",
    )
    if any(marker.lower() in description.lower() for marker in compiled_markers):
        description = re.sub(
            r"^(?:A clearly (?:female|male) .*? speaker|"
            r"An androgynous or non-gendered .*? speaker)\.\s*",
            "",
            description,
            flags=re.IGNORECASE,
        )
        description = re.sub(
            r"\s*Speaking style:\s*[^.]+\.",
            "",
            description,
            flags=re.IGNORECASE,
        )
        description = re.sub(
            r"\s*Maintain this vocal identity consistently and prioritize "
            r"intelligible natural audiobook speech\.",
            "",
            description,
            flags=re.IGNORECASE,
        )
        description = re.sub(
            r"\s*Distinguishing direction:\s*[^.]+\.?",
            "",
            description,
            flags=re.IGNORECASE,
        ).strip(" .")
        warnings.append(
            "A previously compiled prompt was reduced to its source voice "
            "description before recompilation."
        )

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
    owner_character_ids: dict[str, str] = {}
    for speaker_id in sorted(speaking_ids):
        character = registry.characters[speaker_id]
        owner_id = character.voice_id or speaker_id

        # Narration is the one role where a project deliberately exposes an
        # unassigned alternative at the review gate.  Preserve a previously
        # selected narrator candidate even though it is not a registry entry.
        if speaker_id == "narrator" and owner_id in {
            "narrator_male",
            "narrator_female",
        }:
            owner_character_ids[owner_id] = speaker_id
            owner_to_speakers.setdefault(owner_id, []).append(speaker_id)
            continue
        
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

        owner_character_ids[owner_id] = owner_id
        owner_to_speakers.setdefault(owner_id, []).append(speaker_id)

    # A new project offers two concrete narrator references during voice
    # review.  Only one is assigned, so downstream generation remains exactly
    # one voice per script line.  Other characters still receive no unused
    # profiles.
    if "narrator" in speaking_ids:
        narrator = registry.characters["narrator"]
        selected_id = narrator.voice_id or "narrator"
        selected_gender = (
            Gender.MALE
            if selected_id == "narrator_male"
            else Gender.FEMALE
            if selected_id == "narrator_female"
            else narrator.gender
        )
        alternative_gender = (
            Gender.MALE if selected_gender != Gender.MALE else Gender.FEMALE
        )
        alternative_id = f"narrator_{alternative_gender.value}"
        owner_character_ids.setdefault(alternative_id, "narrator")
        owner_to_speakers.setdefault(alternative_id, [])

    voices: dict[str, dict[str, Any]] = {}
    previous_prompts: list[tuple[str, str, str]] = []
    used_contrasts: set[str] = set()
    # Keep the ordinary speaking cast's palette stable when narrator choices
    # are added or changed.  Alternatives are deliberately allocated last so
    # introducing the extra preview never changes another character's design
    # fingerprint or triggers unrelated regeneration.
    voice_order = sorted(
        owner_to_speakers,
        key=lambda candidate: (
            candidate in {"narrator_male", "narrator_female"},
            candidate,
        ),
    )
    for voice_id in voice_order:
        owner_character_id = owner_character_ids.get(voice_id, voice_id)
        owner = registry.characters[owner_character_id]
        profile_gender = (
            Gender.MALE
            if voice_id == "narrator_male"
            else Gender.FEMALE
            if voice_id == "narrator_female"
            else owner.gender
        )
        prompt, warnings = compile_effective_voice_prompt(
            gender=profile_gender,
            age_range=owner.age_range,
            source_description=owner.voice_description,
            speaking_style=owner.speaking_style,
        )
        source_signature = _normalized_signature(owner.voice_description)
        similar_to = [
            previous_id
            for previous_id, previous_prompt, previous_source in previous_prompts
            if (
                source_signature
                and source_signature == _normalized_signature(previous_source)
            )
            or (
                _token_similarity(
                    owner.voice_description,
                    previous_source,
                )
                >= _SOURCE_SIMILARITY_THRESHOLD
            )
            or (
                _token_similarity(prompt, previous_prompt)
                >= _PROMPT_SIMILARITY_THRESHOLD
            )
        ]
        # Every speaking profile receives a stable palette direction. Voice
        # Design otherwise tends to collapse same-gender characters onto the
        # shared reference sentence even when their prose descriptions differ.
        contrast = _contrast_for(voice_id, used_contrasts)
        prompt += f" Distinguishing direction: {contrast}"
        if similar_to:
            warnings.append(
                "The initial profile was too similar to "
                f"{', '.join(similar_to)}; its dedicated contrast direction "
                "is required."
            )

        assigned = owner_to_speakers[voice_id]
        display_name = owner.name
        if owner_character_id == "narrator":
            display_name = f"Narrator — {profile_gender.value.title()}"
        profile_payload = {
            "schema": VOICE_CAST_SCHEMA_VERSION,
            "voice_id": voice_id,
            "owner_character_id": owner_character_id,
            "name": display_name,
            "gender": profile_gender.value,
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
                "gender": profile_gender.value,
                "age_range": owner.age_range,
                "effective_prompt": prompt,
                "design_model": design_model,
                "design_config": design_config or {},
            }
        )
        voices[voice_id] = profile_payload
        previous_prompts.append((voice_id, prompt, owner.voice_description))

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
