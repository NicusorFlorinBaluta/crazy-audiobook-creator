"""Audio Analyzer — Signal-level quality checks on generated audio.

Detects:
  - Clipping (peak > threshold)
  - Excessive noise floor
  - Unnatural silence gaps
  - Duration anomalies
"""

from __future__ import annotations

import logging
import re

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
            # Short utterances have a fixed onset/release cost, so a purely
            # relative tolerance rejects healthy one- and two-word clips.
            # The CPS guard below still catches swallowed or repeated speech.
            allowed_delta = max(
                1.25,
                expected_duration * self.duration_tolerance,
            )
            duration_ok = abs(duration - expected_duration) <= allowed_delta

        # RMS loudness measurement & CPS pacing check
        rms = np.sqrt(np.mean(audio**2)) if len(audio) > 0 else 0
        rms_dbfs = 20 * np.log10(rms) if rms > 0 else -100.0

        # Pitch median measurement
        pitch_median = self._measure_pitch(audio, sample_rate)

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
        # A clip inside the accepted timing envelope should not subsequently
        # fail the aggregate score for the same duration measurement.
        duration_score = (
            1.0
            if expected_duration <= 0 or duration_ok
            else max(0.0, 1.0 - duration_deviation)
        )

        return {
            # These metrics are embedded in QualityResult.metrics (dict[str,
            # Any]), where Pydantic intentionally does not coerce NumPy scalar
            # types. Keep the analyzer boundary JSON-native so a completed
            # chapter cannot fail while serializing its response.
            "duration_seconds": float(duration),
            "expected_duration_seconds": float(expected_duration),
            "duration_deviation": float(duration_deviation),
            "duration_ok": bool(duration_ok and not pacing_anomaly),
            "peak_dbfs": float(peak_dbfs),
            "rms_dbfs": float(rms_dbfs),
            "clipping_detected": bool(clipping_detected),
            "noise_floor_db": float(noise_floor_db),
            "has_long_silence": bool(has_long_silence),
            "pacing_anomaly": bool(pacing_anomaly),
            "pitch_median": float(pitch_median),
            "cps": float(cps),
            "artifact_score": float(artifact_score),
            "duration_score": float(duration_score),
            "sample_rate": int(sample_rate),
        }

    @staticmethod
    def _measure_pitch(audio: np.ndarray, sample_rate: int) -> float:
        """Measure median pitch (F0) using autocorrelation."""
        if len(audio) == 0:
            return 0.0
            
        frame_size = int(sample_rate * 0.05)  # 50ms frames
        if len(audio) < frame_size:
            return 0.0
            
        pitches = []
        # Search range for 50Hz to 400Hz
        min_lag = int(sample_rate / 400.0)
        max_lag = int(sample_rate / 50.0)
        
        for i in range(0, len(audio) - frame_size, frame_size):
            frame = audio[i:i + frame_size]
            # Autocorrelation
            result = np.correlate(frame, frame, mode='full')
            result = result[len(result) // 2:]
            
            if max_lag < len(result):
                peak_idx = min_lag + np.argmax(result[min_lag:max_lag])
                if result[peak_idx] > 0.2 * result[0]:  # Threshold for voiced frame
                    pitch = sample_rate / peak_idx
                    pitches.append(pitch)
                    
        if not pitches:
            return 0.0
            
        return float(np.median(pitches))

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

        # Treat punctuation-separated words as distinct spoken words. A plain
        # whitespace split undercounts prose such as "white-faintly" and
        # "Starling-finally-felt", producing false slow-duration failures.
        word_count = len(
            re.findall(r"[^\W_]+(?:['’][^\W_]+)*|\d+", text, re.UNICODE)
        )
        wpm = AVERAGE_WORDS_PER_MINUTE * speed
        expected_seconds = (word_count / wpm) * 60

        return expected_seconds
