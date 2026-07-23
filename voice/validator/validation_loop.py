"""Validation Loop — Orchestrates TTS generation + quality validation with retry.

Implements the generate → validate → retry pipeline for each segment,
with configurable retry limits and quality thresholds.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import numpy as np

from voice.tts_server.qwen3_engine import Qwen3TTSEngine
from voice.tts_server.voice_library import VoiceLibraryManager
from voice.validator.whisper_validator import WhisperValidator
from voice.validator.audio_analyzer import AudioAnalyzer
from shared.constants import (
    QUALITY_WEIGHT_ARTIFACT,
    QUALITY_WEIGHT_DURATION,
    QUALITY_WEIGHT_WER,
    QUALITY_SCORE_PASS_THRESHOLD,
    ValidationStatus,
)
from shared.models import (
    ChapterQualityReport,
    GenerateChapterResponse,
    QualityResult,
    ScriptLine,
)

logger = logging.getLogger(__name__)


class ValidationLoop:
    """Generate audio segments with quality validation and retry logic."""

    def __init__(
        self,
        whisper: WhisperValidator,
        analyzer: AudioAnalyzer,
        engine: Qwen3TTSEngine,
        library: VoiceLibraryManager,
        wer_threshold: float = 0.20,
        max_retries: int = 3,
        embedding_store: Any | None = None,
    ):
        self.whisper = whisper
        self.analyzer = analyzer
        self.engine = engine
        self.library = library
        self.wer_threshold = wer_threshold
        self.max_retries = max_retries
        self.embedding_store = embedding_store

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
    ) -> GenerateChapterResponse:
        """Process an entire chapter: generate all lines + validate + retry.

        Uses batch mode: generate all → validate all → retry failed.

        Args:
            project_id: Project identifier.
            chapter_number: Chapter number.
            lines: List of script lines to generate.
            workspace: Workspace directory for output files.
            validate: Whether to run quality validation.
            auto_retry: Whether to auto-retry failed segments.
            max_retries: Maximum retry attempts per line.
            ws_connections: WebSocket connections for progress updates.

        Returns:
            GenerateChapterResponse with quality report.
        """
        segments_dir = workspace / project_id / "segments"
        segments_dir.mkdir(parents=True, exist_ok=True)

        total_lines = len(lines)
        generated = 0
        retried = 0
        failed_validation = 0
        total_duration = 0.0
        quality_results: list[QualityResult] = []
        flagged_lines: list[str] = []

        logger.info(
            "Processing chapter %d: %d lines for project '%s'",
            chapter_number,
            total_lines,
            project_id,
        )

        # Phase 1: Generate all segments in batches in narrative script order
        BATCH_SIZE = 5  # Configurable batch size
        
        for i in range(0, total_lines, BATCH_SIZE):
            batch_lines = lines[i:i + BATCH_SIZE]
            batch_requests = []
            
            for line in batch_lines:
                output_path = segments_dir / f"{line.line_id}.wav"
                fx_dict = line.voice_fx.model_dump() if getattr(line, "voice_fx", None) else None
                
                needs_regen = True
                if self.embedding_store:
                    needs_regen = self.embedding_store.line_needs_regeneration(
                        project_id=project_id,
                        line_id=line.line_id,
                        text=line.text,
                        speaker=line.speaker,
                        emotion=line.emotion or "",
                        speed=getattr(line, "speed", 1.0),
                        fx_dict=fx_dict,
                        output_path=output_path,
                    )
                else:
                    needs_regen = not (output_path.exists() and output_path.stat().st_size > 1000)

                if not needs_regen:
                    try:
                        import soundfile as sf
                        info = sf.info(str(output_path))
                        total_duration += info.duration
                        generated += 1
                        logger.info("Line %s audio already exists & fingerprint matches (%.2fs), skipping synthesis", line.line_id, info.duration)
                        continue
                    except Exception:
                        pass

                voice_ref = self.library.get_voice_path(project_id, line.speaker)
                ref_text = self.library.get_voice_ref_text(project_id, line.speaker)

                if not voice_ref.exists():
                    logger.warning("No voice reference for '%s', using narrator reference with Full ICL", line.speaker)
                    voice_ref = self.library.get_voice_path(project_id, "narrator")
                    ref_text = self.library.get_voice_ref_text(project_id, "narrator")

                if not ref_text:
                    ref_text = self.library.get_voice_ref_text(project_id, "narrator")
                    
                batch_requests.append({
                    "text": line.text,
                    "voice_reference_path": voice_ref,
                    "ref_text": ref_text,
                    "emotion_instruction": line.emotion,
                    "speed": line.speed,
                    "voice_fx": line.voice_fx,
                    "output_path": output_path,
                })
                
            if not batch_requests:
                continue

            try:
                # Generate a batch concurrently on GPU
                audios = self.engine.generate_speech_batch(batch_requests)
                
                for idx, audio in enumerate(audios):
                    line = batch_lines[idx]
                    duration = len(audio) / self.engine.sample_rate
                    total_duration += duration
                    generated += 1
                    
                    if ws_connections:
                        self._send_progress(
                            ws_connections, project_id, chapter_number,
                            line.line_id, generated, total_lines,
                            line.speaker, line.emotion,
                        )
                        
            except Exception as e:
                logger.error("Batch generation failed at index %d: %s", i, e)
                continue

        # Phase 2: Validate all segments (if enabled)
        if validate:
            logger.info("Validating %d segments...", generated)

            import gc, torch
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

            # Load Whisper model (keeping TTS loaded in VRAM)
            self.whisper.load()

            for line in lines:
                audio_path = segments_dir / f"{line.line_id}.wav"
                if not audio_path.exists():
                    continue

                result = self._validate_segment(
                    str(audio_path), line.text, line.line_id, line.speed
                )
                quality_results.append(result)

                if self.embedding_store:
                    fx_dict = line.voice_fx.model_dump() if getattr(line, "voice_fx", None) else None
                    self.embedding_store.save_generation_fingerprint(
                        project_id=project_id,
                        line_id=line.line_id,
                        text=line.text,
                        speaker=line.speaker,
                        emotion=line.emotion or "",
                        speed=getattr(line, "speed", 1.0),
                        fx_dict=fx_dict,
                        output_path=audio_path,
                        duration_seconds=result.duration_seconds,
                        wer=result.wer,
                        quality_score=result.quality_score,
                        validation_status=str(result.status.value) if hasattr(result.status, "value") else str(result.status),
                    )

                if result.status == ValidationStatus.FAIL:
                    failed_validation += 1
                elif result.status == ValidationStatus.FLAGGED:
                    flagged_lines.append(line.line_id)

            # Phase 3: Retry failed segments
            if auto_retry and failed_validation > 0:
                logger.info("Retrying %d failed segments...", failed_validation)

                # Ensure TTS engine is ready for retries
                self.engine.load()

                failed_lines = [
                    line for line, result in zip(lines, quality_results)
                    if result.status == ValidationStatus.FAIL
                ]

                for attempt in range(2, max_retries + 1):
                    if not failed_lines:
                        break

                    still_failed: list[ScriptLine] = []

                    for line in failed_lines:
                        output_path = segments_dir / f"{line.line_id}.wav"
                        voice_ref = self.library.get_voice_path(project_id, line.speaker)
                        ref_text = self.library.get_voice_ref_text(project_id, line.speaker)
                        if not voice_ref.exists():
                            voice_ref = self.library.get_voice_path(project_id, "narrator")
                            ref_text = self.library.get_voice_ref_text(project_id, "narrator")

                        try:
                            audio = self.engine.generate_speech(
                                text=line.text,
                                voice_reference_path=voice_ref,
                                ref_text=ref_text,
                                emotion_instruction=line.emotion,
                                speed=line.speed,
                                voice_fx=line.voice_fx,
                                output_path=output_path,
                            )
                            retried += 1
                        except Exception:
                            still_failed.append(line)
                            continue

                    # Re-validate retried segments
                    self.engine.unload()
                    self.whisper.load()

                    new_still_failed: list[ScriptLine] = []
                    for line in failed_lines:
                        if line in still_failed:
                            new_still_failed.append(line)
                            continue

                        audio_path = segments_dir / f"{line.line_id}.wav"
                        result = self._validate_segment(
                            str(audio_path), line.text, line.line_id, line.speed, attempt
                        )

                        # Update quality result
                        for j, qr in enumerate(quality_results):
                            if qr.line_id == line.line_id:
                                quality_results[j] = result
                                break

                        if result.status == ValidationStatus.FAIL:
                            new_still_failed.append(line)

                    failed_lines = new_still_failed

                    if failed_lines:
                        # Swap back for next retry
                        self.whisper.unload()
                        self.engine.load()

                # Ensure TTS is loaded at the end
                if not self.engine.is_loaded:
                    self.whisper.unload()
                    self.engine.load()

            else:
                # Swap back to TTS
                self.whisper.unload()
                self.engine.load()

        # Build quality report
        final_failed = sum(
            1 for r in quality_results if r.status == ValidationStatus.FAIL
        )

        wer_values = [r.wer for r in quality_results if r.wer >= 0]
        avg_wer = np.mean(wer_values) if wer_values else 0.0
        worst_wer = max(wer_values) if wer_values else 0.0

        quality_report = ChapterQualityReport(
            chapter_number=chapter_number,
            total_segments=total_lines,
            passed=sum(1 for r in quality_results if r.status == ValidationStatus.PASS),
            failed=final_failed,
            flagged=len(flagged_lines),
            total_retries=retried,
            average_wer=float(avg_wer),
            worst_wer=float(worst_wer),
            flagged_lines=flagged_lines,
        )

        return GenerateChapterResponse(
            status="success",
            chapter_number=chapter_number,
            total_lines=total_lines,
            generated=generated,
            failed_validation=final_failed,
            retried=retried,
            total_duration_seconds=total_duration,
            quality_report=quality_report,
            segment_files_dir=str(segments_dir),
        )

    def validate_single(
        self,
        audio_file: str,
        expected_text: str,
    ) -> QualityResult:
        """Validate a single audio segment (standalone endpoint)."""
        if not self.whisper.is_loaded:
            self.whisper.load()

        return self._validate_segment(audio_file, expected_text, "manual", 1.0)

    def _validate_segment(
        self,
        audio_file: str,
        expected_text: str,
        line_id: str,
        speed: float,
        attempt: int = 1,
    ) -> QualityResult:
        """Run all validation checks on a single segment."""
        # STT + WER check
        transcribed = self.whisper.transcribe(audio_file)
        wer = self.whisper.calculate_wer(expected_text, transcribed)

        # Audio quality analysis
        analysis = self.analyzer.analyze(audio_file, expected_text, speed)

        # Composite quality score
        quality_score = (
            (1 - wer) * QUALITY_WEIGHT_WER
            + analysis["artifact_score"] * QUALITY_WEIGHT_ARTIFACT
            + analysis["duration_score"] * QUALITY_WEIGHT_DURATION
        )

        # Determine status
        if wer > self.wer_threshold:
            status = ValidationStatus.FAIL
        elif quality_score < QUALITY_SCORE_PASS_THRESHOLD:
            status = ValidationStatus.FLAGGED
        else:
            status = ValidationStatus.PASS

        logger.info(
            "[Validator] Line %s (attempt %d): status=%s, WER=%.3f (threshold=%.2f), quality_score=%.2f\n  Expected:    \"%s\"\n  Transcribed: \"%s\"",
            line_id,
            attempt,
            status,
            wer,
            self.wer_threshold,
            quality_score,
            expected_text[:80] + ("..." if len(expected_text) > 80 else ""),
            transcribed[:80] + ("..." if len(transcribed) > 80 else ""),
        )

        return QualityResult(
            line_id=line_id,
            status=status,
            wer=wer,
            transcribed_text=transcribed,
            duration_seconds=analysis["duration_seconds"],
            expected_duration_seconds=analysis["expected_duration_seconds"],
            peak_dbfs=analysis["peak_dbfs"],
            noise_floor_db=analysis["noise_floor_db"],
            clipping_detected=analysis["clipping_detected"],
            quality_score=quality_score,
            attempt=attempt,
        )

    @staticmethod
    def _send_progress(
        ws_connections: list,
        project_id: str,
        chapter: int,
        line_id: str,
        progress: int,
        total: int,
        speaker: str,
        emotion: str,
    ) -> None:
        """Send progress update to all connected WebSocket clients."""
        message = json.dumps({
            "type": "progress",
            "project_id": project_id,
            "chapter": chapter,
            "line_id": line_id,
            "progress": progress,
            "total": total,
            "percent": round(progress / total * 100, 1) if total > 0 else 0,
            "current_speaker": speaker,
            "current_emotion": emotion,
        })

        for ws in ws_connections:
            try:
                asyncio.get_event_loop().create_task(ws.send_text(message))
            except Exception:
                pass
