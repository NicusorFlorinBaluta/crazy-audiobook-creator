"""Tests for syllable-aware expected duration in AudioAnalyzer (Spec F20)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from shared.constants import AVERAGE_WORDS_PER_MINUTE
from voice.validator.audio_analyzer import AudioAnalyzer


def test_menzoberranzan_expects_materially_more_than_word_based():
    """'Menzoberranzan' (1 word, 5 syllables) must expect materially more than 1 / WPM * 60."""
    word_based = (1.0 / AVERAGE_WORDS_PER_MINUTE) * 60  # 0.40s
    actual_expected = AudioAnalyzer._expected_duration("Menzoberranzan", speed=1.0)
    # Expected is (5 / 225) * 60 = 1.33s
    assert actual_expected >= 1.2
    assert actual_expected >= word_based * 2.5


def test_bedorijay_fumed_passes_duration_check(tmp_path: Path):
    """'Bedorijay fumed.' at 2.08s actual duration was previously failing by 0.03s (delta 1.28s > 1.25s).
    With syllable estimation it expects ~1.33s, giving a delta of 0.75s <= 1.25s, so it passes.
    """
    analyzer = AudioAnalyzer()
    expected_dur = AudioAnalyzer._expected_duration("Bedorijay fumed.", speed=1.0)
    allowed_delta = max(1.25, expected_dur * analyzer.duration_tolerance)

    actual_duration = 2.08
    assert abs(actual_duration - expected_dur) <= allowed_delta

    # Also verify through analyze() with a synthetic 2.08s wav
    sr = 24000
    num_samples = int(actual_duration * sr)
    audio = np.zeros(num_samples, dtype=np.float32)
    # Add quiet tone so it is not pure silence
    audio[:1000] = 0.01

    wav_path = tmp_path / "bedorijay.wav"
    sf.write(str(wav_path), audio, sr)

    metrics = analyzer.analyze(str(wav_path), expected_text="Bedorijay fumed.")
    assert metrics["duration_ok"] is True


def test_swallowed_text_fails_cps_check(tmp_path: Path):
    """A genuinely swallowed line (e.g. 50 characters in 0.5s -> 100 CPS) is caught by the CPS check."""
    analyzer = AudioAnalyzer()
    long_text = "This is a very long sentence that was completely swallowed by the synthesizer in half a second."
    sr = 24000
    duration = 0.5
    num_samples = int(duration * sr)
    audio = np.full(num_samples, 0.01, dtype=np.float32)

    wav_path = tmp_path / "swallowed.wav"
    sf.write(str(wav_path), audio, sr)

    metrics = analyzer.analyze(str(wav_path), expected_text=long_text)
    assert metrics["pacing_anomaly"] is True
    assert metrics["duration_ok"] is False


def test_fallback_to_word_count_when_no_syllables():
    """Numbers or words without vowel groups fall back to word count calculation."""
    # Text with digits only
    text = "123 456"
    expected = AudioAnalyzer._expected_duration(text, speed=1.0)
    assert expected > 0.0
    expected_word_based = (2.0 / AVERAGE_WORDS_PER_MINUTE) * 60
    assert abs(expected - expected_word_based) < 1e-5
