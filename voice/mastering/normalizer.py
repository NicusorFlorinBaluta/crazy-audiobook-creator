"""Loudness Normalizer — LUFS normalization and peak limiting.

Implements audiobook-standard loudness normalization:
  - Integrated LUFS measurement
  - Target LUFS adjustment (-19 LUFS default)
  - Oversampled peak ceiling
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
        peak_ceiling_mode: str = "global",
    ):
        self.target_lufs = target_lufs
        self.peak_limit_dbfs = peak_limit_dbfs
        self.output_sample_rate = output_sample_rate
        self.output_bit_depth = output_bit_depth
        self.noise_gate_enabled = noise_gate_enabled
        self.noise_gate_threshold = noise_gate_threshold
        self.noise_gate_attack_ms = noise_gate_attack_ms
        self.noise_gate_release_ms = noise_gate_release_ms
        if peak_ceiling_mode not in {"global", "soft_limiter"}:
            raise ValueError("peak_ceiling_mode must be 'global' or 'soft_limiter'")
        self.peak_ceiling_mode = peak_ceiling_mode

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
        3. Resample to output rate
        4. Apply an oversampled peak ceiling at the final sample rate
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

        # Step 3: Resample before the final peak check because conversion can
        # create new inter-sample peaks.
        if sample_rate != self.output_sample_rate:
            audio = self._resample(audio, sample_rate, self.output_sample_rate)
            sample_rate = self.output_sample_rate

        # Step 4: Transparent peak ceiling. Final loudness is measured again
        # because peak-constrained material may finish below the LUFS target.
        pre_ceiling_audio = audio
        audio = self._apply_peak_ceiling(audio)
        peak_limit = 10 ** (self.peak_limit_dbfs / 20)
        knee = peak_limit * 0.80
        limited_sample_fraction = float(np.mean(np.abs(pre_ceiling_audio) > knee)) if pre_ceiling_audio.size else 0.0

        # Final measurements
        final_lufs = self._measure_lufs(audio, sample_rate)
        peak = self._measure_true_peak(audio)
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
            "peak_ceiling_mode": self.peak_ceiling_mode,
            "limited_sample_fraction": limited_sample_fraction,
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
            rms = float(np.sqrt(np.mean(audio**2)))
            if rms > 0:
                lufs = float(20 * np.log10(rms) - 0.691)  # Rough LUFS approximation
                if math.isinf(lufs) or math.isnan(lufs):
                    return -70.0
                return lufs
            return -70.0
        except Exception as e:
            logger.warning("LUFS measurement failed: %s", e)
            return -70.0

    def _apply_peak_ceiling(self, audio: np.ndarray) -> np.ndarray:
        """Apply the configured transparent peak ceiling."""
        peak_limit = 10 ** (self.peak_limit_dbfs / 20)
        if self.peak_ceiling_mode == "soft_limiter":
            audio = self._apply_soft_limiter(audio, peak_limit)
        peak = self._measure_true_peak(audio)

        if peak > peak_limit:
            ratio = peak_limit / peak
            audio = audio * ratio
            logger.debug("Peak ceiling applied: %.2f -> %.2f", peak, peak_limit)

        return audio

    @staticmethod
    def _apply_soft_limiter(
        audio: np.ndarray,
        peak_limit: float,
        knee_ratio: float = 0.80,
    ) -> np.ndarray:
        """Limit only peak samples with a continuous, unity-slope soft knee."""
        if audio.size == 0:
            return audio
        knee = peak_limit * knee_ratio
        headroom = max(peak_limit - knee, 1e-9)
        magnitude = np.abs(audio)
        limited = audio.copy()
        above = magnitude > knee
        if np.any(above):
            compressed = knee + headroom * (1.0 - np.exp(-(magnitude[above] - knee) / headroom))
            limited[above] = np.sign(audio[above]) * compressed
        return limited

    def _apply_noise_gate(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """Apply a noise gate to clean up silence portions (vectorized)."""
        threshold = 10 ** (self.noise_gate_threshold / 20)
        attack_samples = max(1, int(sample_rate * self.noise_gate_attack_ms / 1000))
        release_samples = max(1, int(sample_rate * self.noise_gate_release_ms / 1000))

        # Calculate envelope using fast moving average of squared signal
        window_size = max(1, int(sample_rate * 0.01))  # 10ms windows
        kernel = np.ones(window_size) / window_size
        squared_env = np.convolve(audio**2, kernel, mode="same")
        envelope = np.sqrt(np.maximum(0, squared_env))

        # Gate binary mask
        gate = (envelope > threshold).astype(np.float64)

        # Smooth gate with 1-pole exponential filter
        alpha_attack = 1.0 - np.exp(-1.0 / attack_samples)
        alpha_release = 1.0 - np.exp(-1.0 / release_samples)

        try:
            from scipy.signal import lfilter

            attack = lfilter(
                [alpha_attack],
                [1.0, -(1.0 - alpha_attack)],
                gate,
            )
            release = lfilter(
                [alpha_release],
                [1.0, -(1.0 - alpha_release)],
                gate[::-1],
            )[::-1]
            smoothed = np.clip(np.maximum(attack, release), 0.0, 1.0)
        except ImportError:
            smoothed = np.zeros_like(gate)
            curr = 0.0
            rates = np.where(gate > 0.5, alpha_attack, alpha_release)
            for i in range(len(gate)):
                curr += rates[i] * (gate[i] - curr)
                smoothed[i] = curr

        return audio * smoothed

    @staticmethod
    def _measure_true_peak(audio: np.ndarray) -> float:
        """Estimate inter-sample peak with 4x polyphase oversampling."""
        if len(audio) == 0:
            return 0.0
        try:
            from scipy.signal import resample_poly

            oversampled = resample_poly(audio, 4, 1)
            return float(np.max(np.abs(oversampled)))
        except ImportError:
            return float(np.max(np.abs(audio)))

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
