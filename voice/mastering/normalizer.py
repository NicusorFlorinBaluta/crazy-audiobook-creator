"""Loudness Normalizer — LUFS normalization and peak limiting.

Implements audiobook-standard loudness normalization:
  - Integrated LUFS measurement
  - Target LUFS adjustment (-19 LUFS default)
  - True peak limiting (-1 dBTP)
  - Noise gate
  - Sample rate conversion to 44.1 kHz
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)


class LoudnessNormalizer:
    """Normalize audio loudness to audiobook standards."""

    def __init__(
        self,
        target_lufs: float = -19.0,
        peak_limit_dbfs: float = -1.0,
        output_sample_rate: int = 44100,
        output_bit_depth: int = 16,
        noise_gate_enabled: bool = True,
        noise_gate_threshold: float = -50.0,
        noise_gate_attack_ms: float = 5.0,
        noise_gate_release_ms: float = 50.0,
    ):
        self.target_lufs = target_lufs
        self.peak_limit_dbfs = peak_limit_dbfs
        self.output_sample_rate = output_sample_rate
        self.output_bit_depth = output_bit_depth
        self.noise_gate_enabled = noise_gate_enabled
        self.noise_gate_threshold = noise_gate_threshold
        self.noise_gate_attack_ms = noise_gate_attack_ms
        self.noise_gate_release_ms = noise_gate_release_ms

    def normalize(
        self,
        audio: np.ndarray,
        sample_rate: int,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        """Apply full mastering chain to an audio array.

        Steps:
        1. Noise gate (clean up silence)
        2. LUFS loudness normalization
        3. Peak limiting
        4. Resample to output rate
        5. Save to file

        Args:
            audio: Input audio samples.
            sample_rate: Input sample rate.
            output_path: Path to save the normalized audio.

        Returns:
            Dict with loudness metrics and file info.
        """
        audio = audio.astype(np.float64)

        # Step 1: Noise gate
        if self.noise_gate_enabled:
            audio = self._apply_noise_gate(audio, sample_rate)

        # Step 2: LUFS normalization
        current_lufs = self._measure_lufs(audio, sample_rate)
        if current_lufs > -70:  # Only normalize if not silence
            gain_db = self.target_lufs - current_lufs
            gain_linear = 10 ** (gain_db / 20)
            audio = audio * gain_linear
            logger.debug(
                "LUFS normalization: %.1f → %.1f LUFS (gain: %.1f dB)",
                current_lufs,
                self.target_lufs,
                gain_db,
            )

        # Step 3: Peak limiting
        audio = self._apply_peak_limiter(audio)

        # Step 4: Resample if needed
        if sample_rate != self.output_sample_rate:
            audio = self._resample(audio, sample_rate, self.output_sample_rate)
            sample_rate = self.output_sample_rate

        # Final measurements
        final_lufs = self._measure_lufs(audio, sample_rate)
        peak = float(np.max(np.abs(audio)))
        peak_dbfs = float(20 * np.log10(peak)) if peak > 0 else -100.0

        # Convert to output format
        audio = audio.astype(np.float32)
        duration = len(audio) / sample_rate

        # Save
        if output_path:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)

            # Write as WAV with specified bit depth
            subtype = f"PCM_{self.output_bit_depth}"
            sf.write(
                str(output_path),
                audio,
                sample_rate,
                subtype=subtype,
            )
            logger.info(
                "Mastered audio saved: %s (%.1fs, %.1f LUFS, %.1f dBFS peak)",
                output_path.name,
                duration,
                final_lufs,
                peak_dbfs,
            )

        return {
            "duration_seconds": duration,
            "lufs": final_lufs,
            "peak_dbfs": peak_dbfs,
            "sample_rate": sample_rate,
        }

    def _measure_lufs(self, audio: np.ndarray, sample_rate: int) -> float:
        """Measure integrated loudness in LUFS."""
        import math
        try:
            import pyloudnorm
            meter = pyloudnorm.Meter(sample_rate)
            # pyloudnorm expects at least 0.4 seconds
            if len(audio) / sample_rate < 0.4:
                return -70.0
            lufs = float(meter.integrated_loudness(audio))
            if math.isinf(lufs) or math.isnan(lufs):
                return -70.0
            return lufs
        except ImportError:
            logger.warning("pyloudnorm not available — using RMS approximation")
            rms = float(np.sqrt(np.mean(audio ** 2)))
            if rms > 0:
                lufs = float(20 * np.log10(rms) - 0.691)  # Rough LUFS approximation
                if math.isinf(lufs) or math.isnan(lufs):
                    return -70.0
                return lufs
            return -70.0
        except Exception as e:
            logger.warning("LUFS measurement failed: %s", e)
            return -70.0

    def _apply_peak_limiter(self, audio: np.ndarray) -> np.ndarray:
        """Apply a simple peak limiter to prevent clipping."""
        peak_limit = 10 ** (self.peak_limit_dbfs / 20)
        peak = np.max(np.abs(audio))

        if peak > peak_limit:
            ratio = peak_limit / peak
            audio = audio * ratio
            logger.debug("Peak limited: %.2f → %.2f", peak, peak_limit)

        return audio

    def _apply_noise_gate(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """Apply a noise gate to clean up silence portions."""
        threshold = 10 ** (self.noise_gate_threshold / 20)
        attack_samples = int(sample_rate * self.noise_gate_attack_ms / 1000)
        release_samples = int(sample_rate * self.noise_gate_release_ms / 1000)

        # Calculate envelope (RMS in short windows)
        window_size = max(1, int(sample_rate * 0.01))  # 10ms windows
        envelope = np.zeros_like(audio)

        for i in range(0, len(audio) - window_size, window_size):
            rms = np.sqrt(np.mean(audio[i : i + window_size] ** 2))
            envelope[i : i + window_size] = rms

        # Gate: where envelope is below threshold, apply attenuation
        gate = np.where(envelope > threshold, 1.0, 0.0)

        # Smooth the gate with attack/release
        smoothed = np.zeros_like(gate)
        current = 0.0
        for i in range(len(gate)):
            if gate[i] > current:
                # Attack (opening)
                rate = 1.0 / max(1, attack_samples)
                current = min(1.0, current + rate)
            else:
                # Release (closing)
                rate = 1.0 / max(1, release_samples)
                current = max(0.0, current - rate)
            smoothed[i] = current

        return audio * smoothed

    @staticmethod
    def _resample(audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
        """Resample audio to a new sample rate."""
        try:
            import librosa
            return librosa.resample(audio, orig_sr=orig_sr, target_sr=target_sr)
        except ImportError:
            # Simple linear interpolation fallback
            ratio = target_sr / orig_sr
            new_length = int(len(audio) * ratio)
            indices = np.linspace(0, len(audio) - 1, new_length)
            return np.interp(indices, np.arange(len(audio)), audio)
