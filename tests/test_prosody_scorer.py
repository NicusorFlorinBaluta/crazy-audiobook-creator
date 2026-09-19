import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from brain.orchestrator.quality_trends import _is_monotone_row
from voice.validator.prosody_scorer import ProsodyScorer


class ProsodyScorerTests(unittest.TestCase):
    """F12 -- Pinning tests for monotone detection recalibration.

    Covers:
    - Synthetic segment with low pitch variance and normal dynamic range produces a monotone warning;
      one with normal pitch variance does not.
    - Negative: replay this book's stored per-segment metrics through the new rule and assert
      the warning count is neither 0 nor anywhere near 7,928.
    - Dynamic range threshold default is 5.29, acting as corroboration rather than a veto.
    """

    def test_low_pitch_variance_with_normal_dynamic_range_flags_monotone(self) -> None:
        """A flat pitch with normal speech dynamic range must trigger monotone warning."""
        scorer = ProsodyScorer(sample_rate=24000, pitch_cv_threshold=0.06, dynamic_range_threshold=5.29)

        # 1. Monotone audio: pure sine wave at 150 Hz with moderate amplitude modulation (normal dynamic range)
        sr = 24000
        duration = 2.0
        t = np.linspace(0, duration, int(sr * duration), endpoint=False)
        # 150 Hz steady pitch (pitch_cv will be ~0)
        carrier = np.sin(2 * np.pi * 150.0 * t)
        # Modulate amplitude so peak / mean_rms > 5.29 (normal dynamic range)
        envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 2.0 * t)
        audio_flat = (carrier * envelope * 0.8).astype(np.float32)

        with tempfile.TemporaryDirectory() as directory:
            path_flat = Path(directory) / "flat.wav"
            sf.write(str(path_flat), audio_flat, sr)

            result_flat = scorer.analyze(path_flat, "Flat text")
            self.assertTrue(
                result_flat.get("monotone_warning"),
                f"Expected monotone_warning=True for flat pitch; got {result_flat}",
            )
            self.assertLess(result_flat.get("pitch_cv", 1.0), 0.06)

        # 2. Expressive audio: frequency sweeping across an octave (100 Hz to 200 Hz)
        f_instant = 100.0 + 100.0 * np.sin(2 * np.pi * 1.5 * t)
        phase = 2 * np.pi * np.cumsum(f_instant) / sr
        carrier_expressive = np.sin(phase)
        audio_expressive = (carrier_expressive * envelope * 0.8).astype(np.float32)

        with tempfile.TemporaryDirectory() as directory:
            path_expressive = Path(directory) / "expressive.wav"
            sf.write(str(path_expressive), audio_expressive, sr)

            result_expressive = scorer.analyze(path_expressive, "Expressive text")
            self.assertFalse(
                result_expressive.get("monotone_warning"),
                f"Expected monotone_warning=False for expressive pitch; got {result_expressive}",
            )
            self.assertGreater(result_expressive.get("pitch_cv", 0.0), 0.06)

    def test_negative_replay_stored_metrics_neither_zero_nor_all(self) -> None:
        """Replay stored per-segment metrics: warning count must be > 0 and << total segments."""
        db_path = Path("brain/projects/pipeline_state.db")
        if not db_path.exists():
            self.skipTest("brain/projects/pipeline_state.db not found")

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT details FROM quality_logs WHERE project_id=?",
            ("the-finest-edge-of-twilight-book",),
        )
        raw_rows = [json.loads(r[0]) for r in cursor.fetchall() if r[0]]
        conn.close()

        if not raw_rows:
            self.skipTest("No quality_logs found for the-finest-edge-of-twilight-book")

        total = len(raw_rows)
        self.assertGreaterEqual(total, 7000, f"Expected ~7900 rows; found {total}")

        monotone_count = sum(1 for r in raw_rows if _is_monotone_row(r, pitch_cv_thresh=0.06, dr_thresh=5.29))

        # Must NOT be 0 (the original defect where conjunction was constant False)
        self.assertGreater(
            monotone_count,
            0,
            "Recalibrated monotone rule must not produce 0 warnings on this corpus",
        )
        # Must NOT be anywhere near total (e.g. less than 15% of total segments)
        self.assertLess(
            monotone_count,
            int(total * 0.15),
            f"Monotone detector fired on {monotone_count}/{total} segments; too aggressive",
        )

    def test_borderline_pitch_corroborated_by_low_dynamic_range(self) -> None:
        """Dynamic range < 5.29 corroborates borderline pitch_cv (< 0.09)."""
        scorer = ProsodyScorer(pitch_cv_threshold=0.06, dynamic_range_threshold=5.29)

        # Mock direct analysis values:
        # 1. pitch_cv = 0.07 (above 0.06, but below 0.09) and dynamic_range = 4.5 (< 5.29)
        # This is borderline pitch corroborated by flat dynamic range -> True
        row_corroborated = {
            "duration_seconds": 2.0,
            "pitch_cv": 0.07,
            "peak_dbfs": -10.0,
            "metrics": {"rms_dbfs": -23.0},  # dr = 10^(13/20) = 4.46 < 5.29
        }
        self.assertTrue(_is_monotone_row(row_corroborated, scorer.pitch_cv_threshold, scorer.dynamic_range_threshold))

        # 2. pitch_cv = 0.07 and dynamic_range = 8.0 (> 5.29)
        # Borderline pitch NOT corroborated -> False
        row_unflagged = {
            "duration_seconds": 2.0,
            "pitch_cv": 0.07,
            "peak_dbfs": -5.0,
            "metrics": {"rms_dbfs": -23.0},  # dr = 10^(18/20) = 7.94 > 5.29
        }
        self.assertFalse(_is_monotone_row(row_unflagged, scorer.pitch_cv_threshold, scorer.dynamic_range_threshold))


if __name__ == "__main__":
    unittest.main()
