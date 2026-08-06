"""Audio Assembler — Concatenate segments into chapter-length audio.

Handles:
  - Segment concatenation in order
  - Configurable silence insertion between segments
  - Cross-fading between adjacent segments
  - Chapter start/end silence
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)


class AudioAssembler:
    """Assemble individual audio segments into a chapter."""

    def __init__(
        self,
        crossfade_ms: int = 50,
        sample_rate: int = 24000,
        chapter_start_silence_ms: int = 1000,
        chapter_end_silence_ms: int = 2000,
    ):
        self.crossfade_ms = crossfade_ms
        self.sample_rate = sample_rate
        self.chapter_start_silence_ms = chapter_start_silence_ms
        self.chapter_end_silence_ms = chapter_end_silence_ms

    def assemble_chapter(
        self,
        segments: list,
        workspace: Path,
        announcement_audio: np.ndarray | None = None,
    ) -> dict[str, Any]:
        """Assemble multiple segments into a single chapter audio.

        Args:
            segments: List of MasterSegmentInfo objects with file paths and pause info.
            workspace: Base workspace directory.
            announcement_audio: Optional Narrator chapter announcement audio numpy array.

        Returns:
            Dict with 'audio' (numpy array) and 'sample_rate'.
        """
        if not segments and announcement_audio is None:
            return {
                "audio": np.array([], dtype=np.float32),
                "sample_rate": self.sample_rate,
            }

        logger.info("Assembling %d segments (announcement=%s)...", len(segments), announcement_audio is not None)

        parts: list[np.ndarray] = []
        join_diagnostics: list[dict[str, Any]] = []

        # Chapter start silence (1.0s standard audiobook start)
        parts.append(self._silence(self.chapter_start_silence_ms))

        # Add Narrator Chapter Announcement if provided
        if announcement_audio is not None and len(announcement_audio) > 0:
            ann_audio = announcement_audio.astype(np.float32)
            if announcement_audio.ndim > 1:
                ann_audio = ann_audio.mean(axis=1)
            parts.append(ann_audio)
            # 1.5s pause after chapter announcement before body text
            parts.append(self._silence(1500))

        previous_pause_after = 0
        previous_was_audio = False
        previous_audio: np.ndarray | None = None
        previous_line_id = ""
        for i, segment in enumerate(segments):
            # One timing owner: adjacent pause directives are combined with
            # max(), never added together. Chapter edges are owned here.
            pause_before = getattr(segment, "pause_before_ms", 0)
            gap_before = 0 if i == 0 else max(previous_pause_after, pause_before)
            if gap_before > 0:
                parts.append(self._silence(gap_before))
                previous_was_audio = False

            # Load audio segment
            audio_path = Path(segment.file)
            if not audio_path.is_absolute():
                audio_path = workspace / segment.file

            if not audio_path.exists():
                raise FileNotFoundError(f"Segment file not found: {audio_path}")

            audio, sr = sf.read(str(audio_path))
            if audio.size == 0:
                raise ValueError(f"Segment file is empty: {audio_path}")
            if audio.ndim > 1:
                audio = audio.mean(axis=1)

            # Resample if needed
            if sr != self.sample_rate:
                try:
                    import librosa
                    audio = librosa.resample(audio, orig_sr=sr, target_sr=self.sample_rate)
                except ImportError:
                    logger.warning("librosa not available for resampling")

            audio = audio.astype(np.float32)

            if previous_audio is not None:
                diagnostic = self._join_diagnostic(
                    previous_line_id=previous_line_id,
                    current_line_id=str(getattr(segment, "line_id", i)),
                    previous=previous_audio,
                    current=audio,
                    gap_ms=gap_before,
                )
                join_diagnostics.append(diagnostic)

            # Cross-fade with previous segment
            if (
                self.crossfade_ms > 0
                and previous_was_audio
                and len(parts) > 0
                and len(parts[-1]) > 0
            ):
                audio = self._apply_crossfade(parts[-1], audio)

            parts.append(audio)
            previous_was_audio = True
            previous_pause_after = getattr(segment, "pause_after_ms", 500)
            previous_audio = audio.copy()
            previous_line_id = str(getattr(segment, "line_id", i))

        # The final script pause and chapter outro are alternatives, not
        # cumulative delays.
        final_pause = max(previous_pause_after, self.chapter_end_silence_ms)
        if final_pause > 0:
            parts.append(self._silence(final_pause))

        # Concatenate all parts
        assembled = np.concatenate([p for p in parts if len(p) > 0])
        duration = len(assembled) / self.sample_rate

        logger.info("Chapter assembled: %.1f seconds", duration)

        return {
            "audio": assembled,
            "sample_rate": self.sample_rate,
            "join_diagnostics": join_diagnostics,
            "join_warnings": sum(
                item["status"] == "warning" for item in join_diagnostics
            ),
        }

    @staticmethod
    def _join_diagnostic(
        *,
        previous_line_id: str,
        current_line_id: str,
        previous: np.ndarray,
        current: np.ndarray,
        gap_ms: int,
    ) -> dict[str, Any]:
        """Measure one boundary without changing or rejecting the audio."""
        def rms_dbfs(audio: np.ndarray) -> float:
            if audio.size == 0:
                return -100.0
            rms = float(np.sqrt(np.mean(np.square(audio.astype(np.float64)))))
            return float(20 * np.log10(rms)) if rms > 0 else -100.0

        previous_rms = rms_dbfs(previous)
        current_rms = rms_dbfs(current)
        loudness_delta = abs(previous_rms - current_rms)
        boundary_jump = (
            abs(float(current[0]) - float(previous[-1]))
            if previous.size and current.size and gap_ms == 0
            else 0.0
        )
        reasons: list[str] = []
        if loudness_delta > 8.0:
            reasons.append("segment_loudness_delta")
        if gap_ms == 0 and boundary_jump > 0.15:
            reasons.append("abrupt_zero_gap_boundary")
        return {
            "previous_line_id": previous_line_id,
            "current_line_id": current_line_id,
            "gap_ms": int(gap_ms),
            "previous_rms_dbfs": previous_rms,
            "current_rms_dbfs": current_rms,
            "loudness_delta_db": loudness_delta,
            "zero_gap_sample_jump": boundary_jump,
            "status": "warning" if reasons else "clean",
            "reasons": reasons,
        }

    def _silence(self, ms: int) -> np.ndarray:
        """Generate silence of the given duration."""
        samples = int(self.sample_rate * ms / 1000)
        return np.zeros(samples, dtype=np.float32)

    def _apply_crossfade(
        self,
        prev: np.ndarray,
        current: np.ndarray,
    ) -> np.ndarray:
        """Apply a raised-cosine crossfade between two audio segments.

        Modifies the end of prev and start of current for smooth transition.
        Returns the modified current segment (prev is modified in place).
        """
        fade_samples = int(self.sample_rate * self.crossfade_ms / 1000)
        fade_samples = min(fade_samples, len(prev), len(current))

        if fade_samples < 2:
            return current

        # Raised cosine fade curve
        t = np.linspace(0, np.pi / 2, fade_samples)
        fade_out = np.cos(t).astype(np.float32)
        fade_in = np.sin(t).astype(np.float32)

        # Apply fade-out to end of previous segment
        prev[-fade_samples:] *= fade_out

        # Apply fade-in to start of current segment
        current = current.copy()
        current[:fade_samples] *= fade_in

        # Overlap-add the crossfade region
        prev[-fade_samples:] += current[:fade_samples]

        # Return current without the crossfade region (it's already in prev)
        return current[fade_samples:]
