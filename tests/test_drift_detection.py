from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from voice.validator.audio_analyzer import AudioAnalyzer


def _write_tone(path: Path, *, frequency: float, amplitude: float) -> None:
    sample_rate = 24_000
    time = np.arange(sample_rate * 2, dtype=np.float32) / sample_rate
    audio = amplitude * np.sin(2 * np.pi * frequency * time)
    sf.write(path, audio.astype(np.float32), sample_rate)


def test_drift_and_join_signals_use_self_contained_audio(tmp_path: Path) -> None:
    """Detect pitch drift and a quiet join without a machine-local fixture."""
    baseline_file = tmp_path / "baseline.wav"
    shifted_file = tmp_path / "shifted.wav"
    _write_tone(baseline_file, frequency=220.0, amplitude=0.1)
    _write_tone(shifted_file, frequency=110.0, amplitude=0.001)

    analyzer = AudioAnalyzer()
    expected = "She walked through the moonlit garden"
    baseline = analyzer.analyze(str(baseline_file), expected, speed=1.0)
    shifted = analyzer.analyze(str(shifted_file), expected, speed=1.0)

    baseline_pitch = float(baseline.get("pitch_median", 0.0))
    shifted_pitch = float(shifted.get("pitch_median", 0.0))
    assert baseline_pitch > 0
    assert shifted_pitch > 0
    assert abs(shifted_pitch - baseline_pitch) / baseline_pitch > 0.30
    assert float(shifted.get("rms_dbfs", 0.0)) < -30.0
