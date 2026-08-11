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
from scripts.benchmark_tts_fixture import (
    _balanced_order,
    _deep_update,
    _summarize_mode,
)
from scripts.benchmark_script_chunks import _parse_configs


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

    def test_summary_reports_tts_percentiles_and_substages(self) -> None:
        records = [
            {
                "event": "chapter_generation",
                "chapter_number": 1,
                "segment_metrics": [
                    {
                        "line_id": "ch01_0000",
                        "speaker_role": "narrator",
                        "text_characters": 40,
                        "synthesis_seconds": 2.0,
                        "synthesis_audio_rtf": 1.0,
                        "synthesis_cache_hit": False,
                        "tts_autoregressive_generation_seconds": 1.5,
                        "tts_total_seconds": 2.0,
                        "tts_cold_model_loads": 1,
                    },
                    {
                        "line_id": "ch01_0001",
                        "speaker_role": "character",
                        "text_characters": 140,
                        "synthesis_seconds": 4.0,
                        "synthesis_audio_rtf": 2.0,
                        "synthesis_cache_hit": False,
                        "tts_autoregressive_generation_seconds": 3.5,
                        "tts_total_seconds": 4.0,
                    },
                    {
                        "line_id": "ch01_0002",
                        "speaker_role": "narrator",
                        "text_characters": 30,
                        "synthesis_seconds": 0.0,
                        "synthesis_audio_rtf": 0.0,
                        "synthesis_cache_hit": True,
                    },
                ],
            }
        ]

        summary = summarize_metrics(records)["tts_segments"]

        self.assertEqual(summary["fresh"]["segments"], 2)
        self.assertEqual(summary["cached"]["segments"], 1)
        self.assertEqual(summary["fresh"]["synthesis_seconds"]["p50"], 3.0)
        self.assertEqual(
            summary["by_text_length"]["short_0_80"]["segments"],
            1,
        )
        self.assertEqual(summary["by_speaker_role"]["character"]["segments"], 1)
        self.assertEqual(summary["by_model_state"]["cold"]["segments"], 1)
        self.assertEqual(
            summary["substage_totals_seconds"][
                "tts_autoregressive_generation_seconds"
            ],
            5.0,
        )


class TTSBenchmarkHarnessTests(unittest.TestCase):
    def test_balanced_order_preserves_equal_repetition_counts(self) -> None:
        order = _balanced_order(5, "ABBA")
        self.assertEqual(order.count("A"), 5)
        self.assertEqual(order.count("B"), 5)
        self.assertEqual(order[:4], list("ABBA"))
        self.assertEqual(_balanced_order(1, "ABBA"), ["A", "B"])
        self.assertEqual(_balanced_order(1, "BAAB"), ["B", "A"])

    def test_generation_patch_merges_nested_values_without_mutating_control(self) -> None:
        control = {
            "temperature": 0.9,
            "adaptive_max_new_tokens": {"enabled": False, "minimum_tokens": 512},
        }
        candidate = _deep_update(
            control,
            {"adaptive_max_new_tokens": {"enabled": True}},
        )
        self.assertFalse(control["adaptive_max_new_tokens"]["enabled"])
        self.assertTrue(candidate["adaptive_max_new_tokens"]["enabled"])
        self.assertEqual(candidate["adaptive_max_new_tokens"]["minimum_tokens"], 512)

    def test_mode_summary_reports_median_and_tail(self) -> None:
        summary = _summarize_mode(
            [
                {
                    "realtime_factor": value,
                    "wall_seconds": value * 2,
                    "wer": 0.01,
                    "speaker_similarity": 0.95,
                }
                for value in (1.0, 2.0, 3.0, 4.0, 5.0)
            ]
        )
        self.assertEqual(summary["rtf_p50"], 3.0)
        self.assertEqual(summary["rtf_p95"], 4.8)
        self.assertEqual(summary["maximum_wer"], 0.01)

    def test_script_chunk_config_parser_requires_two_positive_pairs(self) -> None:
        configs = _parse_configs("350:40,550:60")
        self.assertEqual(configs[0]["label"], "w350_f40")
        self.assertEqual(configs[1]["max_fragments_per_chunk"], 60)
        with self.assertRaises(ValueError):
            _parse_configs("350:40")


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
