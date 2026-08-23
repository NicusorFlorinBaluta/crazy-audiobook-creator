from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from fastapi import HTTPException

from brain.dashboard.api.main import (
    ScriptLineUpdate,
    regenerate_chapter,
    update_script_line,
)
import brain.dashboard.api.main as dashboard_main
from brain.director.attribution_audit import audit_book_attribution
from brain.director.character_analyzer import CharacterAnalyzer
from brain.director.script_generator import ScriptGenerator, SourceFragment
from brain.orchestrator.job_queue import JobQueue
from shared.constants import Gender
from shared.models import Character, CharacterRegistry, ExtractedBook, ExtractedChapter, ScriptChapter


def _frag(text: str, start: int = 0) -> SourceFragment:
    return SourceFragment(text=text, start=start, end=start + len(text))


class CharacterAliasDerivationTests(unittest.TestCase):
    def test_derive_character_aliases_extracts_titles_and_suffixes(self) -> None:
        aliases = CharacterAnalyzer._derive_character_aliases(
            char_id="captain_vathi",
            name="Captain Vathi",
            raw_aliases=["Commander"],
        )
        self.assertIn("Commander", aliases)
        self.assertIn("Captain Vathi", aliases)
        self.assertIn("Vathi", aliases)

    def test_derive_character_aliases_extracts_of_patterns(self) -> None:
        aliases = CharacterAnalyzer._derive_character_aliases(
            char_id="sixth_of_dusk",
            name="Sixth of Dusk",
            raw_aliases=[],
        )
        self.assertIn("Sixth of Dusk", aliases)
        self.assertIn("Dusk", aliases)

        aliases_soil = CharacterAnalyzer._derive_character_aliases(
            char_id="second_of_the_soil",
            name="Second of the Soil",
            raw_aliases=[],
        )
        self.assertIn("Soil", aliases_soil)

    def test_derive_character_aliases_filters_stopwords(self) -> None:
        aliases = CharacterAnalyzer._derive_character_aliases(
            char_id="the_narrator",
            name="The Narrator",
            raw_aliases=["the", "and", "narrator"],
        )
        for stop in ["the", "and", "narrator"]:
            self.assertNotIn(stop, [a.lower() for a in aliases])


class DialogueSpeakerResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = CharacterRegistry(
            book_title="Test Book",
            book_author="Test Author",
            characters={
                "sixth_of_dusk": Character(
                    id="sixth_of_dusk",
                    name="Sixth of Dusk",
                    gender=Gender.MALE,
                    age_range="adult",
                    voice_description="deep and cautious",
                    aliases=["Dusk"],
                ),
                "vathi": Character(
                    id="vathi",
                    name="Vathi",
                    gender=Gender.FEMALE,
                    age_range="adult",
                    voice_description="sharp and scholarly",
                    aliases=["Captain Vathi"],
                ),
                "starling": Character(
                    id="starling",
                    name="Starling",
                    gender=Gender.FEMALE,
                    age_range="child",
                    voice_description="young and energetic",
                    aliases=[],
                ),
            }
        )
        self.allowed_speakers = {"narrator", "sixth_of_dusk", "vathi", "starling"}

    def test_resolve_dialogue_speaker_via_alias(self) -> None:
        fragments = [
            _frag('"We must move now,"'),
            _frag("Dusk whispered to the scout."),
        ]
        speaker = ScriptGenerator._resolve_dialogue_speaker(
            frag_idx=0,
            fragments=fragments,
            metadata_map={},
            allowed_speakers=self.allowed_speakers,
            registry=self.registry,
        )
        self.assertEqual(speaker, "sixth_of_dusk")

    def test_resolve_dialogue_speaker_via_pronoun_matching_single_active_character(self) -> None:
        # Preceding dialogue established Vathi and Dusk as active
        metadata_map = {
            0: {"speaker": "sixth_of_dusk"},
            1: {"speaker": "narrator"},
            2: {"speaker": "vathi"},
            3: {"speaker": "narrator"},
        }
        fragments = [
            _frag('"Are you there?"'),
            _frag("he asked."),
            _frag('"I hear you,"'),
            _frag("she replied softly."),
            _frag('"Do not disconnect,"'),
            _frag("she urged."),
        ]
        speaker = ScriptGenerator._resolve_dialogue_speaker(
            frag_idx=4,
            fragments=fragments,
            metadata_map=metadata_map,
            allowed_speakers=self.allowed_speakers,
            registry=self.registry,
        )
        # Female pronoun 'she' with Vathi as the only active female speaker in recent context
        self.assertEqual(speaker, "vathi")

    def test_resolve_dialogue_speaker_via_turn_alternation(self) -> None:
        # Dusk (A) and Vathi (B) talking
        metadata_map = {
            0: {"speaker": "sixth_of_dusk"},
            1: {"speaker": "vathi"},
            2: {"speaker": "sixth_of_dusk"},
        }
        fragments = [
            _frag('"First line."'),
            _frag('"Second line."'),
            _frag('"Third line."'),
            _frag('"Fourth line."'),
            _frag("silence fell."),
        ]
        speaker = ScriptGenerator._resolve_dialogue_speaker(
            frag_idx=3,
            fragments=fragments,
            metadata_map=metadata_map,
            allowed_speakers=self.allowed_speakers,
            registry=self.registry,
        )
        # Preceding dialogue speaker was sixth_of_dusk, so next turn in 2-person dialogue goes to vathi
        self.assertEqual(speaker, "vathi")

    def test_pronoun_alias_is_not_treated_as_named_evidence(self) -> None:
        registry = self.registry.model_copy(deep=True)
        registry.characters["starling"].aliases.append("She")
        exact, kind, gender = ScriptGenerator._dialogue_tag_evidence(
            "She said quietly.", registry
        )
        self.assertIsNone(exact)
        self.assertEqual(kind, "pronoun_gender")
        self.assertEqual(gender, Gender.FEMALE)

    def test_same_gender_context_uses_turn_order_not_most_recent_gender(self) -> None:
        metadata_map = {
            0: {"speaker": "vathi"},
            1: {"speaker": "starling"},
        }
        fragments = [
            _frag('"First."'),
            _frag('"Second."'),
            _frag('"Third,"'),
            _frag("she said."),
        ]
        speaker = ScriptGenerator._resolve_dialogue_speaker(
            2,
            fragments,
            metadata_map,
            self.allowed_speakers,
            registry=self.registry,
        )
        self.assertEqual(speaker, "vathi")

    def test_unresolved_fallback_requires_review(self) -> None:
        fragments = [_frag('"Who is there?"')]
        metadata = ScriptGenerator._fallback_fragment_metadata(
            0,
            {"lines": [{"id": 0, "speaker": "vathi"}]},
            fragments,
            self.allowed_speakers,
            self.registry,
        )
        self.assertEqual(metadata["speaker"], "vathi")
        self.assertTrue(metadata["attribution_review_required"])
        self.assertLess(metadata["speaker_confidence"], 0.55)


class SampleBookAttributionAuditTests(unittest.TestCase):
    def test_sample_book_14_legacy_collective_assignment_is_detected(self) -> None:
        project_dir = Path("brain/projects/sample_book-14")
        if not project_dir.exists() or not (project_dir / "book.json").exists():
            self.skipTest("sample_book-14 not present in workspace")

        book_data = json.loads((project_dir / "book.json").read_text(encoding="utf-8"))
        extracted_book = ExtractedBook.model_validate(book_data)

        char_data = json.loads((project_dir / "characters.json").read_text(encoding="utf-8"))
        registry = CharacterRegistry.model_validate(char_data)

        # Load chapter scripts
        scripts_dir = project_dir / "script"
        script_files = sorted(scripts_dir.glob("chapter_*.json"))
        scripts = [
            ScriptChapter.model_validate_json(f.read_text(encoding="utf-8"))
            for f in script_files
            if not f.name.endswith(".meta.json")
        ]

        audit_report = audit_book_attribution(extracted_book, registry, scripts)
        issues = audit_report.get("issues", [])
        issues_by_kind = {issue["kind"]: issue for issue in issues}
        self.assertEqual(
            set(issues_by_kind),
            {"generic_role_tag", "self_identified_generic_speaker"},
        )
        self.assertEqual(issues_by_kind["generic_role_tag"]["speaker"], "children")
        self.assertIn("child_male", issues_by_kind["generic_role_tag"]["message"])
        self.assertEqual(
            issues_by_kind["self_identified_generic_speaker"]["expected_speaker"],
            "vathi",
        )
        self.assertEqual(audit_report.get("summary", {}).get("chapters"), 8)


class ScriptApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.temp_dir.name)
        self.scripts_dir = self.project_dir / "script"
        self.scripts_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = self.project_dir / "test.db"
        self.job_queue = JobQueue(db_path=str(self.db_path))
        self.project_id = "test-proj"
        self.job_queue.create_job(
            self.project_id,
            {
                "status": "idle",
                "scripted_chapters": [1, 2],
                "generated_chapters": [1],
                "mastered_chapters": [1],
            },
        )
        self.prev_job_queue = dashboard_main.job_queue
        self.prev_pipeline = dashboard_main.pipeline
        dashboard_main.job_queue = self.job_queue
        dashboard_main.pipeline = MagicMock()
        dashboard_main.pipeline.job_queue = self.job_queue

        chapter_data = {
            "chapter_number": 1,
            "chapter_title": "Chapter 1",
            "lines": [
                {"id": "ch01_0001", "speaker": "narrator", "text": "He looked up."},
                {"id": "ch01_0002", "speaker": "narrator", "text": '"Hello," he said.'},
            ],
        }
        (self.scripts_dir / "chapter_001.json").write_text(
            json.dumps(chapter_data), encoding="utf-8"
        )
        (self.project_dir / "book_script.json").write_text(
            json.dumps({"chapters": [chapter_data]}), encoding="utf-8"
        )
        (self.project_dir / "characters.json").write_text(
            json.dumps(
                {
                    "book_title": "Test",
                    "book_author": "Tester",
                    "characters": {
                        "narrator": {"id": "narrator", "name": "Narrator"},
                        "vathi": {"id": "vathi", "name": "Vathi"},
                    },
                }
            ),
            encoding="utf-8",
        )
        (self.project_dir / ".fingerprints.json").write_text(
            json.dumps({"chapters": {"1": "hash1", "2": "hash2"}}), encoding="utf-8"
        )

        self.project_dir_patcher = patch(
            "brain.dashboard.api.main._project_dir",
            return_value=self.project_dir,
        )
        self.project_dir_patcher.start()

    async def asyncTearDown(self) -> None:
        dashboard_main.job_queue = self.prev_job_queue
        dashboard_main.pipeline = self.prev_pipeline
        self.project_dir_patcher.stop()
        self.temp_dir.cleanup()

    async def test_patch_script_line_updates_chapter_and_merged_script(self) -> None:
        req = ScriptLineUpdate(speaker="vathi")
        res = await update_script_line(self.project_id, 1, "ch01_0002", req)
        self.assertEqual(res["status"], "success")

        # Verify chapter file
        ch_data = json.loads(
            (self.scripts_dir / "chapter_001.json").read_text(encoding="utf-8")
        )
        self.assertEqual(ch_data["lines"][1]["speaker"], "vathi")

        # Verify book_script.json
        merged = json.loads(
            (self.project_dir / "book_script.json").read_text(encoding="utf-8")
        )
        self.assertEqual(merged["chapters"][0]["lines"][1]["speaker"], "vathi")

    async def test_regenerate_chapter_cleans_script_fingerprint_and_job_state(self) -> None:
        res = await regenerate_chapter(self.project_id, 1)
        self.assertEqual(res["status"], "success")

        # Chapter script should be removed
        self.assertFalse((self.scripts_dir / "chapter_001.json").exists())

        # Chapter 1 fingerprint removed
        fp_data = json.loads(
            (self.project_dir / ".fingerprints.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("1", fp_data.get("chapters", {}))
        self.assertIn("2", fp_data.get("chapters", {}))

        # Job state updated
        state = self.job_queue.get_job(self.project_id)
        self.assertNotIn(1, state.get("scripted_chapters", []))
        self.assertNotIn(1, state.get("generated_chapters", []))
        self.assertNotIn(1, state.get("mastered_chapters", []))

    async def test_script_edit_rejects_unknown_speaker(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await update_script_line(
                self.project_id,
                1,
                "ch01_0002",
                ScriptLineUpdate(speaker="not_in_registry"),
            )
        self.assertEqual(raised.exception.status_code, 422)

    async def test_script_edit_rejects_running_project(self) -> None:
        self.job_queue.update_job(self.project_id, {"running": True})
        with self.assertRaises(HTTPException) as raised:
            await update_script_line(
                self.project_id,
                1,
                "ch01_0002",
                ScriptLineUpdate(speaker="vathi"),
            )
        self.assertEqual(raised.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
