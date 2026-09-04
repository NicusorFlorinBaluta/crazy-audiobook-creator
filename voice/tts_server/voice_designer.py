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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from shared.constants import VOICE_DESIGN_TEST_SENTENCES, Gender
from shared.models import (
    BootstrapVoiceResult,
    BootstrapVoicesRequest,
    BootstrapVoicesResponse,
    CastPairDiagnostic,
    Character,
    VoiceCandidate,
)
from voice.tts_server.qwen3_engine import Qwen3TTSEngine
from voice.tts_server.voice_library import VoiceLibraryManager

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

    def _build_test_sentence(self, char_id: str, character: Character) -> str:
        gender_key = (
            character.gender.value
            if hasattr(character.gender, "value")
            else str(character.gender)
        ).lower()
        fallback = self.voice_design_test_sentences.get(
            gender_key,
            self.voice_design_test_sentences.get("neutral", self.voice_design_test_sentences.get("other", "This is a fallback test sentence to ensure adequate duration for voice design references.")),
        )

        importance = getattr(character, "importance", "minor")
        if not character.test_sentence:
            if importance == "minor":
                return fallback
            raise ValueError(
                f"Major voice '{char_id}' has no usable source-backed "
                "reference text; choose dialogue before bootstrapping"
            )

        # Pad short sentences to ensure VoiceDesign has enough text to generate ~10s of audio
        # Only do this for minor characters, to avoid making all major characters sound too similar.
        if len(character.test_sentence.split()) < 12 and importance == "minor":
            return f"{character.test_sentence.strip()} {fallback}"

        return character.test_sentence

    @staticmethod
    def _candidate_id(voice_id: str, candidate_index: int) -> str:
        """Keep candidate one canonical; suffix only optional alternatives."""
        if candidate_index < 1:
            raise ValueError("candidate_index must start at 1")
        return voice_id if candidate_index == 1 else f"{voice_id}_cand{candidate_index}"

    def bootstrap_voices(
        self,
        request: BootstrapVoicesRequest,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
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

        def emit(
            phase: str,
            completed: int,
            total: int,
            message: str,
        ) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(
                    {
                        "phase": phase,
                        "completed": max(0, int(completed)),
                        "total": max(1, int(total)),
                        "message": message,
                    }
                )
            except Exception:
                logger.warning("Voice bootstrap progress callback failed", exc_info=True)

        def check_cancelled() -> None:
            if cancel_check is not None and cancel_check():
                raise RuntimeError("Voice bootstrapping cancelled")

        candidate_total = sum(
            max(
                1,
                min(
                    3,
                    int(
                        request.candidate_counts.get(
                            char_id,
                            3
                            if char_id == "narrator"
                            or getattr(character, "importance", "minor") == "major"
                            else 1,
                        )
                    ),
                ),
            )
            for char_id, character in request.characters.items()
        )
        emit(
            "loading_voice_design",
            0,
            1,
            "Loading the voice-design model",
        )

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
                check_cancelled()
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
                        emit(
                            "designing_references",
                            0,
                            candidate_total,
                            f"Preparing {candidate_total} voice reference candidates",
                        )
                        break
                except Exception:
                    pass
                time.sleep(2)
            else:
                raise RuntimeError(
                    "Qwen VoiceDesign Microservice failed to start; "
                    "see qwen-voice-design.log"
                )

            designed_candidates = 0
            for char_id, character in request.characters.items():
                check_cancelled()
                importance = getattr(character, "importance", "minor")
                default_count = 3 if char_id == "narrator" or importance == "major" else 1
                num_candidates = max(
                    1,
                    min(3, int(request.candidate_counts.get(char_id, default_count))),
                )

                candidates = []
                for cand_idx in range(1, num_candidates + 1):
                    check_cancelled()
                    # Candidate one is the canonical, initially assigned profile.
                    # Additional candidates are optional alternatives.  Keeping a
                    # real registry entry under char_id makes a newly bootstrapped
                    # major voice immediately usable and approvable.
                    cand_id = self._candidate_id(char_id, cand_idx)

                    if not request.force_regenerate and self.library.voice_exists(
                        project_id, cand_id
                    ):
                        existing = self.library.get_voice_info(project_id, cand_id)
                        if existing and existing.get("source_type") == "uploaded":
                            candidates.append(VoiceCandidate(
                                id=cand_id,
                                file=existing.get("file", ""),
                                duration_seconds=existing.get("duration_seconds", 0.0),
                                sample_rate=existing.get("sample_rate", 24000),
                            ))
                            designed_candidates += 1
                            emit(
                                "designing_references",
                                designed_candidates,
                                candidate_total,
                                f"Prepared {designed_candidates} of {candidate_total} voice candidates",
                            )
                            continue
                        expected_fingerprint = request.design_fingerprints.get(char_id, "")
                        fingerprint_matches = (
                            not expected_fingerprint
                            or existing
                            and existing.get("design_fingerprint") == expected_fingerprint
                        )
                        if existing and fingerprint_matches:
                            candidates.append(VoiceCandidate(
                                id=cand_id,
                                file=existing.get("file", ""),
                                duration_seconds=existing.get("duration_seconds", 0.0),
                                sample_rate=existing.get("sample_rate", 24000),
                            ))
                            designed_candidates += 1
                            emit(
                                "designing_references",
                                designed_candidates,
                                candidate_total,
                                f"Prepared {designed_candidates} of {candidate_total} voice candidates",
                            )
                            continue

                    result = self._generate_voice(
                        project_id,
                        cand_id,
                        character,
                        design_fingerprint=request.design_fingerprints.get(char_id, ""),
                    )

                    for redesign_attempt in range(1, self.acoustic_regeneration_attempts + 1):
                        _, early_warnings = self._acoustic_diagnostics(Path(result.file), character)
                        if not any("requested adult" in warning for warning in early_warnings):
                            break
                        gender_key = (
                            character.gender.value if isinstance(character.gender, Gender) else str(character.gender)
                        ).lower()
                        correction = (
                            " Make the perceived vocal register unmistakably "
                            f"{gender_key} and consistent with the stated age; "
                            "preserve natural speech and do not caricature it."
                        )
                        self.library.delete_voice(project_id, cand_id)
                        corrected_character = character.model_copy(
                            update={"voice_description": character.voice_description + correction}
                        )
                        result = self._generate_voice(
                            project_id,
                            cand_id,
                            corrected_character,
                            design_fingerprint=request.design_fingerprints.get(char_id, ""),
                        )
                        result.warnings.append("Automatically redesigned once after an acoustic register mismatch.")

                    candidates.append(result)
                    designed_candidates += 1
                    emit(
                        "designing_references",
                        designed_candidates,
                        candidate_total,
                        f"Prepared {designed_candidates} of {candidate_total} voice candidates",
                    )

                voices_generated[char_id] = BootstrapVoiceResult(
                    id=char_id,
                    file=candidates[0].file,
                    duration_seconds=candidates[0].duration_seconds,
                    sample_rate=candidates[0].sample_rate,
                    transcription_wer=candidates[0].transcription_wer,
                    acoustic_metrics=candidates[0].acoustic_metrics,
                    warnings=candidates[0].warnings,
                    candidates=candidates,
                )

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
            validation_total = sum(
                len(result.candidates)
                for result in voices_generated.values()
            )
            validation_completed = 0
            emit(
                "validating_transcripts",
                0,
                validation_total,
                "Checking reference transcripts",
            )
            for char_id, result in voices_generated.items():
                check_cancelled()
                character = request.characters[char_id]
                expected = self._build_test_sentence(char_id, character)

                validated_candidates: list[VoiceCandidate] = []
                for candidate_index, candidate in enumerate(result.candidates):
                    check_cancelled()
                    transcribed = self.validator.transcribe(candidate.file)
                    wer = self.validator.calculate_wer(expected, transcribed)
                    candidate.transcription_wer = float(wer)
                    logger.info(
                        "Reference check for '%s': WER=%.3f transcript=%r",
                        candidate.id,
                        wer,
                        transcribed[:60],
                    )
                    if wer > self.wer_threshold:
                        logger.warning(
                            "Reference candidate '%s' has elevated transcript WER=%.3f (threshold=%.2f); retaining for Voice Review",
                            candidate.id,
                            wer,
                            self.wer_threshold,
                        )
                        candidate.warnings.append(
                            f"Reference transcript WER {wer:.2f} exceeded threshold ({self.wer_threshold:.2f}); manual audition and approval required in Voice Review."
                        )
                    validated_candidates.append(candidate)
                    validation_completed += 1
                    emit(
                        "validating_transcripts",
                        validation_completed,
                        validation_total,
                        f"Checked {validation_completed} of {validation_total} reference transcripts",
                    )
                result.candidates = validated_candidates
                if not validated_candidates:
                    continue
                result.file = validated_candidates[0].file
                result.transcription_wer = result.candidates[0].transcription_wer
            # Do not make chapter-scale TTS/Whisper co-residency an implicit
            # requirement. Release Whisper before loading the speaker encoder.
            self.validator.unload()

        embeddings: dict[str, Any] = {}
        acoustic_total = sum(
            len(result.candidates)
            for result in voices_generated.values()
        )
        acoustic_completed = 0
        emit(
            "measuring_references",
            0,
            acoustic_total,
            "Measuring reference audio quality",
        )
        for char_id, result in voices_generated.items():
            check_cancelled()
            character = request.characters[char_id]
            for cand in result.candidates:
                check_cancelled()
                metrics, warnings = self._acoustic_diagnostics(
                    Path(cand.file), character
                )
                cand.acoustic_metrics = metrics
                cand.warnings.extend(warnings)
                acoustic_completed += 1
                emit(
                    "measuring_references",
                    acoustic_completed,
                    acoustic_total,
                    f"Measured {acoustic_completed} of {acoustic_total} references",
                )
            if result.candidates:
                result.acoustic_metrics = result.candidates[0].acoustic_metrics
                result.warnings = list(result.candidates[0].warnings)
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

        cast_diagnostics: list[CastPairDiagnostic] = []
        voice_ids = list(embeddings)
        pair_total = len(voice_ids) * max(0, len(voice_ids) - 1) // 2
        pair_completed = 0
        emit(
            "comparing_cast",
            0,
            max(1, pair_total),
            "Comparing cast distinctness",
        )
        for index, left_id in enumerate(voice_ids):
            for right_id in voice_ids[index + 1:]:
                check_cancelled()
                similarity = self.engine.embedding_similarity(
                    embeddings[left_id],
                    embeddings[right_id],
                )
                left_v = voices_generated[left_id]
                right_v = voices_generated[right_id]
                diagnostic = self._cast_pair_diagnostic(
                    left_id,
                    right_id,
                    similarity,
                    left_v.acoustic_metrics or {},
                    right_v.acoustic_metrics or {},
                    self.similarity_warning_threshold,
                )
                cast_diagnostics.append(diagnostic)
                pair_completed += 1
                if pair_completed % 25 == 0 or pair_completed == pair_total:
                    emit(
                        "comparing_cast",
                        pair_completed,
                        max(1, pair_total),
                        f"Compared {pair_completed} of {pair_total} cast pairs",
                    )
                if diagnostic.status == "similar":
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

                    # The VoiceDesign helper has already been stopped so the Base
                    # speaker encoder can run without VRAM co-residency.  Do not
                    # attempt an impossible late fallback generation here; the
                    # warning is surfaced for an explicit user redesign instead.

        emit(
            "complete",
            1,
            1,
            "Voice references are ready for review",
        )
        return BootstrapVoicesResponse(
            status="success",
            project_id=project_id,
            voices_generated=voices_generated,
            cast_diagnostics=cast_diagnostics,
        )

    @staticmethod
    def _cast_pair_diagnostic(
        left_id: str,
        right_id: str,
        speaker_similarity: float,
        left_metrics: dict[str, float],
        right_metrics: dict[str, float],
        warning_threshold: float,
    ) -> CastPairDiagnostic:
        """Combine identity, pitch, and spectral evidence without hard-failing."""
        left_f0 = float(left_metrics.get("median_f0_hz", 0.0) or 0.0)
        right_f0 = float(right_metrics.get("median_f0_hz", 0.0) or 0.0)
        pitch_delta = abs(left_f0 - right_f0) if left_f0 and right_f0 else 0.0
        left_range = float(left_metrics.get("f0_range_hz", 0.0) or 0.0)
        right_range = float(right_metrics.get("f0_range_hz", 0.0) or 0.0)
        range_delta = abs(left_range - right_range)
        left_centroid = float(
            left_metrics.get("spectral_centroid_hz", 0.0) or 0.0
        )
        right_centroid = float(
            right_metrics.get("spectral_centroid_hz", 0.0) or 0.0
        )
        centroid_delta = abs(left_centroid - right_centroid)

        pitch_similarity = 1.0 - min(pitch_delta / 180.0, 1.0)
        range_similarity = 1.0 - min(range_delta / 160.0, 1.0)
        spectral_similarity = 1.0 - min(centroid_delta / 2500.0, 1.0)
        composite = (
            0.75 * float(speaker_similarity)
            + 0.15 * pitch_similarity
            + 0.05 * range_similarity
            + 0.05 * spectral_similarity
        )
        composite = max(-1.0, min(1.0, composite))

        objectively_contrasted = (
            pitch_delta >= 40.0 and centroid_delta >= 350.0
        )
        similar = (
            speaker_similarity >= warning_threshold
            and not objectively_contrasted
        )
        return CastPairDiagnostic(
            left_voice_id=left_id,
            right_voice_id=right_id,
            speaker_similarity=float(speaker_similarity),
            composite_similarity=composite,
            pitch_delta_hz=pitch_delta,
            pitch_range_delta_hz=range_delta,
            spectral_centroid_delta_hz=centroid_delta,
            status="similar" if similar else "distinct",
            warning_suppressed=bool(
                speaker_similarity >= warning_threshold and objectively_contrasted
            ),
            suppression_reason=(
                "speaker embedding is similar, but both pitch and spectral "
                "centroid are materially separated"
                if speaker_similarity >= warning_threshold and objectively_contrasted
                else None
            ),
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
        dc_offset = float(abs(np.mean(audio))) if audio.size else 0.0
        duration = float(len(audio) / sample_rate) if sample_rate else 0.0
        clipping_fraction = (
            float(np.mean(np.abs(audio) >= 0.999)) if audio.size else 0.0
        )
        silence_threshold = max(0.002, rms * 0.10)
        silence_ratio = (
            float(np.mean(np.abs(audio) < silence_threshold))
            if audio.size else 1.0
        )
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
        f0_p10 = float(np.percentile(voiced_f0, 10)) if voiced_f0.size else 0.0
        f0_p90 = float(np.percentile(voiced_f0, 90)) if voiced_f0.size else 0.0
        spectral_centroid = 0.0
        spectral_flatness = 0.0
        if audio.size >= 2048:
            try:
                spectral_centroid = float(
                    np.median(
                        librosa.feature.spectral_centroid(
                            y=audio, sr=sample_rate
                        )
                    )
                )
                spectral_flatness = float(
                    np.median(librosa.feature.spectral_flatness(y=audio))
                )
            except Exception as exc:
                logger.debug("Spectral diagnostic failed for %s: %s", audio_path, exc)
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
        if clipping_fraction > 0.001:
            warnings.append("Reference preview contains excessive clipped samples.")
        if dc_offset > 0.02:
            warnings.append("Reference preview has an unusual DC offset.")
        if silence_ratio > 0.60:
            warnings.append("Reference preview contains unusually much silence.")
        return {
            "duration_seconds": duration,
            "peak_amplitude": peak,
            "rms_amplitude": rms,
            "dc_offset": dc_offset,
            "clipping_fraction": clipping_fraction,
            "silence_ratio": silence_ratio,
            "median_f0_hz": median_f0,
            "f0_p10_hz": f0_p10,
            "f0_p90_hz": f0_p90,
            "f0_range_hz": max(0.0, f0_p90 - f0_p10),
            "spectral_centroid_hz": spectral_centroid,
            "spectral_flatness": spectral_flatness,
        }, warnings

    def regenerate_voice(
        self,
        project_id: str,
        character_id: str,
        character: Character,
    ) -> VoiceCandidate:
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
    ) -> VoiceCandidate:
        """Generate a single voice reference clip."""
        test_sentence = self._build_test_sentence(char_id, character)

        import uuid
        base_path = self.library.get_voice_path(project_id, char_id)
        output_path = base_path.with_name(f"{base_path.stem}_{uuid.uuid4().hex[:8]}{base_path.suffix}")

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
        gender_key = character.gender.value if hasattr(character.gender, "value") else str(character.gender)
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

        return VoiceCandidate(
            id=char_id,
            file=str(output_path),
            duration_seconds=duration_seconds,
            sample_rate=sr,
        )
