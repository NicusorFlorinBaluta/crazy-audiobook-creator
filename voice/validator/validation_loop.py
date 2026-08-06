"""Fail-closed TTS generation, validation, and retry orchestration."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

import numpy as np
import soundfile as sf

from shared.constants import (
    QUALITY_SCORE_PASS_THRESHOLD,
    QUALITY_WEIGHT_ARTIFACT,
    QUALITY_WEIGHT_DURATION,
    QUALITY_WEIGHT_WER,
    VALIDATION_SCHEMA_VERSION,
    ValidationStatus,
)
from shared.models import (
    ChapterQualityReport,
    GenerateChapterResponse,
    QualityResult,
    ScriptLine,
)
from voice.tts_server.qwen3_engine import Qwen3TTSEngine
from voice.tts_server.voice_library import VoiceLibraryManager
from voice.validator.audio_analyzer import AudioAnalyzer
from voice.validator.whisper_validator import WhisperValidator

logger = logging.getLogger(__name__)


class GenerationCancelled(RuntimeError):
    """Raised when a project cancellation is observed between segments."""


class ValidationLoop:
    """Generate every requested segment, validate it, and preserve the best try."""

    def __init__(
        self,
        whisper: WhisperValidator,
        analyzer: AudioAnalyzer,
        engine: Qwen3TTSEngine,
        library: VoiceLibraryManager,
        wer_threshold: float = 0.20,
        max_retries: int = 3,
        embedding_store: Any | None = None,
        speaker_similarity_threshold: float = 0.55,
        keep_models_resident: bool = False,
        risk_aware_first_attempt: bool = False,
    ):
        self.whisper = whisper
        self.analyzer = analyzer
        self.engine = engine
        self.library = library
        self.wer_threshold = wer_threshold
        self.max_retries = max_retries
        self.embedding_store = embedding_store
        self.speaker_similarity_threshold = speaker_similarity_threshold
        self.keep_models_resident = keep_models_resident
        self.risk_aware_first_attempt = risk_aware_first_attempt

    def process_chapter(
        self,
        project_id: str,
        chapter_number: int,
        lines: list[ScriptLine],
        workspace: Path,
        validate: bool = True,
        auto_retry: bool = True,
        max_retries: int = 3,
        ws_connections: list | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        validation_terms: set[str] | None = None,
    ) -> GenerateChapterResponse:
        """Generate a chapter with one output and one result per line ID."""
        request_started = time.perf_counter()
        timings: dict[str, float] = {}

        def record_timing(name: str, started: float) -> None:
            timings[name] = timings.get(name, 0.0) + (
                time.perf_counter() - started
            )

        segments_dir = workspace / project_id / "segments"
        segments_dir.mkdir(parents=True, exist_ok=True)

        ids = [line.line_id for line in lines]
        if len(ids) != len(set(ids)):
            raise ValueError(f"Chapter {chapter_number} contains duplicate line IDs")

        total_lines = len(lines)
        generated_ids: list[str] = []
        generation_errors: dict[str, str] = {}
        reference_context: dict[str, tuple[Path, str, dict[str, Any]]] = {}
        total_duration = 0.0
        retry_limit = max(1, max_retries or self.max_retries)
        validation_terms = validation_terms or set()
        risk_adjusted_line_ids: list[str] = []

        logger.info(
            "Processing chapter %d: %d lines for project '%s'",
            chapter_number,
            total_lines,
            project_id,
        )

        # Phase 1: synthesize or reuse each segment. qwen_tts currently performs
        # these calls sequentially, so a fake batch adds no throughput.
        for index, line in enumerate(lines, 1):
            self._raise_if_cancelled(cancel_check)
            output_path = segments_dir / f"{line.line_id}.wav"
            voice_ref, ref_text = self._resolve_reference(project_id, line)
            (
                synthesis_text,
                synthesis_emotion,
                synthesis_speed,
                synthesis_fx,
                risk_reason,
            ) = self._initial_delivery(line, validation_terms)
            if risk_reason:
                risk_adjusted_line_ids.append(line.line_id)
            context = self._generation_context(
                voice_ref,
                ref_text,
                synthesis_text=(
                    synthesis_text if synthesis_text != line.text else None
                ),
                delivery_override=(
                    {
                        "reason": risk_reason,
                        "emotion": synthesis_emotion,
                        "speed": synthesis_speed,
                        "voice_fx": (
                            synthesis_fx.model_dump()
                            if synthesis_fx is not None
                            else None
                        ),
                    }
                    if risk_reason
                    else None
                ),
            )
            reference_context[line.line_id] = (voice_ref, ref_text, context)

            needs_regeneration = not self._valid_audio(output_path)
            if self.embedding_store and not needs_regeneration:
                needs_regeneration = self.embedding_store.line_needs_regeneration(
                    project_id=project_id,
                    line_id=line.line_id,
                    text=line.text,
                    speaker=line.voice_id or line.speaker,
                    emotion=line.emotion or "",
                    speed=line.speed,
                    fx_dict=line.voice_fx.model_dump() if line.voice_fx else None,
                    output_path=output_path,
                    generation_context=context,
                )

            if needs_regeneration:
                last_error: Exception | None = None
                for generation_attempt in range(1, retry_limit + 1):
                    self._raise_if_cancelled(cancel_check)
                    try:
                        operation_started = time.perf_counter()
                        self.engine.generate_speech(
                            text=synthesis_text,
                            voice_reference_path=voice_ref,
                            ref_text=ref_text,
                            emotion_instruction=synthesis_emotion,
                            speed=synthesis_speed,
                            voice_fx=synthesis_fx,
                            output_path=output_path,
                        )
                        record_timing("tts_synthesis", operation_started)
                        if not self._valid_audio(output_path):
                            raise RuntimeError("TTS returned no valid audio artifact")
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                        output_path.unlink(missing_ok=True)
                        logger.error(
                            "Generation failed for %s (attempt %d/%d): %s",
                            line.line_id,
                            generation_attempt,
                            retry_limit,
                            exc,
                        )
                if last_error is not None:
                    generation_errors[line.line_id] = str(last_error)
                    continue

            info = sf.info(str(output_path))
            total_duration += info.duration
            generated_ids.append(line.line_id)
            self._send_progress(
                progress_callback,
                ws_connections,
                project_id,
                chapter_number,
                line,
                index,
                total_lines,
            )

        missing_ids = [line_id for line_id in ids if line_id not in generated_ids]
        if missing_ids:
            logger.error(
                "Chapter %d generation incomplete; missing=%s errors=%s",
                chapter_number,
                missing_ids,
                generation_errors,
            )
            return self._failure_response(
                chapter_number,
                total_lines,
                generated_ids,
                missing_ids,
                total_duration,
            )

        if not validate:
            return GenerateChapterResponse(
                status="success",
                chapter_number=chapter_number,
                total_lines=total_lines,
                generated=len(generated_ids),
                total_duration_seconds=total_duration,
                segment_files_dir=str(segments_dir),
                generated_line_ids=generated_ids,
                failed_line_ids=[],
            )

        # Phase 2: validate every segment. Accepted validation is cached
        # independently from synthesis, so validator changes never force TTS
        # regeneration and unchanged WAVs do not reload Whisper on resume.
        speaker_similarity: dict[str, float | None] = {}
        validation_context_by_id: dict[str, dict[str, Any]] = {}
        quality_by_id: dict[str, QualityResult] = {}
        uncached_lines: list[ScriptLine] = []
        for line in lines:
            voice_ref, _, _ = reference_context[line.line_id]
            validation_context = self._validation_context(
                voice_ref=voice_ref,
                speed=line.speed,
                validation_terms=validation_terms,
            )
            validation_context_by_id[line.line_id] = validation_context
            cached_result = None
            if self.embedding_store:
                operation_started = time.perf_counter()
                cached_result = self.embedding_store.get_validation_result(
                    project_id=project_id,
                    line_id=line.line_id,
                    output_path=segments_dir / f"{line.line_id}.wav",
                    expected_text=line.text,
                    validation_context=validation_context,
                )
                record_timing("validation_cache_lookup", operation_started)
            if cached_result:
                quality_by_id[line.line_id] = QualityResult(
                    **cached_result
                ).model_copy(update={"attempt": 1})
                continue
            uncached_lines.append(line)

        for line in uncached_lines:
            voice_ref, _, _ = reference_context[line.line_id]
            try:
                operation_started = time.perf_counter()
                speaker_similarity[line.line_id] = self.engine.speaker_similarity(
                    segments_dir / f"{line.line_id}.wav",
                    voice_ref,
                )
                record_timing("speaker_similarity", operation_started)
            except Exception as exc:
                logger.warning(
                    "Speaker similarity unavailable for %s: %s",
                    line.line_id,
                    exc,
                )
                speaker_similarity[line.line_id] = None

        if uncached_lines:
            if not self.keep_models_resident:
                operation_started = time.perf_counter()
                self.engine.unload()
                record_timing("tts_unload", operation_started)
            operation_started = time.perf_counter()
            self.whisper.load()
            record_timing("whisper_load", operation_started)
            for line in uncached_lines:
                self._raise_if_cancelled(cancel_check)
                audio_path = segments_dir / f"{line.line_id}.wav"
                voice_ref, _, _ = reference_context[line.line_id]
                result = self._validate_segment(
                    str(audio_path),
                    line.text,
                    line.line_id,
                    line.speed,
                    voice_ref_path=voice_ref,
                    reference_pitch_median=reference_pitch_map[line.line_id],
                    speaker_similarity=speaker_similarity[line.line_id],
                    require_speaker_similarity=True,
                    validation_terms=validation_terms,
                    timing_accumulator=timings,
                )
                quality_by_id[line.line_id] = result

        quality_attempts: list[QualityResult] = [
            quality_by_id[line.line_id] for line in lines
        ]
        validation_cache_hits = total_lines - len(uncached_lines)
        validation_cache_misses = len(uncached_lines)
        logger.info(
            "[ValidatorCache] chapter=%d hits=%d misses=%d",
            chapter_number,
            validation_cache_hits,
            validation_cache_misses,
        )

        # Phase 3: retry both FAIL and FLAGGED results. Each retry is written to
        # a side file and replaces the current artifact only if it is better.
        candidates = [
            line
            for line in lines
            if not self._is_accepted(quality_by_id[line.line_id].status)
        ]
        retried = 0
        if auto_retry:
            for attempt in range(2, retry_limit + 1):
                if not candidates:
                    break
                self._raise_if_cancelled(cancel_check)
                if not self.keep_models_resident:
                    operation_started = time.perf_counter()
                    self.whisper.unload()
                    record_timing("whisper_unload", operation_started)
                operation_started = time.perf_counter()
                self.engine.load()
                record_timing("tts_load", operation_started)

                attempt_files: dict[str, Path] = {}
                attempt_similarity: dict[str, float | None] = {}
                attempt_speeds: dict[str, float] = {}
                for line in candidates:
                    self._raise_if_cancelled(cancel_check)
                    voice_ref, ref_text, _ = reference_context[line.line_id]
                    attempt_path = (
                        segments_dir / f".{line.line_id}.attempt-{attempt}.wav"
                    )
                    retry_emotion, retry_speed, retry_fx = (
                        self._retry_delivery(line, attempt)
                    )
                    retry_text = self._retry_synthesis_text(line, attempt)
                    try:
                        logger.info(
                            "Retrying %s with intelligibility fallback "
                            "(attempt=%d speed=%.2f emotion=%s fx=%s text=%s)",
                            line.line_id,
                            attempt,
                            retry_speed,
                            retry_emotion,
                            "preserved" if retry_fx is not None else "disabled",
                            (
                                "plain-normalized"
                                if retry_text != line.text
                                else "original"
                            ),
                        )
                        operation_started = time.perf_counter()
                        self.engine.generate_speech(
                            text=retry_text,
                            voice_reference_path=voice_ref,
                            ref_text=ref_text,
                            emotion_instruction=retry_emotion,
                            speed=retry_speed,
                            voice_fx=retry_fx,
                            output_path=attempt_path,
                        )
                        record_timing("retry_tts_synthesis", operation_started)
                        if not self._valid_audio(attempt_path):
                            raise RuntimeError("Retry produced no valid audio artifact")
                        attempt_files[line.line_id] = attempt_path
                        attempt_speeds[line.line_id] = retry_speed
                        try:
                            operation_started = time.perf_counter()
                            attempt_similarity[line.line_id] = (
                                self.engine.speaker_similarity(
                                    attempt_path,
                                    voice_ref,
                                )
                            )
                            record_timing(
                                "retry_speaker_similarity",
                                operation_started,
                            )
                        except Exception:
                            attempt_similarity[line.line_id] = None
                        retried += 1
                    except Exception as exc:
                        attempt_path.unlink(missing_ok=True)
                        logger.error(
                            "Retry generation failed for %s (attempt %d): %s",
                            line.line_id,
                            attempt,
                            exc,
                        )

                if not self.keep_models_resident:
                    operation_started = time.perf_counter()
                    self.engine.unload()
                    record_timing("tts_unload", operation_started)
                operation_started = time.perf_counter()
                self.whisper.load()
                record_timing("whisper_load", operation_started)
                next_candidates: list[ScriptLine] = []
                for line in candidates:
                    attempt_path = attempt_files.get(line.line_id)
                    if attempt_path is None:
                        next_candidates.append(line)
                        continue
                    candidate_result = self._validate_segment(
                        str(attempt_path),
                        line.text,
                        line.line_id,
                        attempt_speeds.get(line.line_id, line.speed),
                        attempt,
                        speaker_similarity=attempt_similarity.get(line.line_id),
                        require_speaker_similarity=True,
                        validation_terms=validation_terms,
                        timing_accumulator=timings,
                    )
                    quality_attempts.append(candidate_result)
                    current_result = quality_by_id[line.line_id]
                    if self._is_better(candidate_result, current_result):
                        os.replace(
                            attempt_path,
                            segments_dir / f"{line.line_id}.wav",
                        )
                        quality_by_id[line.line_id] = candidate_result
                        current_result = candidate_result
                    else:
                        attempt_path.unlink(missing_ok=True)
                    if not self._is_accepted(current_result.status):
                        next_candidates.append(line)
                candidates = next_candidates

        # Co-residency is scoped to this chapter's retry loop. Always release
        # Whisper at the request boundary so the next chapter's long initial
        # TTS pass runs without the measured co-residency slowdown.
        operation_started = time.perf_counter()
        self.whisper.unload()
        record_timing("whisper_unload", operation_started)
        operation_started = time.perf_counter()
        self.engine.load()
        record_timing("tts_load", operation_started)

        quality_results = [quality_by_id[line.line_id] for line in lines]
        failed_ids = [
            result.line_id
            for result in quality_results
            if result.status == ValidationStatus.FAIL
        ]
        flagged_ids = [
            result.line_id
            for result in quality_results
            if result.status == ValidationStatus.FLAGGED
        ]
        warning_ids = [
            result.line_id
            for result in quality_results
            if result.status == ValidationStatus.ACCEPTED_WITH_WARNING
        ]

        unaccepted_ids = [
            result.line_id
            for result in quality_results
            if not self._is_accepted(result.status)
        ]

        # Cache only selected artifacts that passed every required check.
        if self.embedding_store:
            for line in lines:
                result = quality_by_id[line.line_id]
                if not self._is_accepted(result.status):
                    continue
                _, _, context = reference_context[line.line_id]
                operation_started = time.perf_counter()
                self.embedding_store.save_validation_result(
                    project_id=project_id,
                    line_id=line.line_id,
                    output_path=segments_dir / f"{line.line_id}.wav",
                    expected_text=line.text,
                    validation_context=validation_context_by_id[line.line_id],
                    result=result.model_dump(mode="json"),
                )
                record_timing("validation_cache_write", operation_started)
                operation_started = time.perf_counter()
                self.embedding_store.save_generation_fingerprint(
                    project_id=project_id,
                    line_id=line.line_id,
                    text=line.text,
                    speaker=line.voice_id or line.speaker,
                    emotion=line.emotion or "",
                    speed=line.speed,
                    fx_dict=line.voice_fx.model_dump() if line.voice_fx else None,
                    output_path=segments_dir / f"{line.line_id}.wav",
                    duration_seconds=result.duration_seconds,
                    wer=result.wer,
                    quality_score=result.quality_score,
                    validation_status=result.status.value,
                    generation_context=context,
                )
                record_timing("generation_fingerprint_write", operation_started)

        wer_values = [result.wer for result in quality_results]
        quality_report = ChapterQualityReport(
            chapter_number=chapter_number,
            total_segments=total_lines,
            passed=sum(
                result.status == ValidationStatus.PASS for result in quality_results
            ),
            accepted_with_warning=len(warning_ids),
            failed=len(failed_ids),
            flagged=len(flagged_ids),
            total_retries=retried,
            average_wer=float(np.mean(wer_values)) if wer_values else 0.0,
            worst_wer=max(wer_values, default=0.0),
            average_quality_score=float(
                np.mean([result.quality_score for result in quality_results])
            )
            if quality_results
            else 0.0,
            flagged_lines=flagged_ids,
            warning_lines=warning_ids,
            artifact_detections=sum(
                result.clipping_detected for result in quality_results
            ),
        )

        timings["total"] = time.perf_counter() - request_started
        rounded_timings = {
            key: round(value, 6) for key, value in timings.items()
        }
        logger.info(
            "[ChapterTiming] chapter=%d timings=%s",
            chapter_number,
            json.dumps(rounded_timings, sort_keys=True),
        )
        return GenerateChapterResponse(
            status="failed" if unaccepted_ids else "success",
            chapter_number=chapter_number,
            total_lines=total_lines,
            generated=len(generated_ids),
            failed_validation=len(unaccepted_ids),
            accepted_with_warning=len(warning_ids),
            retried=retried,
            total_duration_seconds=total_duration,
            quality_report=quality_report,
            segment_files_dir=str(segments_dir),
            generated_line_ids=generated_ids,
            failed_line_ids=unaccepted_ids,
            quality_results=quality_attempts,
            validation_cache_hits=validation_cache_hits,
            validation_cache_misses=validation_cache_misses,
            timings_seconds=rounded_timings,
            risk_adjusted_line_ids=risk_adjusted_line_ids,
        )

    def validate_single(self, audio_file: str, expected_text: str) -> QualityResult:
        if not self.whisper.is_loaded:
            self.whisper.load()
        return self._validate_segment(audio_file, expected_text, "manual", 1.0)

    def _resolve_reference(
        self, project_id: str, line: ScriptLine
    ) -> tuple[Path, str]:
        voice_id = line.voice_id or line.speaker
        voice_ref = self.library.get_voice_path(project_id, voice_id)
        ref_text = self.library.get_voice_ref_text(project_id, voice_id)
        if not voice_ref.exists():
            logger.warning(
                "No voice reference for '%s'; using narrator reference",
                voice_id,
            )
            voice_ref = self.library.get_voice_path(project_id, "narrator")
            ref_text = self.library.get_voice_ref_text(project_id, "narrator")
        if not voice_ref.exists():
            raise FileNotFoundError(
                f"No voice reference for voice '{voice_id}' or narrator"
            )
        # Never combine one character's audio with another character's
        # transcript. An empty transcript intentionally selects x-vector mode.
        return voice_ref, ref_text or ""

    def _generation_context(
        self,
        voice_ref: Path,
        ref_text: str,
        synthesis_text: str | None = None,
        delivery_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        hash_file = (
            self.embedding_store.hash_file
            if self.embedding_store
            else lambda path: ""
        )
        hash_text = (
            self.embedding_store.hash_text
            if self.embedding_store
            else lambda value: ""
        )
        context = {
            "voice_reference_hash": hash_file(voice_ref),
            "reference_text_hash": hash_text(ref_text),
            "model": self.engine.model_name,
            "language": getattr(self.engine, "language", "auto"),
            "generation": getattr(self.engine, "generation_config", {}),
            # Frozen compatibility marker from generation fingerprint v1.
            # It is intentionally no longer tied to VALIDATION_SCHEMA_VERSION;
            # future validator changes therefore cannot invalidate audio.
            "validation_schema": "3",
        }
        if synthesis_text:
            context["synthesis_text"] = synthesis_text
        if delivery_override:
            context["delivery_override"] = delivery_override
        return context

    def _initial_delivery(
        self,
        line: ScriptLine,
        validation_terms: set[str],
    ) -> tuple[str, str | None, float, Any | None, str | None]:
        """Apply conservative first-attempt clarity controls to risky text."""
        synthesis_text = self._prepare_synthesis_text(
            line.spoken_text or line.text
        )
        if not self.risk_aware_first_attempt:
            return (
                synthesis_text,
                line.emotion,
                line.speed,
                line.voice_fx,
                None,
            )

        normalized = self.whisper._normalize_text(synthesis_text)
        words = normalized.split()
        letters = "".join(char for char in line.text if char.isalpha())
        expressive_short = (
            len(words) <= 3
            and bool(letters)
            and (letters.isupper() or "!" in line.text)
        )
        if expressive_short:
            return (
                normalized or synthesis_text,
                "clear emphatic delivery",
                1.0,
                None,
                "short_expressive",
            )
        return synthesis_text, line.emotion, line.speed, line.voice_fx, None

    def _validation_context(
        self,
        *,
        voice_ref: Path,
        speed: float,
        validation_terms: set[str],
    ) -> dict[str, Any]:
        """Return only inputs that can change validation acceptance."""
        hash_file = (
            self.embedding_store.hash_file
            if self.embedding_store
            else lambda path: ""
        )
        return {
            "validation_schema": VALIDATION_SCHEMA_VERSION,
            "whisper_model": getattr(self.whisper, "model_name", "unknown"),
            "wer_threshold": self.wer_threshold,
            "speaker_similarity_threshold": self.speaker_similarity_threshold,
            "voice_reference_hash": hash_file(voice_ref),
            "speed": round(float(speed), 6),
            "validation_terms": sorted(
                {
                    self.whisper._normalize_text(term)
                    for term in validation_terms
                    if self.whisper._normalize_text(term)
                }
            ),
            "analyzer": {
                "noise_threshold": getattr(
                    self.analyzer, "noise_threshold", -50.0
                ),
                "clipping_threshold": getattr(
                    self.analyzer, "clipping_threshold", -0.5
                ),
                "max_silence_seconds": getattr(
                    self.analyzer, "max_silence_seconds", 3.0
                ),
                "duration_tolerance": getattr(
                    self.analyzer, "duration_tolerance", 0.3
                ),
            },
        }

    def _validate_segment(
        self,
        audio_file: str,
        expected_text: str,
        line_id: str,
        speed: float,
        attempt: int = 1,
        speaker_similarity: float | None = None,
        require_speaker_similarity: bool = False,
        validation_terms: set[str] | None = None,
        timing_accumulator: dict[str, float] | None = None,
        voice_ref_path: Path | None = None,
    ) -> QualityResult:
        transcription_started = time.perf_counter()
        transcribed = self.whisper.transcribe(audio_file)
        if timing_accumulator is not None:
            timing_accumulator["whisper_transcription"] = (
                timing_accumulator.get("whisper_transcription", 0.0)
                + time.perf_counter()
                - transcription_started
            )
        # Validate against the deterministic spoken form used for synthesis.
        # The authored source remains unchanged, while fused expressive text
        # such as ``Letsgoletsgoletsgo`` is checked as three complete spoken
        # repetitions instead of one invented orthographic token.
        validation_text = self._prepare_synthesis_text(expected_text)
        wer = self.whisper.calculate_wer(validation_text, transcribed)
        text_similarity = self.whisper.calculate_text_similarity(
            validation_text,
            transcribed,
        )
        orthographic_segmentation_match = (
            self.whisper.is_orthographic_segmentation_match(
                validation_text,
                transcribed,
            )
        )
        compact_error_rate = 1.0 - text_similarity
        analysis_started = time.perf_counter()
        analysis = self.analyzer.analyze(audio_file, validation_text, speed)
        if timing_accumulator is not None:
            timing_accumulator["audio_analysis"] = (
                timing_accumulator.get("audio_analysis", 0.0)
                + time.perf_counter()
                - analysis_started
            )
            
        reference_pitch_median = 0.0
        if voice_ref_path and voice_ref_path.exists():
            if not hasattr(self, "_ref_pitch_cache"):
                self._ref_pitch_cache = {}
            if str(voice_ref_path) not in self._ref_pitch_cache:
                ref_analysis = self.analyzer.analyze(str(voice_ref_path), "", 1.0)
                self._ref_pitch_cache[str(voice_ref_path)] = ref_analysis.get("pitch_median", 0.0)
            reference_pitch_median = self._ref_pitch_cache[str(voice_ref_path)]
            
        word_count = len(self.whisper._normalize_text(validation_text).split())
        normalized_expected = self.whisper._normalize_text(validation_text)
        eligible_glossary_match = any(
            normalized_term
            and normalized_term in normalized_expected
            for normalized_term in (
                self.whisper._normalize_text(term)
                for term in (validation_terms or set())
            )
        )
        spelling_variant_match = (
            eligible_glossary_match
            and (
                (2 <= word_count <= 3 and text_similarity >= 0.75)
                or (word_count > 3 and text_similarity >= 0.90)
            )
        )
        glossary_adjusted_wer = self._glossary_adjusted_wer(
            normalized_expected,
            self.whisper._normalize_text(transcribed),
            validation_terms or set(),
        )
        glossary_phonetic_match = (
            eligible_glossary_match
            and glossary_adjusted_wer < wer
            and glossary_adjusted_wer <= self.wer_threshold
        )
        effective_text_error = (
            0.0
            if orthographic_segmentation_match
            else (
                min(wer, compact_error_rate, glossary_adjusted_wer)
                if spelling_variant_match or glossary_phonetic_match
                else wer
            )
        )
        quality_score = (
            (1 - effective_text_error) * QUALITY_WEIGHT_WER
            + analysis["artifact_score"] * QUALITY_WEIGHT_ARTIFACT
            + analysis["duration_score"] * QUALITY_WEIGHT_DURATION
        )
        estimated_word_errors = wer * max(word_count, 1)
        # For one- and two-word clips, one wrong word means the utterance is
        # materially wrong. Longer clips follow the configured WER threshold;
        # the former 6-15 word special case silently rejected lines that were
        # already below that configured threshold.
        length_sensitive_wer_failure = (
            (word_count <= 2 and estimated_word_errors > 0.05)
            or (word_count > 2 and wer > self.wer_threshold)
        ) and not (
            spelling_variant_match
            or glossary_phonetic_match
            or orthographic_segmentation_match
        )

        hard_audio_failure = (
            analysis["clipping_detected"]
            or analysis["has_long_silence"]
            or analysis["pacing_anomaly"]
            or (
                require_speaker_similarity
                and (
                    speaker_similarity is None
                    or speaker_similarity < self.speaker_similarity_threshold
                )
            )
        )
        if length_sensitive_wer_failure or hard_audio_failure:
            status = ValidationStatus.FAIL
            acceptance_reason = (
                "transcription_mismatch"
                if length_sensitive_wer_failure
                else "hard_audio_check"
            )
        elif (
            not analysis["duration_ok"]
            or analysis["noise_floor_db"] > self.analyzer.noise_threshold
            or quality_score < QUALITY_SCORE_PASS_THRESHOLD
        ):
            status = ValidationStatus.ACCEPTED_WITH_WARNING
            acceptance_reason = "accepted_soft_audio_warning"
        else:
            status = ValidationStatus.PASS
            acceptance_reason = (
                "orthographic_segmentation_equivalent"
                if orthographic_segmentation_match and wer > 0
                else (
                    "approved_glossary_spelling_variant"
                    if (
                        spelling_variant_match or glossary_phonetic_match
                    ) and wer > self.wer_threshold
                    else "wer_and_audio_checks"
                )
            )

        logger.info(
            "[Validator] %s attempt=%d status=%s WER=%.3f "
            "text_similarity=%.3f score=%.2f",
            line_id,
            attempt,
            status.value,
            wer,
            text_similarity,
            quality_score,
        )
        res = QualityResult(
            line_id=line_id,
            status=status,
            wer=wer,
            transcribed_text=transcribed,
            duration_seconds=analysis["duration_seconds"],
            expected_duration_seconds=analysis["expected_duration_seconds"],
            peak_dbfs=analysis["peak_dbfs"],
            noise_floor_db=analysis["noise_floor_db"],
            clipping_detected=analysis["clipping_detected"],
            duration_ok=analysis["duration_ok"],
            has_long_silence=analysis["has_long_silence"],
            pacing_anomaly=analysis["pacing_anomaly"],
            pitch_median=analysis.get("pitch_median", 0.0),
            reference_pitch_median=reference_pitch_median,
            text_similarity=text_similarity,
            effective_text_error=effective_text_error,
            acceptance_reason=acceptance_reason,
            speaker_similarity=speaker_similarity,
            quality_score=quality_score,
            attempt=attempt,
            metrics=analysis,
            warnings=[],
            passed_hard_gates=not hard_audio_failure and not length_sensitive_wer_failure,
        )
        
        # Phase 5.1/5.2 Report-only drift and join checks
        if reference_pitch_median > 0 and analysis.get("pitch_median", 0.0) > 0:
            pitch_delta = abs(analysis.get("pitch_median", 0.0) - reference_pitch_median) / reference_pitch_median
            if pitch_delta > 0.30:  # 30% deviation
                res.warnings.append("Drift check (report-only): Pitch significantly deviated from reference bounds.")
        if analysis.get("rms_dbfs", 0.0) < -30:
            res.warnings.append("Join check (report-only): Abrupt loudness drop suspected.")
            
        return res

    def _glossary_adjusted_wer(
        self,
        normalized_reference: str,
        normalized_hypothesis: str,
        validation_terms: set[str],
    ) -> float:
        """Discount phonetic ASR spellings only for approved book terms.

        Fictional names are often transcribed with plausible alternate
        spellings. The alignment below gives zero substitution cost only when
        the expected token is present in the project glossary and the observed
        token is a close character-level rendering. Insertions, deletions, and
        changes to ordinary prose retain their full WER cost.
        """
        reference_words = normalized_reference.split()
        hypothesis_words = normalized_hypothesis.split()
        if not reference_words:
            return 0.0 if not hypothesis_words else 1.0

        glossary_words = {
            word
            for term in validation_terms
            for word in self.whisper._normalize_text(term).split()
            if len(word) >= 4
        }
        if not glossary_words:
            return 1.0

        rows = len(reference_words) + 1
        columns = len(hypothesis_words) + 1
        distance = [[0.0] * columns for _ in range(rows)]
        for row in range(rows):
            distance[row][0] = float(row)
        for column in range(columns):
            distance[0][column] = float(column)

        for row in range(1, rows):
            expected = reference_words[row - 1]
            for column in range(1, columns):
                observed = hypothesis_words[column - 1]
                equivalent = expected == observed
                if not equivalent and expected in glossary_words:
                    equivalent = (
                        len(observed) >= 4
                        and SequenceMatcher(None, expected, observed).ratio()
                        >= 0.60
                    )
                substitution_cost = 0.0 if equivalent else 1.0
                distance[row][column] = min(
                    distance[row - 1][column] + 1.0,
                    distance[row][column - 1] + 1.0,
                    distance[row - 1][column - 1] + substitution_cost,
                )
        return min(distance[-1][-1] / len(reference_words), 1.0)

    @staticmethod
    def _is_better(candidate: QualityResult, current: QualityResult) -> bool:
        rank = {
            ValidationStatus.FAIL: 0,
            ValidationStatus.FLAGGED: 1,
            ValidationStatus.ACCEPTED_WITH_WARNING: 2,
            ValidationStatus.PASS: 3,
        }
        return (
            rank[candidate.status],
            candidate.quality_score,
            -candidate.wer,
        ) > (
            rank[current.status],
            current.quality_score,
            -current.wer,
        )

    @staticmethod
    def _is_accepted(status: ValidationStatus) -> bool:
        """Return whether a segment is safe to cache, master, and export."""
        return status in {
            ValidationStatus.PASS,
            ValidationStatus.ACCEPTED_WITH_WARNING,
        }

    @staticmethod
    def _retry_delivery(
        line: ScriptLine,
        attempt: int,
    ) -> tuple[str, float, Any | None]:
        """Progressively trade post-processing intensity for intelligibility."""
        if attempt <= 2:
            # Keep some pacing character, but remove shout/panic pitch and
            # tone changes that commonly make short lines hard to recognize.
            speed = 1.0 + (float(line.speed) - 1.0) * 0.35
            return "neutral clear articulation", speed, line.voice_fx

        # Last automatic attempt uses the clean cloned voice without extra
        # speed, pitch, or tonal post-processing.
        return "neutral clear articulation", 1.0, None

    def _retry_synthesis_text(
        self,
        line: ScriptLine,
        attempt: int,
    ) -> str:
        """Use plain spoken text for the last intelligibility fallback.

        Quotes, all-caps emphasis, and unusual punctuation can make a short
        expressive line less intelligible. Earlier attempts preserve the
        authored delivery; only the final retry removes those presentation
        cues. Validation continues to compare against the original text.
        """
        synthesis_text = self._prepare_synthesis_text(
            line.spoken_text or line.text
        )
        if attempt <= 2:
            return synthesis_text
        normalized = self.whisper._normalize_text(synthesis_text)
        return normalized or synthesis_text

    @staticmethod
    def _prepare_synthesis_text(text: str) -> str:
        """Separate exact concatenated repetitions before sending text to TTS.

        Some prose intentionally removes spaces for breathless delivery, for
        example ``Letsgoletsgoletsgo``. Neural TTS may merge or drop one of
        those repetitions. Three-or-more exact repetitions are rare in ordinary
        words, so separating them with commas improves count fidelity without
        rewriting normal compounds or two-part words such as ``couscous``.
        """

        def expand_repetition(match: re.Match[str]) -> str:
            token = match.group(0)
            folded = token.casefold()
            token_length = len(folded)
            spoken_overrides = {
                "letsgo": "Let's go",
            }
            for unit_length in range(2, token_length // 3 + 1):
                if token_length % unit_length:
                    continue
                repeat_count = token_length // unit_length
                if repeat_count < 3:
                    continue
                unit = folded[:unit_length]
                if len(set(unit)) < 2 or unit * repeat_count != folded:
                    continue
                displayed_unit = spoken_overrides.get(
                    unit,
                    token[:unit_length],
                )
                return ", ".join(
                    [displayed_unit]
                    + [displayed_unit.lower()] * (repeat_count - 1)
                )
            return token

        return re.sub(r"[^\W_]+", expand_repetition, text, flags=re.UNICODE)

    @staticmethod
    def _valid_audio(path: Path) -> bool:
        if not path.is_file() or path.stat().st_size < 1000:
            return False
        try:
            info = sf.info(str(path))
            return info.frames > 0 and info.samplerate > 0 and info.duration > 0
        except Exception:
            return False

    @staticmethod
    def _raise_if_cancelled(
        cancel_check: Callable[[], bool] | None,
    ) -> None:
        if cancel_check and cancel_check():
            raise GenerationCancelled("Chapter generation cancelled")

    @staticmethod
    def _send_progress(
        progress_callback: Callable[[dict[str, Any]], None] | None,
        ws_connections: list | None,
        project_id: str,
        chapter_number: int,
        line: ScriptLine,
        progress: int,
        total: int,
    ) -> None:
        message = {
            "type": "progress",
            "project_id": project_id,
            "chapter": chapter_number,
            "line_id": line.line_id,
            "progress": progress,
            "total": total,
            "percent": round(progress / total * 100, 1) if total else 0,
            "current_speaker": line.speaker,
            "current_emotion": line.emotion,
        }
        if progress_callback:
            progress_callback(message)
            return
        if ws_connections:
            # Kept for compatibility; the server now passes a thread-safe
            # callback instead of scheduling on a worker thread's event loop.
            logger.debug("No thread-safe progress callback; message=%s", json.dumps(message))

    @staticmethod
    def _failure_response(
        chapter_number: int,
        total_lines: int,
        generated_ids: list[str],
        failed_ids: list[str],
        total_duration: float,
    ) -> GenerateChapterResponse:
        return GenerateChapterResponse(
            status="failed",
            chapter_number=chapter_number,
            total_lines=total_lines,
            generated=len(generated_ids),
            failed_validation=len(failed_ids),
            total_duration_seconds=total_duration,
            quality_report=ChapterQualityReport(
                chapter_number=chapter_number,
                total_segments=total_lines,
                failed=len(failed_ids),
                flagged_lines=failed_ids,
            ),
            generated_line_ids=generated_ids,
            failed_line_ids=failed_ids,
        )
