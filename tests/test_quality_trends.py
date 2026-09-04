import unittest

from brain.orchestrator.quality_trends import (
    _within_chapter_consistency,
    build_long_form_quality_report,
)
from shared.models import ScriptChapter, ScriptLine


def _script(chapter: int) -> ScriptChapter:
    return ScriptChapter(
        chapter_number=chapter,
        chapter_title=str(chapter),
        lines=[
            ScriptLine(
                line_id=f"ch{chapter:02d}_{index:04d}",
                speaker="mara",
                voice_id="mara",
                text="A line.",
            )
            for index in range(5)
        ],
    )


def _logs(chapter: int, similarity: float, pitch: float, monotone: bool = False):
    return [
        {
            "line_id": f"ch{chapter:02d}_{index:04d}",
            "chapter_number": chapter,
            "attempt": 1,
            "details": {
                "selected": True,
                "speaker_similarity": similarity,
                "pitch_median": pitch,
                "duration_seconds": 2.0,
                "monotone_warning": monotone,
            },
        }
        for index in range(5)
    ]


def test_long_form_report_flags_correlated_identity_drift_and_prosody():
    report = build_long_form_quality_report(
        _logs(1, 0.86, 140.0) + _logs(2, 0.58, 185.0, monotone=True),
        [_script(1), _script(2)],
    )
    kinds = {warning["kind"] for warning in report["warnings"]}
    assert "cross_chapter_voice_drift" in kinds
    assert "sustained_monotone_delivery" in kinds


def test_pitch_expression_alone_does_not_claim_identity_drift():
    report = build_long_form_quality_report(
        _logs(1, 0.86, 140.0) + _logs(2, 0.86, 190.0),
        [_script(1), _script(2)],
    )
    assert not any(warning["kind"] == "cross_chapter_voice_drift" for warning in report["warnings"])


class WithinChapterConsistencyTests(unittest.TestCase):
    """Line-to-line delivery variation inside one chapter must be measurable.

    Every prior audio gate is per-segment or cross-chapter. Nothing checked
    whether adjacent lines in the same chapter match each other -- the artifact
    a listener notices first, and the metric a sampling or seeding change needs
    to be evaluated against.
    """

    @staticmethod
    def _rows(pitches, *, duration=2.0, characters=60):
        return [
            {
                "line_id": f"ch01_{index:04d}",
                "pitch_median": pitch,
                "duration_seconds": duration,
                "text_characters": characters,
            }
            for index, pitch in enumerate(pitches, 1)
        ]

    def test_a_consistent_narrator_reports_no_warning(self) -> None:
        result = _within_chapter_consistency(self._rows([120.0, 121.0, 119.5, 120.5, 120.0, 121.5, 119.0]))
        self.assertTrue(result["measured_for_warnings"])
        self.assertEqual(result["warnings"], [])
        self.assertLess(result["pitch_relative_spread"], 0.05)

    def test_wildly_varying_pitch_is_flagged(self) -> None:
        result = _within_chapter_consistency(self._rows([90.0, 180.0, 95.0, 200.0, 100.0, 190.0, 88.0]))
        self.assertIn("within_chapter_pitch_variation", result["warnings"])
        self.assertIn("within_chapter_pitch_jump", result["warnings"])

    def test_inconsistent_speaking_rate_is_flagged(self) -> None:
        rows = []
        # Same pitch throughout; only the characters-per-second varies.
        for index, (duration, characters) in enumerate(
            [(1.0, 30), (4.0, 30), (1.0, 30), (4.5, 30), (1.0, 30), (5.0, 30), (1.0, 30)],
            start=1,
        ):
            rows.append(
                {
                    "line_id": f"ch01_{index:04d}",
                    "pitch_median": 120.0,
                    "duration_seconds": duration,
                    "text_characters": characters,
                }
            )
        result = _within_chapter_consistency(rows)
        self.assertIn("within_chapter_rate_variation", result["warnings"])
        self.assertNotIn("within_chapter_pitch_variation", result["warnings"])

    def test_a_short_chapter_is_measured_but_never_warned(self) -> None:
        """Too few segments to distinguish variance from expression."""
        result = _within_chapter_consistency(self._rows([90.0, 200.0, 95.0]))
        self.assertFalse(result["measured_for_warnings"])
        self.assertEqual(result["warnings"], [])
        self.assertIsNotNone(result["pitch_relative_spread"])

    def test_unvoiced_and_zero_duration_rows_do_not_crash(self) -> None:
        rows = [
            {"line_id": "a", "pitch_median": 0.0, "duration_seconds": 0.0, "text_characters": 0},
            {"line_id": "b", "pitch_median": 0.0, "duration_seconds": 1.0, "text_characters": 0},
        ]
        result = _within_chapter_consistency(rows)
        self.assertIsNone(result["pitch_relative_spread"])
        self.assertIsNone(result["speaking_rate_relative_spread"])
        self.assertIsNone(result["largest_adjacent_pitch_jump_ratio"])
        self.assertEqual(result["warnings"], [])

    def test_empty_input_is_safe(self) -> None:
        result = _within_chapter_consistency([])
        self.assertEqual(result["segments"], 0)
        self.assertEqual(result["warnings"], [])

    def test_the_diagnostic_is_attached_to_the_report(self) -> None:
        """It must reach the persisted report, not just exist as a helper."""
        chapter = ScriptChapter(
            chapter_number=1,
            chapter_title="One",
            lines=[
                ScriptLine(
                    line_id=f"ch01_{index:04d}",
                    speaker="narrator",
                    text="A sentence of roughly consistent length here.",
                )
                for index in range(1, 8)
            ],
        )
        logs = [
            {
                "line_id": f"ch01_{index:04d}",
                "chapter_number": 1,
                "attempt": 1,
                "details": {
                    "selected": True,
                    "pitch_median": 90.0 if index % 2 else 200.0,
                    "duration_seconds": 2.0,
                    "speaker_similarity": 0.95,
                },
            }
            for index in range(1, 8)
        ]
        report = build_long_form_quality_report(logs, [chapter])
        row = report["chapter_voice_metrics"][0]
        self.assertIn("within_chapter_consistency", row)
        self.assertIn("within_chapter_pitch_variation", row["warnings"])
        kinds = {warning["kind"] for warning in report["warnings"]}
        self.assertIn("within_chapter_pitch_variation", kinds)
        self.assertIn(
            "within_chapter_min_segments",
            report["policy"],
        )
