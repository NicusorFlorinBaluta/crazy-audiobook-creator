from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from brain.orchestrator.pipeline import Pipeline
from shared.models import ScriptChapter, ScriptLine
from shared.pronunciation import (
    apply_pronunciations,
    build_pronunciation_inventory,
)


class ValidationTermsTests(unittest.TestCase):
    def test_inventory_exposes_repeated_terms_without_inventing_pronunciation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_dir = Path(directory)
            (project_dir / "characters.json").write_text(
                json.dumps({"characters": {"narrator": {"name": "Narrator"}}}),
                encoding="utf-8",
            )
            (project_dir / "book_script.json").write_text(
                json.dumps(
                    {
                        "chapters": [
                            {
                                "chapter_number": 1,
                                "lines": [
                                    {"text": "They sailed toward Patji."},
                                    {"text": "Patji sheltered the Eelakin."},
                                ],
                            },
                            {
                                "chapter_number": 2,
                                "lines": [{"text": "The Eelakin returned."}],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            inventory = build_pronunciation_inventory(project_dir)

        by_term = {item["term"]: item for item in inventory["candidates"]}
        self.assertEqual(by_term["Patji"]["status"], "review_required")
        self.assertIsNone(by_term["Patji"]["spoken_text"])
        self.assertEqual(by_term["Eelakin"]["chapters"], [1, 2])
        self.assertNotIn("They", by_term)

    def test_project_mapping_promotes_inventory_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_dir = Path(directory)
            (project_dir / "book_script.json").write_text(
                json.dumps(
                    {
                        "chapters": [
                            {
                                "chapter_number": 1,
                                "lines": [
                                    {"text": "Patji waited."},
                                    {"text": "Patji listened."},
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (project_dir / "pronunciation_dict.json").write_text(
                json.dumps({"Patji": "Pah-chee"}),
                encoding="utf-8",
            )

            inventory = build_pronunciation_inventory(project_dir)

        candidate = next(item for item in inventory["candidates"] if item["term"] == "Patji")
        self.assertEqual(candidate["status"], "verified")
        self.assertEqual(candidate["spoken_text"], "Pah-chee")
        self.assertEqual(candidate["mapping_source"], "project")

    def test_pronunciation_application_is_non_recursive(self) -> None:
        spoken = apply_pronunciations(
            "King of the Pantheon.",
            {
                "King": "Keen-g",
                "King of the Pantheon": "King-of-the-Pan-thee-on",
            },
        )
        self.assertEqual(spoken, "King-of-the-Pan-thee-on.")

    def test_pronunciation_override_preserves_authored_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_dir = Path(directory)
            (project_dir / "pronunciation_dict.json").write_text(
                json.dumps({"Patji": "Pah-chee"}),
                encoding="utf-8",
            )
            chapter = ScriptChapter(
                chapter_number=1,
                chapter_title="One",
                lines=[
                    ScriptLine(
                        line_id="ch01_0000",
                        speaker="narrator",
                        text="Patji watched Patji's shore.",
                    )
                ],
            )

            prepared = Pipeline.__new__(Pipeline)._prepare_generation_lines(
                chapter,
                project_dir,
            )

        self.assertEqual(chapter.lines[0].text, "Patji watched Patji's shore.")
        self.assertEqual(prepared[0].text, "Patji watched Patji's shore.")
        self.assertEqual(
            prepared[0].spoken_text,
            "Pah-chee watched Pah-chee's shore.",
        )

    def test_longest_pronunciation_phrase_is_applied_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_dir = Path(directory)
            (project_dir / "pronunciation_dict.json").write_text(
                json.dumps(
                    {
                        "King": "Keen-g",
                        "King of the Pantheon": "King-of-the-Pan-thee-on",
                    }
                ),
                encoding="utf-8",
            )
            chapter = ScriptChapter(
                chapter_number=1,
                chapter_title="One",
                lines=[
                    ScriptLine(
                        line_id="ch01_0000",
                        speaker="narrator",
                        text="King of the Pantheon.",
                    )
                ],
            )

            prepared = Pipeline.__new__(Pipeline)._prepare_generation_lines(
                chapter,
                project_dir,
            )

        self.assertEqual(prepared[0].spoken_text, "King-of-the-Pan-thee-on.")

    def test_invalid_pronunciation_entry_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_dir = Path(directory)
            (project_dir / "pronunciation_dict.json").write_text(
                json.dumps({"Patji": ""}),
                encoding="utf-8",
            )
            chapter = ScriptChapter(
                chapter_number=1,
                chapter_title="One",
                lines=[
                    ScriptLine(
                        line_id="ch01_0000",
                        speaker="narrator",
                        text="Patji.",
                    )
                ],
            )

            with self.assertRaisesRegex(ValueError, "cannot be empty"):
                Pipeline.__new__(Pipeline)._prepare_generation_lines(
                    chapter,
                    project_dir,
                )

    def test_repeated_world_terms_are_included_without_cast_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_dir = Path(directory)
            (project_dir / "characters.json").write_text(
                json.dumps({"characters": {"narrator": {"name": "Narrator"}}}),
                encoding="utf-8",
            )
            (project_dir / "book_script.json").write_text(
                json.dumps(
                    {
                        "chapters": [
                            {"utterances": [{"text": "They sailed to Patji."}]},
                            {
                                "utterances": [
                                    {"text": "Patji, god of the Eelakin."},
                                    {"text": "The Eelakin returned home."},
                                ]
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            terms = Pipeline._validation_terms(project_dir)

        self.assertIn("Patji", terms)
        self.assertIn("Eelakin", terms)
        self.assertNotIn("They", terms)

    def test_performance_metric_is_durable_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_dir = Path(directory)
            Pipeline._append_performance_metric(
                project_dir,
                {
                    "event": "chapter_generation",
                    "chapter_number": 2,
                    "timings_seconds": {"total": 1.25},
                },
            )
            rows = [
                json.loads(line)
                for line in (project_dir / "performance_metrics.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "chapter_generation")
        self.assertEqual(rows[0]["chapter_number"], 2)
        self.assertIn("timestamp", rows[0])


if __name__ == "__main__":
    unittest.main()
