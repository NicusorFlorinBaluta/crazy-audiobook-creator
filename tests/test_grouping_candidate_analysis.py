import unittest

from scripts.analyze_grouping_candidates import (
    GroupingBounds,
    analyze_chapter,
    parse_candidates,
)
from shared.models import ScriptChapter, ScriptLine


class GroupingCandidateAnalysisTests(unittest.TestCase):
    def test_parse_candidates_rejects_engine_ceiling_violation(self) -> None:
        with self.assertRaisesRegex(ValueError, "500-character ceiling"):
            parse_candidates("300:50:501:70")

    def test_analysis_reuses_production_grouping_and_preserves_trace(self) -> None:
        source = "First sentence. Second sentence.\n\nA separate paragraph."
        first_end = len("First sentence.")
        second_start = first_end + 1
        second_end = second_start + len("Second sentence.")
        third_start = second_end + 2
        chapter = ScriptChapter(
            chapter_number=1,
            chapter_title="Test",
            lines=[
                ScriptLine(
                    line_id="ch01_0000",
                    speaker="narrator",
                    text=source[:first_end],
                    source_fragment_id=0,
                    source_fragment_ids=[0],
                    source_start=0,
                    source_end=first_end,
                ),
                ScriptLine(
                    line_id="ch01_0001",
                    speaker="narrator",
                    text=source[second_start:second_end],
                    source_fragment_id=1,
                    source_fragment_ids=[1],
                    source_start=second_start,
                    source_end=second_end,
                ),
                ScriptLine(
                    line_id="ch01_0002",
                    speaker="narrator",
                    text=source[third_start:],
                    source_fragment_id=2,
                    source_fragment_ids=[2],
                    source_start=third_start,
                    source_end=len(source),
                ),
            ],
        )
        result = analyze_chapter(
            chapter,
            source,
            GroupingBounds("candidate", 100, 20, 100, 20),
        )
        self.assertEqual(result["control_calls"], 3)
        self.assertEqual(result["candidate_calls"], 2)
        self.assertEqual(result["call_reduction"], 1)
        self.assertTrue(result["fragment_trace_preserved"])
        self.assertTrue(result["unique_fragment_trace"])
        self.assertEqual(result["source_mismatches"], [])
        self.assertFalse(result["introduced_over_engine_ceiling"])
        self.assertEqual(
            result["added_merges"][0]["constituent_line_ids"],
            [
                "ch01_0000",
                "ch01_0001",
            ],
        )

    def test_preexisting_oversize_line_does_not_fail_candidate_ceiling(self) -> None:
        source = "A" * 501
        chapter = ScriptChapter(
            chapter_number=1,
            chapter_title="Existing",
            lines=[
                ScriptLine(
                    line_id="ch01_0000",
                    speaker="narrator",
                    text=source,
                    source_fragment_id=0,
                    source_fragment_ids=[0],
                    source_start=0,
                    source_end=len(source),
                )
            ],
        )
        result = analyze_chapter(
            chapter,
            source,
            GroupingBounds("candidate", 300, 50, 400, 68),
        )
        self.assertTrue(result["preexisting_over_engine_ceiling"])
        self.assertFalse(result["introduced_over_engine_ceiling"])


if __name__ == "__main__":
    unittest.main()
