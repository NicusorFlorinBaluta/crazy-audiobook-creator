from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from shared.config_validation import validate_brain_config, validate_voice_config
from shared.performance import read_metrics, summarize_metrics
from shared.progress import ProgressEstimator
from shared.reference_selection import reference_line_score, select_reference_text
from shared.logging_utils import rotate_file


ROOT = Path(__file__).resolve().parents[1]


class ConfigurationValidationTests(unittest.TestCase):
    def test_checked_in_configs_validate_without_constructing_models(self) -> None:
        for path, validator in (
            (ROOT / "brain" / "config.yaml", validate_brain_config),
            (ROOT / "voice" / "config.yaml", validate_voice_config),
        ):
            with self.subTest(path=path.name):
                validator(yaml.safe_load(path.read_text(encoding="utf-8")))

    def test_unsafe_voice_backend_fails_before_model_loading(self) -> None:
        with self.assertRaisesRegex(ValueError, "whisper_backend"):
            validate_voice_config(
                {"validation": {"whisper_backend": "mystery_backend"}}
            )

    def test_invalid_enabled_schedule_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires at least one window"):
            validate_brain_config({"schedule": {"enabled": True, "windows": []}})


class ProgressEstimatorTests(unittest.TestCase):
    def test_eta_uses_a_bounded_median_and_reports_confidence(self) -> None:
        estimator = ProgressEstimator(window_size=3)
        estimator.observe("tts", 1, 2)
        estimator.observe("tts", 1, 20)
        estimator.observe("tts", 1, 3)

        eta, confidence = estimator.estimate(
            "tts", completed_units=4, total_units=10
        )

        self.assertEqual(eta, 18.0)
        self.assertEqual(confidence, "medium")
        snapshot = estimator.snapshot(
            "tts", stage="generating", phase="synthesis", message="Working",
            completed_units=4, total_units=10,
        )
        self.assertEqual(snapshot.percent, 40.0)
        self.assertEqual(snapshot.schema_version, 1)


class ReferenceSelectionTests(unittest.TestCase):
    def test_real_diverse_dialogue_beats_repetitive_emphasis(self) -> None:
        clear = (
            "We should cross the old bridge before sunrise, then ask the "
            "station master which road reaches the harbor."
        )
        repetitive = "RUN RUN RUN! RUN! RUN!"

        selection = select_reference_text(
            [repetitive, clear], seed_text="Generic fallback sentence."
        )

        self.assertGreater(reference_line_score(clear), reference_line_score(repetitive))
        self.assertTrue(selection.text.startswith(clear))
        self.assertFalse(selection.used_seed_text)

    def test_seed_is_only_appended_when_real_dialogue_is_insufficient(self) -> None:
        selection = select_reference_text(
            ["Yes."], seed_text="This calm sentence supplies enough varied words for a stable voice reference.",
            minimum_words=8,
        )
        self.assertTrue(selection.used_seed_text)
        self.assertEqual(selection.source_line_count, 1)


class PerformanceSummaryTests(unittest.TestCase):
    def test_summary_uses_latest_successful_chapter_record(self) -> None:
        records = [
            {"event": "chapter_generation", "chapter_number": 1,
             "segments": 2, "synthesis_cache_misses": 2},
            {"event": "chapter_generation", "chapter_number": 1,
             "segments": 2, "synthesis_cache_hits": 2},
            {"event": "chapter_generation", "chapter_number": 2,
             "segments": 4, "failed_validation": 1},
            {"event": "chapter_mastering", "chapter_number": 1},
        ]
        summary = summarize_metrics(records)
        self.assertEqual(summary["generation_chapters"], 1)
        self.assertEqual(summary["mastered_chapters"], 1)
        self.assertEqual(summary["generation_totals"]["synthesis_cache_hits"], 2.0)
        self.assertNotIn("synthesis_cache_misses", summary["generation_totals"])

    def test_reader_ignores_a_truncated_jsonl_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.jsonl"
            path.write_text(json.dumps({"event": "ok"}) + "\n{", encoding="utf-8")
            self.assertEqual(read_metrics(path), [{"event": "ok"}])


class LogRotationTests(unittest.TestCase):
    def test_closed_managed_log_is_rotated_with_bounded_backups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "managed.log"
            path.write_text("abcdef", encoding="utf-8")
            self.assertTrue(rotate_file(path, max_bytes=3, backup_count=2))
            self.assertFalse(path.exists())
            self.assertEqual(
                (Path(directory) / "managed.log.1").read_text(encoding="utf-8"),
                "abcdef",
            )


if __name__ == "__main__":
    unittest.main()
