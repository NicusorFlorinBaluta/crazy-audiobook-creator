"""Voice Designer — Bootstrap character voices from text descriptions.

Orchestrates the Voice Design → Save → Clone workflow:
1. Takes character voice descriptions from the LLM
2. Generates reference clips using Qwen3-TTS VoiceDesign
3. Saves them to the Voice Library for reuse
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from voice.tts_server.qwen3_engine import Qwen3TTSEngine
from voice.tts_server.voice_library import VoiceLibraryManager
from shared.constants import Gender, VOICE_DESIGN_TEST_SENTENCES
from shared.models import (
    BootstrapVoiceResult,
    BootstrapVoicesRequest,
    BootstrapVoicesResponse,
    Character,
)

logger = logging.getLogger(__name__)


class VoiceDesigner:
    """Generate unique voice reference clips for characters."""

    def __init__(
        self,
        engine: Qwen3TTSEngine,
        library: VoiceLibraryManager,
        validator: Any = None,
        voice_design_duration: float = 10.0,
        voice_design_model: str = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        voice_design_test_sentences: dict[str, str] | None = None,
        wer_threshold: float = 0.20,
        similarity_warning_threshold: float = 0.88,
        acoustic_regeneration_attempts: int = 1,
    ):
        self.engine = engine
        self.library = library
        self.validator = validator
        self.voice_design_duration = voice_design_duration
        self.voice_design_model = voice_design_model
        self.wer_threshold = wer_threshold
        self.similarity_warning_threshold = max(
            -1.0, min(1.0, similarity_warning_threshold)
        )
        self.acoustic_regeneration_attempts = max(
            0, int(acoustic_regeneration_attempts)
        )
        configured_sentences = voice_design_test_sentences or {}
        self.voice_design_test_sentences = {
            "male": configured_sentences.get(
                "male", VOICE_DESIGN_TEST_SENTENCES["male"]
            ),
            "female": configured_sentences.get(
                "female", VOICE_DESIGN_TEST_SENTENCES["female"]
            ),
            "other": configured_sentences.get(
                "other",
                configured_sentences.get(
                    "neutral", VOICE_DESIGN_TEST_SENTENCES["other"]
                ),
            ),
        }

    def bootstrap_voices(
        self,
        request: BootstrapVoicesRequest,
    ) -> BootstrapVoicesResponse:
        """Generate voice reference clips for all characters in a project.

        For each character:
        1. Check if a voice already exists (skip if idempotent)
        2. Select a test sentence based on gender
3. Use Qwen3-TTS VoiceDesign to generate a reference clip
        4. Transcribe reference clip with Whisper for Full ICL mode
        5. Save to the voice library

        Args:
            request: Bootstrap request with project ID and characters.

        Returns:
            Response with generated voice file paths.
        """
        project_id = request.project_id
        voices_generated: dict[str, BootstrapVoiceResult] = {}

        import httpx
        
        logger.info(
            "Bootstrapping %d voices for project '%s'",
            len(request.characters),
            project_id,
        )

        # Boot the dedicated VoiceDesign model only for this stage so its
        # VRAM can be released before the cloning model is loaded.
        logger.info("Booting Qwen VoiceDesign Microservice on port 8101...")
        python_exe = Path(sys.executable)

        design_script = Path("qwen_voice_design_server.py").resolve()
        log_file = open("qwen-voice-design.log", "w", encoding="utf-8")
        popen_kwargs: dict[str, Any] = {
            "stdout": log_file,
            "stderr": subprocess.STDOUT,
            "env": {
                **os.environ,
                "QWEN_VOICE_DESIGN_MODEL": self.voice_design_model,
            },
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            design_proc = subprocess.Popen(
                [str(python_exe), str(design_script)],
                **popen_kwargs,
            )
        except Exception:
            log_file.close()
            raise
        try:
            # Allow up to 10 minutes for a first-time model download.
            for _ in range(300):
                if design_proc.poll() is not None:
                    raise RuntimeError(
                        "Qwen VoiceDesign Microservice exited during startup; "
                        "see qwen-voice-design.log"
                    )
                try:
                    resp = httpx.get(
                        "http://127.0.0.1:8101/health",
                        timeout=2.0,
                    )
                    if (
                        resp.status_code == 200
                        and resp.json().get("model_loaded") is True
                    ):
                        logger.info("Qwen VoiceDesign Microservice is ready!")
                        break
                except Exception:
                    pass
                time.sleep(2)
            else:
                raise RuntimeError(
                    "Qwen VoiceDesign Microservice failed to start; "
                    "see qwen-voice-design.log"
                )

            for char_id, character in request.characters.items():
                # Check if voice already exists and skip if not forcing regeneration
                if not request.force_regenerate and self.library.voice_exists(
                    project_id, char_id
                ):
                    existing = self.library.get_voice_info(project_id, char_id)
                    expected_fingerprint = request.design_fingerprints.get(
                        char_id, ""
                    )
                    fingerprint_matches = (
                        not expected_fingerprint
                        or existing
                        and existing.get("design_fingerprint")
                        == expected_fingerprint
                    )
                    if existing and fingerprint_matches:
                        logger.info("Voice for '%s' already exists, skipping", char_id)
                        voices_generated[char_id] = BootstrapVoiceResult(
                            file=existing.get("file", ""),
                            duration_seconds=existing.get("duration_seconds", 0.0),
                            sample_rate=existing.get("sample_rate", 24000),
                        )
                        continue
                    if existing and not fingerprint_matches:
                        logger.info(
                            "Voice design fingerprint changed for '%s'; "
                            "regenerating reference",
                            char_id,
                        )

                # Generate voice reference clip
                result = self._generate_voice(
                    project_id,
                    char_id,
                    character,
                    design_fingerprint=request.design_fingerprints.get(
                        char_id, ""
                    ),
                )
                for redesign_attempt in range(
                    1, self.acoustic_regeneration_attempts + 1
                ):
                    _, early_warnings = self._acoustic_diagnostics(
                        Path(result.file),
                        character,
                    )
                    if not any(
                        "requested adult" in warning
                        for warning in early_warnings
                    ):
                        break
                    gender_key = (
                        character.gender.value
                        if isinstance(character.gender, Gender)
                        else str(character.gender)
                    ).lower()
                    correction = (
                        " Make the perceived vocal register unmistakably "
                        f"{gender_key} and consistent with the stated age; "
                        "preserve natural speech and do not caricature it."
                    )
                    logger.warning(
                        "Automatically redesigning '%s' after acoustic "
                        "register mismatch (attempt %d/%d)",
                        char_id,
                        redesign_attempt,
                        self.acoustic_regeneration_attempts,
                    )
                    self.library.delete_voice(project_id, char_id)
                    corrected_character = character.model_copy(
                        update={
                            "voice_description": (
                                character.voice_description + correction
                            )
                        }
                    )
                    result = self._generate_voice(
                        project_id,
                        char_id,
                        corrected_character,
                        design_fingerprint=request.design_fingerprints.get(
                            char_id, ""
                        ),
                    )
                    result.warnings.append(
                        "Automatically redesigned once after an acoustic "
                        "register mismatch."
                    )
                voices_generated[char_id] = result
        
        finally:
            logger.info("Shutting down Qwen VoiceDesign Microservice...")
            if design_proc.poll() is None:
                design_proc.terminate()
                try:
                    design_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    design_proc.kill()
                    design_proc.wait(timeout=10)
            log_file.close()

        # Validate only after VoiceDesign has released VRAM.
        if self.validator:
            validation_failures: list[str] = []
            for char_id, result in voices_generated.items():
                character = request.characters[char_id]
                gender_key = (
                    character.gender.value
                    if isinstance(character.gender, Gender)
                    else str(character.gender)
                )
                expected = self.voice_design_test_sentences.get(
                    gender_key,
                    self.voice_design_test_sentences["other"],
                )
                transcribed = self.validator.transcribe(result.file)
                wer = self.validator.calculate_wer(expected, transcribed)
                result.transcription_wer = float(wer)
                logger.info(
                    "Reference check for '%s': WER=%.3f transcript=%r",
                    char_id,
                    wer,
                    transcribed[:60],
                )
                if wer > self.wer_threshold:
                    self.library.delete_voice(project_id, char_id)
                    validation_failures.append(
                        f"{char_id} (WER={wer:.3f})"
                    )
            # Do not make chapter-scale TTS/Whisper co-residency an implicit
            # requirement. Release Whisper before loading the speaker encoder.
            self.validator.unload()
            if validation_failures:
                raise RuntimeError(
                    "Voice references failed transcript check: "
                    + ", ".join(validation_failures)
                )

        embeddings: dict[str, Any] = {}
        for char_id, result in voices_generated.items():
            character = request.characters[char_id]
            metrics, warnings = self._acoustic_diagnostics(
                Path(result.file),
                character,
            )
            result.acoustic_metrics = metrics
            result.warnings.extend(warnings)
            try:
                embeddings[char_id] = self.engine.speaker_embedding(result.file)
            except Exception as exc:
                logger.warning(
                    "Could not extract cast embedding for '%s': %s",
                    char_id,
                    exc,
                )
                result.warnings.append(
                    "Acoustic distinctness could not be checked."
                )

        voice_ids = list(embeddings)
        for index, left_id in enumerate(voice_ids):
            for right_id in voice_ids[index + 1:]:
                similarity = self.engine.embedding_similarity(
                    embeddings[left_id],
                    embeddings[right_id],
                )
                if similarity >= self.similarity_warning_threshold:
                    left_v = voices_generated[left_id]
                    right_v = voices_generated[right_id]

                    # Suppress false positives across different genders or large pitch deltas (>= 40 Hz)
                    left_g = str(getattr(left_v, "gender", "") or "").lower()
                    right_g = str(getattr(right_v, "gender", "") or "").lower()
                    
                    left_metrics = getattr(getattr(left_v, "quality", None), "acoustic_metrics", None)
                    right_metrics = getattr(getattr(right_v, "quality", None), "acoustic_metrics", None)
                    left_f0 = float(getattr(left_metrics, "median_f0_hz", 0.0) or 0.0) if left_metrics else 0.0
                    right_f0 = float(getattr(right_metrics, "median_f0_hz", 0.0) or 0.0) if right_metrics else 0.0

                    genders_differ = bool(left_g and right_g and left_g != right_g and left_g != "other" and right_g != "other")
                    pitch_differs = bool(left_f0 > 0 and right_f0 > 0 and abs(left_f0 - right_f0) >= 40.0)

                    if genders_differ or pitch_differs:
                        logger.info(
                            "Suppressing acoustic similarity warning for '%s' and '%s' (similarity: %.3f, gender: %s vs %s, pitch: %.1f Hz vs %.1f Hz)",
                            left_id,
                            right_id,
                            similarity,
                            left_g,
                            right_g,
                            left_f0,
                            right_f0,
                        )
                        continue

                    warning = (
                        f"Sounds very similar to {right_id} "
                        f"(speaker similarity {similarity:.3f})."
                    )
                    voices_generated[left_id].warnings.append(warning)
                    voices_generated[right_id].warnings.append(
                        f"Sounds very similar to {left_id} "
                        f"(speaker similarity {similarity:.3f})."
                    )
                    logger.warning(
                        "Cast voices '%s' and '%s' are acoustically similar: %.3f",
                        left_id,
                        right_id,
                        similarity,
                    )

        return BootstrapVoicesResponse(
            status="success",
            project_id=project_id,
            voices_generated=voices_generated,
        )

    @staticmethod
    def _acoustic_diagnostics(
        audio_path: Path,
        character: Character,
    ) -> tuple[dict[str, float], list[str]]:
        """Measure obvious audio/casting problems without making identity claims."""
        import librosa
        import numpy as np
        import soundfile as sf

        audio, sample_rate = sf.read(str(audio_path), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
        duration = float(len(audio) / sample_rate) if sample_rate else 0.0
        warnings: list[str] = []
        voiced_f0: Any = np.array([], dtype=np.float32)
        if audio.size >= 2048:
            try:
                f0 = librosa.yin(
                    audio,
                    fmin=65.0,
                    fmax=500.0,
                    sr=sample_rate,
                )
                voiced_f0 = f0[np.isfinite(f0)]
            except Exception as exc:
                logger.debug("Pitch diagnostic failed for %s: %s", audio_path, exc)
        median_f0 = (
            float(np.median(voiced_f0)) if voiced_f0.size else 0.0
        )
        gender = (
            character.gender.value
            if isinstance(character.gender, Gender)
            else str(character.gender)
        ).lower()
        age = character.age_range.lower()
        if "child" not in age and "young" not in age:
            if gender == "male" and median_f0 > 240.0:
                warnings.append(
                    "Pitch is unusually high for the requested adult male design; review this voice."
                )
            elif gender == "female" and 0.0 < median_f0 < 105.0:
                warnings.append(
                    "Pitch is unusually low for the requested adult female design; review this voice."
                )
        if peak >= 0.999:
            warnings.append("Reference preview reaches digital full scale; check clipping.")
        if rms < 0.005:
            warnings.append("Reference preview is unusually quiet.")
        return {
            "duration_seconds": duration,
            "peak_amplitude": peak,
            "rms_amplitude": rms,
            "median_f0_hz": median_f0,
        }, warnings

    def regenerate_voice(
        self,
        project_id: str,
        character_id: str,
        character: Character,
    ) -> BootstrapVoiceResult:
        """Force-regenerate a single character's voice."""
        logger.info("Regenerating voice for '%s' in project '%s'", character_id, project_id)
        response = self.bootstrap_voices(
            BootstrapVoicesRequest(
                project_id=project_id,
                characters={character_id: character},
                force_regenerate=True,
            )
        )
        return response.voices_generated[character_id]

    def _generate_voice(
        self,
        project_id: str,
        char_id: str,
        character: Character,
        design_fingerprint: str = "",
    ) -> BootstrapVoiceResult:
        """Generate a single voice reference clip."""
        # Select test sentence based on gender
        gender_key = character.gender.value if isinstance(character.gender, Gender) else str(character.gender)
        test_sentence = self.voice_design_test_sentences.get(
            gender_key,
            self.voice_design_test_sentences["other"],
        )

        # Generate a designed reference voice with Qwen3-TTS VoiceDesign.
        output_path = self.library.get_voice_path(project_id, char_id)

        logger.info(
            "Generating voice for '%s' (%s): %s",
            character.name,
            char_id,
            character.voice_description[:60],
        )

        import requests
        resp = requests.post(
            "http://127.0.0.1:8101/voices/design",
            json={
                "prompt": character.voice_description,
                "text": test_sentence,
                "output_path": str(output_path),
                "duration_seconds": self.voice_design_duration,
            },
            timeout=600,
        )
        
        if resp.status_code != 200:
            raise RuntimeError(f"Qwen VoiceDesign microservice failed: {resp.text}")

        # Try to read the file to get duration and sample rate
        import soundfile as sf
        audio, sr = sf.read(str(output_path))
        duration_seconds = len(audio) / sr

        # The synthesis input is the authoritative Full-ICL transcript.
        # Whisper validation is deferred until VoiceDesign has released VRAM.
        ref_text = test_sentence

        # Save to voice library registry
        self.library.register_voice(
            project_id=project_id,
            character_id=char_id,
            name=character.name,
            description=character.voice_description,
            gender=gender_key,
            file_path=str(output_path),
            duration_seconds=duration_seconds,
            sample_rate=sr,
            ref_text=ref_text,
            design_fingerprint=design_fingerprint,
            source_type="generated",
        )

        return BootstrapVoiceResult(
            file=str(output_path),
            duration_seconds=duration_seconds,
            sample_rate=sr,
        )
