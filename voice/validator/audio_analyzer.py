"""Audio Analyzer — Signal-level quality checks on generated audio.

Detects:
  - Clipping (peak > threshold)
  - Excessive noise floor
  - Unnatural silence gaps
  - Duration anomalies
"""

from __future__ import annotations

import logging

import numpy as np
import soundfile as sf

from shared.constants import AVERAGE_WORDS_PER_MINUTE

logger = logging.getLogger(__name__)


class AudioAnalyzer:
    """Analyze audio segments for quality issues."""

    def __init__(
        self,
        noise_threshold: float = -50.0,
        clipping_threshold: float = -0.5,
        max_silence_seconds: float = 3.0,
        duration_tolerance: float = 0.3,
    ):
        self.noise_threshold = noise_threshold
        self.clipping_threshold = clipping_threshold
        self.max_silence_seconds = max_silence_seconds
        self.duration_tolerance = duration_tolerance

    def analyze(
        self,
        audio_file: str,
        expected_text: str = "",
        speed: float = 1.0,
    ) -> dict:
        """Run all audio quality checks on a segment.

        Args:
            audio_file: Path to the .wav file.
            expected_text: The text that was spoken (for duration check).
            speed: The speed parameter used during generation.

        Returns:
            Dict with analysis results.
        """
        audio, sample_rate = sf.read(audio_file)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)  # Convert to mono

        duration = len(audio) / sample_rate

        # Peak / clipping check
        peak = np.max(np.abs(audio))
        peak_dbfs = 20 * np.log10(peak) if peak > 0 else -100
        clipping_detected = peak_dbfs > self.clipping_threshold

        # Noise floor check
        noise_floor_db = self._measure_noise_floor(audio, sample_rate)

        # Silence gap check
        has_long_silence = self._check_silence_gaps(audio, sample_rate)

        # Duration sanity check
        expected_duration = self._expected_duration(expected_text, speed)
        duration_ok = True
        duration_deviation = 0.0
        if expected_duration > 0:
            duration_deviation = abs(duration - expected_duration) / expected_duration
            duration_ok = duration_deviation <= self.duration_tolerance

        # RMS loudness measurement & CPS pacing check
        rms = np.sqrt(np.mean(audio**2)) if len(audio) > 0 else 0
        rms_dbfs = 20 * np.log10(rms) if rms > 0 else -100.0

        # Characters per second (CPS) check (detects trailing repetition or swallowed text)
        char_count = len(expected_text.strip())
        cps = char_count / duration if duration > 0 and char_count > 0 else 0.0
        pacing_anomaly = False
        if char_count > 10 and duration > 0:
            if cps < 4.0:   # Hallucinated long silence/repetition
                pacing_anomaly = True
                logger.warning("[AudioAnalyzer] Slow pacing anomaly detected (%.1f CPS) for file: %s", cps, audio_file)
            elif cps > 32.0: # Swallowed/rushed text
                pacing_anomaly = True
                logger.warning("[AudioAnalyzer] Fast pacing anomaly detected (%.1f CPS) for file: %s", cps, audio_file)

        # Artifact score (1.0 = perfect, reduced for each issue)
        artifact_score = 1.0
        if clipping_detected:
            artifact_score -= 0.3
        if noise_floor_db > self.noise_threshold:
            artifact_score -= 0.2
        if has_long_silence:
            artifact_score -= 0.2
        if pacing_anomaly:
            artifact_score -= 0.3
        artifact_score = max(0.0, artifact_score)

        # Duration score
        duration_score = max(0.0, 1.0 - duration_deviation) if expected_duration > 0 else 1.0

        return {
            "duration_seconds": duration,
            "expected_duration_seconds": expected_duration,
            "duration_deviation": duration_deviation,
            "duration_ok": duration_ok and not pacing_anomaly,
            "peak_dbfs": peak_dbfs,
            "rms_dbfs": rms_dbfs,
            "clipping_detected": clipping_detected,
            "noise_floor_db": noise_floor_db,
            "has_long_silence": has_long_silence,
            "pacing_anomaly": pacing_anomaly,
            "cps": cps,
            "artifact_score": artifact_score,
            "duration_score": duration_score,
            "sample_rate": sample_rate,
        }

    @staticmethod
    def _measure_noise_floor(audio: np.ndarray, sample_rate: int) -> float:
        """Measure the noise floor by analyzing the quietest segments."""
        # Split audio into short frames
        frame_size = int(sample_rate * 0.05)  # 50ms frames
        frames = [
            audio[i : i + frame_size]
            for i in range(0, len(audio) - frame_size, frame_size)
        ]

        if not frames:
            return -100.0

        # Calculate RMS for each frame
        rms_values = []
        for frame in frames:
            rms = np.sqrt(np.mean(frame ** 2))
            if rms > 0:
                rms_db = 20 * np.log10(rms)
                rms_values.append(rms_db)

        if not rms_values:
            return -100.0

        # Noise floor = average of the quietest 10% of frames
        rms_values.sort()
        n_quiet = max(1, len(rms_values) // 10)
        noise_floor = np.mean(rms_values[:n_quiet])

        return float(noise_floor)

    def _check_silence_gaps(self, audio: np.ndarray, sample_rate: int) -> bool:
        """Check for unnatural silence gaps within the audio."""
        # Threshold for "silence" (-40 dBFS)
        silence_threshold = 10 ** (-40 / 20)

        # Find consecutive silent samples
        is_silent = np.abs(audio) < silence_threshold
        max_silence_samples = int(self.max_silence_seconds * sample_rate)

        # Find runs of silence
        current_run = 0
        for is_s in is_silent:
            if is_s:
                current_run += 1
                if current_run > max_silence_samples:
                    return True
            else:
                current_run = 0

        return False

    @staticmethod
    def _expected_duration(text: str, speed: float) -> float:
        """Calculate expected duration based on word count and speed."""
        if not text:
            return 0.0

        word_count = len(text.split())
        wpm = AVERAGE_WORDS_PER_MINUTE * speed
        expected_seconds = (word_count / wpm) * 60

        return expected_seconds
