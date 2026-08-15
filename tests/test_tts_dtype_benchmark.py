import unittest

from scripts.benchmark_tts_dtype import _mode_summary, _session_order


class TTSDtypeBenchmarkTests(unittest.TestCase):
    def test_session_order_is_balanced_and_requires_two_sessions(self) -> None:
        self.assertEqual(_session_order(2, "ABBA"), list("ABBA"))
        self.assertEqual(_session_order(2, "BAAB"), list("BAAB"))
        with self.assertRaisesRegex(ValueError, "at least two"):
            _session_order(1, "ABBA")

    def test_mode_summary_includes_load_and_autoregressive_totals(self) -> None:
        runs = [
            {
                "realtime_factor": 1.0,
                "wall_seconds": 4.0,
                "speaker_similarity": 0.9,
                "wer": 0.0,
                "engine_metrics": {"autoregressive_generation_seconds": 3.5},
            },
            {
                "realtime_factor": 2.0,
                "wall_seconds": 6.0,
                "speaker_similarity": 0.8,
                "wer": 0.1,
                "engine_metrics": {"autoregressive_generation_seconds": 5.5},
            },
        ]
        summary = _mode_summary(runs, [10.0, 12.0])
        self.assertEqual(summary["sessions"], 2)
        self.assertEqual(summary["load_seconds_p50"], 11.0)
        self.assertEqual(summary["total_measured_wall_seconds"], 10.0)
        self.assertEqual(summary["autoregressive_seconds"], 9.0)


if __name__ == "__main__":
    unittest.main()
