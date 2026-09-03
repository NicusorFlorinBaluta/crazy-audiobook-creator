from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from brain.director.character_analyzer import CharacterAnalyzer
from brain.director.ollama_client import OllamaGenerationLimitError
from brain.director.script_generator import (
    MetadataAttributionError,
    ScriptGenerator,
    SourceFragment,
)
from brain.orchestrator.pipeline import Pipeline
from shared.constants import Gender
from shared.artifacts import (
    assert_script_covers_source,
    build_segment_manifest,
    format_chapter_set,
)
from shared.models import (
    Character,
    CharacterRegistry,
    ExtractedBook,
    ExtractedChapter,
    BookMetadata,
    GenerateChapterRequest,
    ScriptChapter,
    ScriptLine,
)


class FakeCharacterOllama:
    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def generate_json(self, *args, **kwargs):
        self.calls += 1
        return {
            "tone": "quiet",
            "characters": {
                "narrator": {
                    "name": "Narrator",
                    "gender": "other",
                    "voice_description": "Clear and measured",
                    "dialogue_count": 1,
                }
            },
        }


class FakeReferenceAugmentationOllama:
    model = "fake-reference"

    def __init__(self, augmentation=None, fail_augmentation=False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.augmentation = augmentation or {"patches": []}
        self.fail_augmentation = fail_augmentation

    def generate_json(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        if len(self.calls) == 1:
            return {
                "tone": "adventurous",
                "characters": {
                    "mara": {
                        "name": "Mara",
                        "gender": "female",
                        "age_range": "adult",
                        "voice_description": "female speaker with a clear voice",
                        "speaking_style": "",
                        "dialogue_count": 4,
                    }
                },
            }
        if self.fail_augmentation:
            raise RuntimeError("supplement unavailable")
        return self.augmentation


class FakeIdentityOllama:
    model = "fake"

    def __init__(self, evidence: str, same_character: bool = True) -> None:
        self.evidence = evidence
        self.same_character = same_character

    def generate_json(self, *args, **kwargs):
        return {
            "decisions": [
                {
                    "left_id": "dusk",
                    "right_id": "sixth_of_dusk",
                    "same_character": self.same_character,
                    "evidence": self.evidence,
                }
            ]
        }


class FakeScriptOllama:
    model = "fake-script"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []
        self.last_generation_metrics = {}

    def generate_json(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected script model request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ScriptFidelityTests(unittest.TestCase):
    def test_joint_checkpoint_allows_only_exact_or_source_compatible_runtime_migration(self) -> None:
        exact = {"fingerprint": "current", "source_fingerprint": "source"}
        migrated = {"fingerprint": "previous", "source_fingerprint": "source"}
        stale_source = {"fingerprint": "previous", "source_fingerprint": "other"}

        self.assertTrue(
            Pipeline._joint_checkpoint_is_compatible(
                exact,
                character_fingerprint="current",
                source_fingerprint="source",
            )
        )
        self.assertTrue(
            Pipeline._joint_checkpoint_is_compatible(
                migrated,
                character_fingerprint="current",
                source_fingerprint="source",
            )
        )
        self.assertFalse(
            Pipeline._joint_checkpoint_is_compatible(
                stale_source,
                character_fingerprint="current",
                source_fingerprint="source",
            )
        )

    def test_pipeline_joint_mode_persists_registry_scripts_and_comparison_metrics(self) -> None:
        source = '"We should leave," Mara said.'
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        response = {
            "chapter_number": 1,
            "chapter_title": "One",
            "chapter_summary": "Mara proposes leaving.",
            "scenes": [],
            "character_updates": [{
                "character_id": "mara",
                "name": "Mara",
                "aliases": [],
                "gender": "female",
                "age_range": "adult",
                "personality_traits": ["decisive"],
                "voice_description": (
                    "female speaker, adult age, medium pitch, moderate volume, "
                    "measured speed, neutral accent, clear texture, high clarity, "
                    "natural fluency, decisive emotion and calm tone"
                ),
                "speaking_style": "concise",
                "evidence_fragment_ids": [0],
                "discovery_confidence": 0.96,
            }],
            "lines": [
                {
                    "id": index,
                    "speaker": (
                        "mara"
                        if ScriptGenerator._is_dialogue_fragment(fragment.text)
                        else "narrator"
                    ),
                    "speaker_confidence": 0.96,
                    "speaker_evidence": "Mara said",
                    "dialogue_kind": (
                        "spoken"
                        if ScriptGenerator._is_dialogue_fragment(fragment.text)
                        else None
                    ),
                    "emotion": "calm resolve",
                    "speed": 1.0,
                }
                for index, fragment in enumerate(fragments)
            ],
        }

        class FakeQueue:
            def __init__(self):
                self.state = {
                    "force_character_analysis": False,
                    "scripted_chapters": [],
                    "generated_chapters": [],
                    "mastered_chapters": [],
                }

            def get_job(self, project_id):
                return dict(self.state)

            def update_job(self, project_id, updates):
                self.state.update(updates)

            def update_progress(self, project_id, progress):
                self.state["progress"] = progress

        with TemporaryDirectory() as directory:
            project_dir = Path(directory)
            book = ExtractedBook(
                metadata=BookMetadata(
                    title="Book",
                    author="Author",
                    total_chapters=1,
                    total_words=len(source.split()),
                ),
                chapters=[ExtractedChapter(
                    number=1,
                    title="One",
                    text=source,
                    word_count=len(source.split()),
                )],
            )
            (project_dir / "book.json").write_text(
                book.model_dump_json(indent=2), encoding="utf-8"
            )
            ollama = FakeScriptOllama([response])
            pipeline = Pipeline.__new__(Pipeline)
            pipeline.config = {"script": {"joint_analysis": True}}
            pipeline.ollama = ollama
            pipeline.character_analyzer = CharacterAnalyzer(ollama)
            pipeline.script_generator = ScriptGenerator(
                ollama,
                group_utterances=False,
            )
            pipeline.job_queue = FakeQueue()
            pipeline.external_validator = MagicMock()
            pipeline.external_validator.resolve_attributions.return_value = {
                "attempted": False
            }
            pipeline._update_stage = MagicMock()
            pipeline._check_stop = MagicMock()
            pipeline._check_schedule = MagicMock()
            pipeline._check_deployment_pause = MagicMock()
            pipeline._assert_attribution_audit = MagicMock()
            pipeline._progress_estimator = MagicMock()
            pipeline._progress_estimator.snapshot.return_value = {"percent": 0}
            metrics: list[dict] = []
            pipeline._append_performance_metric = (
                lambda project_path, metric: metrics.append(metric)
            )
            pipeline.character_analyzer.analyze = MagicMock(
                side_effect=AssertionError("legacy analyzer should not run")
            )

            with patch(
                "brain.orchestrator.pipeline.build_pronunciation_inventory",
                return_value={},
            ):
                pipeline._run_script_director("book", project_dir)

            registry = CharacterRegistry.model_validate_json(
                (project_dir / "characters.json").read_text(encoding="utf-8")
            )
            saved_script = ScriptChapter.model_validate_json(
                (project_dir / "script" / "chapter_001.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn("mara", registry.characters)
            self.assertEqual(
                next(
                    line for line in saved_script.lines
                    if line.dialogue_kind == "spoken"
                ).speaker,
                "mara",
            )
            self.assertEqual(metrics[-1]["director_mode"], "joint")
            self.assertEqual(metrics[-1]["pass1_seconds"], 0.0)
            self.assertFalse(
                (project_dir / "characters.joint.checkpoint.json").exists()
            )

    def test_joint_pass_registers_only_source_evidenced_speaker(self) -> None:
        source = '"We should leave," Mara said.'
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        response = {
            "chapter_number": 1,
            "chapter_title": "One",
            "chapter_summary": "Mara proposes leaving.",
            "scenes": [],
            "character_updates": [
                {
                    "character_id": "mara",
                    "name": "Mara",
                    "aliases": [],
                    "gender": "female",
                    "age_range": "adult",
                    "personality_traits": ["decisive"],
                    "voice_description": (
                        "female speaker, adult age, medium pitch, moderate volume, "
                        "measured speed, neutral accent, clear texture, high clarity, "
                        "natural fluency, decisive emotion and calm tone"
                    ),
                    "speaking_style": "concise and decisive",
                    "test_sentence": "We can make a careful choice and still move forward before the weather changes.",
                    "evidence_fragment_ids": [0],
                    "discovery_confidence": 0.96,
                }
            ],
            "lines": [
                {
                    "id": index,
                    "speaker": (
                        "mara"
                        if ScriptGenerator._is_dialogue_fragment(fragment.text)
                        else "narrator"
                    ),
                    "speaker_confidence": 0.96,
                    "speaker_evidence": "Mara said",
                    "dialogue_kind": (
                        "spoken"
                        if ScriptGenerator._is_dialogue_fragment(fragment.text)
                        else None
                    ),
                    "emotion": "calm",
                    "speed": 1.0,
                    "pause_before_ms": 0,
                    "pause_after_ms": 400,
                }
                for index, fragment in enumerate(fragments)
            ],
        }
        analyzer = CharacterAnalyzer(FakeCharacterOllama())
        book = ExtractedBook(
            metadata=BookMetadata(title="Book", author="Author", total_chapters=1),
            chapters=[
                ExtractedChapter(
                    number=1,
                    title="One",
                    text=source,
                    word_count=len(source.split()),
                )
            ],
        )
        registry = analyzer.create_joint_seed_registry(book)
        generator = ScriptGenerator(FakeScriptOllama([response]))

        script = generator.generate_chapter_script(
            book.chapters[0],
            registry,
            allow_character_discovery=True,
        )

        self.assertIn("mara", registry.characters)
        self.assertAlmostEqual(
            registry.characters["mara"].discovery_confidence,
            0.96,
        )
        self.assertTrue(registry.characters["mara"].discovery_evidence)
        self.assertEqual(
            next(line for line in script.lines if line.dialogue_kind == "spoken").speaker,
            "mara",
        )

    def test_joint_pass_rejects_character_without_matching_source_evidence(self) -> None:
        fragments = [SourceFragment(text='"Hello."', start=0, end=8)]
        registry = CharacterRegistry(
            book_title="Book",
            book_author="Author",
            characters={
                "narrator": Character(
                    id="narrator",
                    name="Narrator",
                    gender=Gender.OTHER,
                    age_range="adult",
                    voice_description="neutral voice",
                )
            },
        )
        generator = ScriptGenerator(FakeScriptOllama([]))
        generator._apply_joint_character_updates(
            {
                "character_updates": [{
                    "character_id": "invented_person",
                    "name": "Invented Person",
                    "evidence_fragment_ids": [0],
                    "discovery_confidence": 0.99,
                }],
                "lines": [{"id": 0, "speaker": "invented_person"}],
            },
            fragments,
            registry,
        )
        self.assertNotIn("invented_person", registry.characters)

    def test_joint_registry_reconciliation_returns_deterministic_speaker_remap(self) -> None:
        analyzer = CharacterAnalyzer(FakeCharacterOllama())
        registry = CharacterRegistry(
            book_title="Book",
            book_author="Author",
            characters={
                "narrator": Character(
                    id="narrator",
                    name="Narrator",
                    gender=Gender.OTHER,
                    age_range="adult",
                    voice_description="neutral voice",
                ),
                "dusk": Character(
                    id="dusk",
                    name="Dusk",
                    gender=Gender.MALE,
                    age_range="adult",
                    voice_description="measured voice",
                    discovered_in_pass2=True,
                    discovery_confidence=0.9,
                ),
                "sixth_of_dusk": Character(
                    id="sixth_of_dusk",
                    name="Sixth of Dusk",
                    aliases=["Dusk"],
                    gender=Gender.MALE,
                    age_range="adult",
                    voice_description="low measured voice",
                    discovered_in_pass2=True,
                    discovery_confidence=0.95,
                ),
            },
        )
        book = ExtractedBook(
            metadata=BookMetadata(title="Book", author="Author", total_chapters=1),
            chapters=[
                ExtractedChapter(
                    number=1,
                    title="One",
                    text="Sixth of Dusk listened.",
                    word_count=4,
                )
            ],
        )

        reconciled, remap = analyzer.finalize_joint_registry(registry, book)

        self.assertEqual(remap["dusk"], "sixth_of_dusk")
        self.assertEqual(remap["sixth_of_dusk"], "sixth_of_dusk")
        self.assertNotIn("dusk", reconciled.characters)

    def test_character_suffixes_are_not_merged_without_explicit_alias(self) -> None:
        characters = {
            "king": {"name": "King", "aliases": [], "dialogue_count": 1},
            "red_king": {
                "name": "Red King",
                "aliases": [],
                "dialogue_count": 2,
            },
        }
        consolidated = CharacterAnalyzer._consolidate_accumulated_characters(
            characters
        )
        self.assertEqual(set(consolidated), {"king", "red_king"})

    def test_explicit_aliases_are_consolidated(self) -> None:
        characters = {
            "dusk": {"name": "Dusk", "aliases": [], "dialogue_count": 1},
            "sixth_of_dusk": {
                "name": "Sixth of Dusk",
                "aliases": ["Dusk"],
                "dialogue_count": 2,
            },
        }
        consolidated = CharacterAnalyzer._consolidate_accumulated_characters(
            characters
        )
        self.assertEqual(set(consolidated), {"sixth_of_dusk"})
        self.assertEqual(consolidated["sixth_of_dusk"]["dialogue_count"], 3)

    def test_name_candidate_requires_verbatim_book_evidence(self) -> None:
        evidence = "Dusk was formally known as the Sixth of Dusk."
        book = ExtractedBook(
            metadata=BookMetadata(title="Book", author="Author", total_chapters=1),
            chapters=[
                ExtractedChapter(
                    number=1,
                    title="One",
                    text=evidence,
                    word_count=len(evidence.split()),
                )
            ],
        )
        characters = {
            "dusk": {"name": "Dusk", "aliases": [], "dialogue_count": 1},
            "sixth_of_dusk": {
                "name": "Sixth of Dusk",
                "aliases": [],
                "dialogue_count": 2,
            },
        }
        analyzer = CharacterAnalyzer(FakeIdentityOllama(evidence))
        consolidated = analyzer._adjudicate_name_candidates(characters, book)
        self.assertEqual(set(consolidated), {"sixth_of_dusk"})

        unsupported = CharacterAnalyzer(
            FakeIdentityOllama("The model invented this evidence.")
        )._adjudicate_name_candidates(
            {
                "dusk": {"name": "Dusk", "aliases": [], "dialogue_count": 1},
                "sixth_of_dusk": {
                    "name": "Sixth of Dusk",
                    "aliases": [],
                    "dialogue_count": 2,
                },
            },
            book,
        )
        self.assertEqual(set(unsupported), {"dusk", "sixth_of_dusk"})

    def test_positive_adjudication_accepts_source_backed_short_name_continuation(self) -> None:
        source = (
            "Sixth of the Dusk crept up on a deathant. "
            '"Its venom is deadly," Dusk whispered.'
        )
        book = ExtractedBook(
            metadata=BookMetadata(title="Book", author="Author", total_chapters=1),
            chapters=[
                ExtractedChapter(
                    number=1,
                    title="One",
                    text=source,
                    word_count=len(source.split()),
                )
            ],
        )
        characters = {
            "dusk": {"name": "Dusk", "aliases": [], "dialogue_count": 1},
            "sixth_of_dusk": {
                "name": "Sixth of Dusk",
                "aliases": [],
                "dialogue_count": 2,
            },
        }
        consolidated = CharacterAnalyzer(
            FakeIdentityOllama("Citation formatting was not verbatim.")
        )._adjudicate_name_candidates(characters, book)
        self.assertEqual(set(consolidated), {"sixth_of_dusk"})

    def test_short_name_continuation_still_requires_positive_adjudication(self) -> None:
        source = "The Red King entered. The King objected from across the room."
        book = ExtractedBook(
            metadata=BookMetadata(title="Book", author="Author", total_chapters=1),
            chapters=[
                ExtractedChapter(
                    number=1,
                    title="One",
                    text=source,
                    word_count=len(source.split()),
                )
            ],
        )
        characters = {
            "king": {"name": "King", "aliases": [], "dialogue_count": 1},
            "red_king": {
                "name": "Red King",
                "aliases": [],
                "dialogue_count": 2,
            },
        }
        consolidated = CharacterAnalyzer(
            FakeIdentityOllama("", same_character=False)
        )._adjudicate_name_candidates(characters, book)
        self.assertEqual(set(consolidated), {"king", "red_king"})

    def test_distinctive_token_overlap_candidate_adjudication(self) -> None:
        evidence = "The Drominadian smiled. 'What a wonderful decision, Sixth!'"
        book = ExtractedBook(
            metadata=BookMetadata(title="Book", author="Author", total_chapters=1),
            chapters=[
                ExtractedChapter(
                    number=1,
                    title="One",
                    text=evidence,
                    word_count=len(evidence.split()),
                )
            ],
        )
        characters = {
            "dusk": {
                "name": "Sixth of the Dusk",
                "aliases": ["Sixth", "Dusk"],
                "dialogue_count": 393,
            },
            "drominadian": {
                "name": "Drominadian",
                "aliases": ["Sixth"],
                "dialogue_count": 16,
            },
        }

        class FakeDrominadianOllama:
            model = "fake"

            def generate_json(self, *args, **kwargs):
                return {
                    "decisions": [
                        {
                            "left_id": "drominadian",
                            "right_id": "dusk",
                            "same_character": True,
                            "evidence": "The Drominadian smiled. 'What a wonderful decision, Sixth!'",
                        }
                    ]
                }

        analyzer = CharacterAnalyzer(FakeDrominadianOllama())
        consolidated = analyzer._adjudicate_name_candidates(characters, book)
        self.assertEqual(set(consolidated), {"dusk"})
        self.assertEqual(consolidated["dusk"]["dialogue_count"], 409)
        self.assertIn("Drominadian", consolidated["dusk"]["aliases"])

    def test_pronoun_does_not_select_arbitrary_speaker(self) -> None:
        fragments = [
            SourceFragment(text='"Hello."', start=0, end=8),
            SourceFragment(text="She turned away.", start=9, end=25),
        ]
        self.assertEqual(
            ScriptGenerator._resolve_dialogue_speaker(
                0,
                fragments,
                {},
                {"narrator", "alice", "beth"},
            ),
            "narrator",
        )

    def test_fragment_spans_cover_multiple_dialogue_styles(self) -> None:
        source = (
            'He said, "Hello." She left.\n'
            "— Wait! she called.\n"
            "It’s fine. ‘Really?’ he asked."
        )
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        self.assertEqual(
            "".join("".join(fragment.text.split()) for fragment in fragments),
            "".join(source.split()),
        )
        self.assertTrue(
            any(
                ScriptGenerator._is_dialogue_fragment(fragment.text)
                for fragment in fragments
            )
        )

    def test_script_coverage_detects_omission(self) -> None:
        source = "One sentence. Another sentence."
        chapter = ScriptChapter(
            chapter_number=1,
            chapter_title="One",
            lines=[
                ScriptLine(
                    line_id="ch01_0000",
                    speaker="narrator",
                    text="One sentence.",
                )
            ],
        )
        with self.assertRaises(ValueError):
            assert_script_covers_source(chapter, source)

    def test_metadata_ids_must_be_complete_and_unique(self) -> None:
        with self.assertRaises(ValueError):
            ScriptGenerator._validate_metadata_ids(
                {"lines": [{"id": 0}, {"id": 0}]},
                2,
            )

    def test_unknown_dialogue_speaker_is_rejected_before_parse(self) -> None:
        fragments = [
            SourceFragment(text='"Hello!"', start=0, end=8),
            SourceFragment(text="the child said.", start=9, end=24),
        ]
        raw = {
            "lines": [
                {"id": 0, "speaker": "child"},
                {"id": 1, "speaker": "child"},
            ]
        }
        with self.assertRaisesRegex(ValueError, "unknown speaker 'child'"):
            ScriptGenerator._validate_metadata_speakers(
                raw,
                fragments,
                {"narrator", "frond"},
            )

    def test_low_confidence_dialogue_speaker_is_rejected_for_correction(self) -> None:
        fragment = SourceFragment(text='"Hello."', start=0, end=8)
        with self.assertRaisesRegex(ValueError, "low confidence"):
            ScriptGenerator._validate_metadata_speakers(
                {
                    "lines": [
                        {
                            "id": 0,
                            "speaker": "speaker",
                            "speaker_confidence": 0.2,
                            "speaker_evidence": "The local exchange indicates speaker.",
                        }
                    ]
                },
                [fragment],
                {"narrator", "speaker"},
                confidence_threshold=0.55,
            )

    def test_unknown_speaker_on_narration_is_ignored(self) -> None:
        fragments = [
            SourceFragment(text="the child said.", start=0, end=15),
        ]
        ScriptGenerator._validate_metadata_speakers(
            {"lines": [{"id": 0, "speaker": "child"}]},
            fragments,
            {"narrator"},
        )

    def test_dialogue_tag_gender_contradiction_is_rejected(self) -> None:
        source = '"You\'re bored, I suppose," she said. Then she paused.'
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        registry = CharacterRegistry(
            book_title="The Blind Owl",
            book_author="Test",
            characters={
                "dusk": Character(
                    id="dusk",
                    name="Dusk",
                    gender=Gender.MALE,
                    age_range="adult",
                    voice_description="low voice",
                ),
                "vathi": Character(
                    id="vathi",
                    name="Vathi",
                    gender=Gender.FEMALE,
                    age_range="adult",
                    voice_description="clear voice",
                ),
            },
        )
        raw = {
            "lines": [
                {
                    "id": index,
                    "speaker": "dusk" if index == 0 else "narrator",
                    "speaker_confidence": 0.95,
                    "speaker_evidence": (
                        "The attached pronoun tag identifies the speaker."
                        if index == 0
                        else ""
                    ),
                }
                for index in range(len(fragments))
            ]
        }

        with self.assertRaisesRegex(ValueError, "female.*pronouns"):
            ScriptGenerator._validate_metadata_speakers(
                raw,
                fragments,
                {"narrator", "dusk", "vathi"},
                registry=registry,
            )

        raw["lines"][0]["speaker"] = "vathi"
        ScriptGenerator._validate_metadata_speakers(
            raw,
            fragments,
            {"narrator", "dusk", "vathi"},
            registry=registry,
        )

    def test_explicit_named_dialogue_tag_contradiction_is_rejected(self) -> None:
        source = '"Wait," Vathi quietly said.'
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        registry = CharacterRegistry(
            book_title="Test",
            book_author="Author",
            characters={
                character_id: Character(
                    id=character_id,
                    name=character_id.title(),
                    gender=gender,
                    age_range="adult",
                    voice_description="test voice",
                )
                for character_id, gender in (
                    ("dusk", Gender.MALE),
                    ("vathi", Gender.FEMALE),
                )
            },
        )

        with self.assertRaisesRegex(ValueError, "names 'vathi'"):
            ScriptGenerator._validate_metadata_speakers(
                {
                    "lines": [
                        {"id": 0, "speaker": "dusk", "speaker_confidence": 0.95},
                        {"id": 1, "speaker": "narrator"},
                    ]
                },
                fragments,
                {"narrator", "dusk", "vathi"},
                registry=registry,
            )

    def test_gendered_noun_dialogue_tag_contradiction_is_rejected(self) -> None:
        registry = CharacterRegistry(
            book_title="Test",
            book_author="Author",
            characters={
                "child_female": Character(
                    id="child_female",
                    name="Girl",
                    gender=Gender.FEMALE,
                    age_range="child",
                    voice_description="young female voice",
                )
            },
        )

        with self.assertRaisesRegex(ValueError, "male speaker"):
            ScriptGenerator._validate_dialogue_tag_attribution(
                "child_female",
                "the boy said, folding his arms.",
                registry,
                54,
            )

    @staticmethod
    def _attribution_registry() -> CharacterRegistry:
        return CharacterRegistry(
            book_title="Test",
            book_author="Author",
            characters={
                "narrator": Character(
                    id="narrator",
                    name="Narrator",
                    gender=Gender.OTHER,
                    age_range="adult",
                    voice_description="neutral voice",
                ),
                "dusk": Character(
                    id="dusk",
                    name="Dusk",
                    gender=Gender.MALE,
                    age_range="adult",
                    voice_description="low voice",
                ),
                "vathi": Character(
                    id="vathi",
                    name="Vathi",
                    gender=Gender.FEMALE,
                    age_range="adult",
                    voice_description="clear voice",
                ),
                "child_male": Character(
                    id="child_male",
                    name="Boy",
                    gender=Gender.MALE,
                    age_range="child",
                    voice_description="young male voice",
                ),
                "child_female": Character(
                    id="child_female",
                    name="Girl",
                    gender=Gender.FEMALE,
                    age_range="child",
                    voice_description="young female voice",
                ),
            },
        )

    @staticmethod
    def _metadata_response(
        fragments: list[SourceFragment],
        dialogue_speaker: str,
        *,
        confidence: float = 0.95,
    ) -> dict:
        return {
            "chapter_number": 1,
            "chapter_title": "One",
            "chapter_summary": "A short exchange.",
            "lines": [
                {
                    "id": index,
                    "speaker": (
                        dialogue_speaker
                        if ScriptGenerator._is_dialogue_fragment(fragment.text)
                        else "narrator"
                    ),
                    "speaker_confidence": confidence,
                    "speaker_evidence": "test evidence",
                    "dialogue_kind": (
                        "spoken"
                        if ScriptGenerator._is_dialogue_fragment(fragment.text)
                        else None
                    ),
                    "emotion": "neutral",
                    "speed": 1.0,
                    "pause_before_ms": 0,
                    "pause_after_ms": 300,
                }
                for index, fragment in enumerate(fragments)
            ],
        }

    def test_compact_metadata_expands_to_quality_equivalent_canonical_rows(self) -> None:
        fragments = [
            SourceFragment("The room was still.", 0, 19),
            SourceFragment('"Go."', 20, 25),
            SourceFragment("The night settled.", 26, 44),
        ]
        compact = {
            "chapter_number": 1,
            "chapter_title": "One",
            "chapter_summary": "A short exchange.",
            "lines": [
                {"id": 0, "scene_index": 0, "emotion": "neutral", "speed": 1.0},
                {
                    "id": 1,
                    "speaker": "dusk",
                    "speaker_confidence": 0.96,
                    "speaker_evidence": "An explicit local cue identifies Dusk.",
                    "emotion": "angry demand",
                    "speed": 1.2,
                },
                {
                    "id": 2,
                    "scene_index": 1,
                    "emotion": "somber reflection",
                    "speed": 0.87,
                },
            ],
        }
        verbose = {
            **{key: value for key, value in compact.items() if key != "lines"},
            "lines": [
                {
                    "id": 0,
                    "scene_index": 0,
                    "speaker": "narrator",
                    "speaker_confidence": None,
                    "speaker_evidence": "",
                    "dialogue_kind": None,
                    "emotion": "neutral",
                    "speed": 1.0,
                    "pause_before_ms": 0,
                    "pause_after_ms": 500,
                },
                {
                    "id": 1,
                    "scene_index": 0,
                    "speaker": "dusk",
                    "speaker_confidence": 0.96,
                    "speaker_evidence": "An explicit local cue identifies Dusk.",
                    "dialogue_kind": "spoken",
                    "emotion": "angry demand",
                    "speed": 1.2,
                    "pause_before_ms": 0,
                    "pause_after_ms": 250,
                },
                {
                    "id": 2,
                    "scene_index": 1,
                    "speaker": "narrator",
                    "speaker_confidence": None,
                    "speaker_evidence": "",
                    "dialogue_kind": None,
                    "emotion": "somber reflection",
                    "speed": 0.87,
                    "pause_before_ms": 0,
                    "pause_after_ms": 700,
                },
            ],
        }
        received_size = len(json.dumps(compact, separators=(",", ":")))

        stats = ScriptGenerator._expand_compact_metadata(compact, fragments)

        self.assertEqual(compact, verbose)
        self.assertEqual(stats["received_characters"], received_size)
        self.assertGreater(stats["character_savings_ratio"], 0.3)
        parsed = ScriptGenerator._parse_script_chapter(
            compact,
            1,
            "One",
            fragments,
            allowed_speakers={"narrator", "dusk"},
            registry=self._attribution_registry(),
        )
        self.assertEqual(parsed.lines[1].speaker, "dusk")
        self.assertEqual(parsed.lines[1].speaker_confidence, 0.96)
        self.assertEqual(parsed.lines[1].dialogue_kind, "spoken")
        self.assertEqual(parsed.lines[1].emotion, "angry demand")
        self.assertEqual(parsed.lines[1].speed, 1.2)
        self.assertEqual(parsed.lines[1].pause_after_ms, 250)

    def test_compact_metadata_refuses_missing_creative_delivery_fields(self) -> None:
        fragments = [SourceFragment("A quiet room.", 0, 13)]
        with self.assertRaisesRegex(ValueError, "required emotion"):
            ScriptGenerator._expand_compact_metadata(
                {"lines": [{"id": 0, "speed": 1.0}]},
                fragments,
            )

    def test_compact_metadata_inherits_explicit_scene_narration_defaults(self) -> None:
        fragments = [SourceFragment("A quiet room.", 0, 13)]
        raw = {
            "scenes": [{
                "narrator_emotion": "hushed suspense",
                "narrator_pace": 0.92,
            }],
            "lines": [{"id": 0, "scene_index": 0}],
        }

        issues = ScriptGenerator._collect_delivery_metadata_issues(raw, fragments)
        self.assertEqual(issues, [])
        stats = ScriptGenerator._expand_compact_metadata(raw, fragments)

        self.assertEqual(raw["lines"][0]["emotion"], "hushed suspense")
        self.assertEqual(raw["lines"][0]["speed"], 0.92)
        self.assertGreater(stats["character_savings_ratio"], 0.0)

    def test_dialogue_focused_schema_reconstructs_only_omitted_narration(self) -> None:
        fragments = [
            SourceFragment("The room was still.", 0, 19),
            SourceFragment('"Go."', 20, 25),
            SourceFragment("The night settled.", 26, 44),
        ]
        raw = {
            "scenes": [
                {
                    "start_id": 0,
                    "narrator_emotion": "neutral",
                    "narrator_pace": 1.0,
                },
                {
                    "start_id": 2,
                    "narrator_emotion": "somber reflection",
                    "narrator_pace": 0.87,
                },
            ],
            "lines": [{
                "id": 1,
                "speaker": "dusk",
                "speaker_confidence": 0.96,
                "speaker_evidence": "An explicit local cue identifies Dusk.",
                "emotion": "angry demand",
                "speed": 1.2,
            }],
        }

        sparse = ScriptGenerator._inflate_dialogue_focused_metadata(raw, fragments)
        self.assertEqual(sparse["received_rows"], 1)
        self.assertEqual(sparse["synthesized_narration_rows"], 2)
        self.assertEqual([row["id"] for row in raw["lines"]], [0, 1, 2])
        self.assertEqual([row["scene_index"] for row in raw["lines"]], [0, 0, 1])
        self.assertNotIn("start_id", raw["scenes"][0])
        self.assertEqual(
            ScriptGenerator._collect_delivery_metadata_issues(raw, fragments),
            [],
        )
        ScriptGenerator._expand_compact_metadata(raw, fragments)
        self.assertEqual(raw["lines"][0]["speaker"], "narrator")
        self.assertEqual(raw["lines"][2]["emotion"], "somber reflection")

    def test_dialogue_focused_schema_refuses_missing_dialogue(self) -> None:
        fragments = [SourceFragment('"Go."', 0, 5)]
        raw = {
            "scenes": [{
                "start_id": 0,
                "narrator_emotion": "neutral",
                "narrator_pace": 1.0,
            }],
            "lines": [],
        }
        with self.assertRaisesRegex(ValueError, "mandatory dialogue IDs"):
            ScriptGenerator._inflate_dialogue_focused_metadata(raw, fragments)

    def test_dialogue_focused_model_response_expands_before_validation(self) -> None:
        source = "A quiet room remained still."
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        response = {
            "chapter_number": 1,
            "chapter_title": "One",
            "chapter_summary": "A quiet moment.",
            "scenes": [{
                "start_id": 0,
                "mood": "quiet",
                "tension": "low",
                "narrator_emotion": "somber reflection",
                "narrator_pace": 0.87,
                "character_state": "calm",
                "transition_intent": "continue",
            }],
            "lines": [],
        }
        ollama = FakeScriptOllama([response])
        generator = ScriptGenerator(
            ollama=ollama,
            group_utterances=False,
            dialogue_focused_schema=True,
        )

        script = generator._process_fragments(
            fragments,
            1,
            "One",
            self._attribution_registry(),
            "",
        )

        self.assertTrue(all(line.speaker == "narrator" for line in script.lines))
        self.assertTrue(all(line.emotion == "somber reflection" for line in script.lines))
        self.assertIn("Dialogue-Focused Output Schema v5", ollama.calls[0][1]["system"])
        sparse = generator.call_metrics[-1]["requests"][0][
            "dialogue_focused_metadata"
        ]
        self.assertEqual(sparse["received_rows"], 0)
        self.assertEqual(sparse["canonical_rows"], len(fragments))

    def test_dialogue_cannot_inherit_narrator_scene_delivery(self) -> None:
        fragments = [SourceFragment('"Go."', 0, 5)]
        raw = {
            "scenes": [{
                "narrator_emotion": "neutral",
                "narrator_pace": 1.0,
            }],
            "lines": [{"id": 0, "scene_index": 0, "speaker": "dusk"}],
        }
        fields = ScriptGenerator._collect_delivery_metadata_issues(
            raw, fragments
        )[0].fields
        self.assertEqual(fields, ("emotion", "speed"))
        with self.assertRaisesRegex(ValueError, "required speed"):
            ScriptGenerator._expand_compact_metadata(
                {"lines": [{"id": 0, "emotion": "neutral"}]},
                fragments,
            )

    def test_compact_scene_carry_forward_uses_fragment_id_order(self) -> None:
        fragments = [
            SourceFragment("First.", 0, 6),
            SourceFragment("Second.", 7, 14),
            SourceFragment("Third.", 15, 21),
        ]
        raw = {
            "lines": [
                {"id": 2, "scene_index": 1, "emotion": "tense", "speed": 1.0},
                {"id": 0, "scene_index": 0, "emotion": "neutral", "speed": 1.0},
                {"id": 1, "emotion": "neutral", "speed": 1.0},
            ]
        }

        ScriptGenerator._expand_compact_metadata(raw, fragments)

        by_id = {row["id"]: row for row in raw["lines"]}
        self.assertEqual(by_id[0]["scene_index"], 0)
        self.assertEqual(by_id[1]["scene_index"], 0)
        self.assertEqual(by_id[2]["scene_index"], 1)

    def test_compact_model_response_preserves_delivery_and_records_savings(self) -> None:
        source = "A quiet room remained still."
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        response = {
            "chapter_number": 1,
            "chapter_title": "One",
            "chapter_summary": "A quiet moment.",
            "scenes": [],
            "lines": [
                {
                    "id": index,
                    "emotion": "somber reflection",
                    "speed": 0.87,
                    **({"scene_index": 0} if index == 0 else {}),
                }
                for index, _fragment in enumerate(fragments)
            ],
        }
        ollama = FakeScriptOllama([response])
        generator = ScriptGenerator(ollama=ollama, group_utterances=False)

        script = generator._process_fragments(
            fragments,
            1,
            "One",
            self._attribution_registry(),
            "",
        )

        self.assertTrue(all(line.speaker == "narrator" for line in script.lines))
        self.assertTrue(all(line.emotion == "somber reflection" for line in script.lines))
        self.assertTrue(all(line.speed == 0.87 for line in script.lines))
        self.assertTrue(all(line.pause_after_ms == 700 for line in script.lines))
        system_prompt = ollama.calls[0][1]["system"]
        user_prompt = ollama.calls[0][0]
        self.assertIn("Quality is the primary requirement", system_prompt)
        self.assertIn("MINIFIED VALID JSON", user_prompt)
        compact_metric = generator.call_metrics[-1]["requests"][0][
            "compact_metadata"
        ]
        self.assertGreater(compact_metric["character_savings_ratio"], 0.25)

    def test_prompt_lists_dialogue_ids_with_mandatory_delivery(self) -> None:
        source = '"Go," Dusk said.'
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        dialogue_ids = [
            index for index, fragment in enumerate(fragments)
            if ScriptGenerator._is_dialogue_fragment(fragment.text)
        ]
        response = {
            "chapter_number": 1,
            "chapter_title": "One",
            "chapter_summary": "A brief exchange.",
            "scenes": [],
            "lines": [
                {
                    "id": index,
                    "emotion": "firm",
                    "speed": 1.0,
                    **(
                        {
                            "speaker": "dusk",
                            "speaker_confidence": 0.99,
                            "speaker_evidence": "The adjacent named tag identifies Dusk.",
                        }
                        if index in dialogue_ids else {}
                    ),
                }
                for index, _fragment in enumerate(fragments)
            ],
        }
        ollama = FakeScriptOllama([response])
        generator = ScriptGenerator(ollama=ollama, group_utterances=False)

        generator._process_fragments(
            fragments, 1, "One", self._attribution_registry(), ""
        )

        prompt = ollama.calls[0][0]
        self.assertIn("MANDATORY DIALOGUE DELIVERY IDS", prompt)
        self.assertIn(json.dumps(dialogue_ids), prompt)

    def test_missing_compact_speed_uses_focused_delivery_repair(self) -> None:
        source = "A quiet room remained still."
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        initial = {
            "chapter_number": 1,
            "chapter_title": "One",
            "chapter_summary": "A quiet moment.",
            "lines": [
                {"id": index, "emotion": "somber reflection"}
                for index, _fragment in enumerate(fragments)
            ],
        }
        repair = {
            "lines": [
                {"id": index, "speed": 0.88}
                for index, _fragment in enumerate(fragments)
            ]
        }
        ollama = FakeScriptOllama([initial, repair])
        generator = ScriptGenerator(ollama=ollama, group_utterances=False)

        script = generator._process_fragments(
            fragments,
            1,
            "One",
            self._attribution_registry(),
            "",
        )

        self.assertTrue(all(line.emotion == "somber reflection" for line in script.lines))
        self.assertTrue(all(line.speed == 0.88 for line in script.lines))
        metric = generator.call_metrics[-1]
        self.assertEqual(metric["full_attempts"], 1)
        self.assertEqual(metric["structural_retries"], 0)
        self.assertEqual(metric["delivery_focused_rounds"], 1)
        self.assertEqual(metric["delivery_focused_retries"], len(fragments))
        self.assertEqual(
            [item["request_kind"] for item in metric["requests"]],
            ["full_chunk", "focused_delivery_batch"],
        )

    def test_narrator_spoken_dialogue_gets_one_strict_focused_retry(self) -> None:
        source = '"Wait."'
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        initial = self._metadata_response(fragments, "narrator")
        initial["lines"][0]["dialogue_kind"] = "spoken"
        ordinary_retry = {
            "lines": [
                {
                    "id": 0,
                    "speaker": "narrator",
                    "speaker_confidence": 0.7,
                    "speaker_evidence": "The turn remains ambiguous in isolation.",
                    "dialogue_kind": "spoken",
                }
            ]
        }
        strict_retry = {
            "lines": [
                {
                    "id": 0,
                    "speaker": "vathi",
                    "speaker_confidence": 0.62,
                    "speaker_evidence": "Local turn continuity best supports Vathi.",
                    "dialogue_kind": "spoken",
                }
            ]
        }
        ollama = FakeScriptOllama([initial, ordinary_retry, strict_retry])
        generator = ScriptGenerator(ollama=ollama, group_utterances=False)

        script = generator._process_fragments(
            fragments,
            1,
            "One",
            self._attribution_registry(),
            "",
        )

        self.assertEqual(script.lines[0].speaker, "vathi")
        metric = generator.call_metrics[-1]
        self.assertEqual(metric["full_attempts"], 1)
        self.assertEqual(metric["strict_attribution_retries"], 1)
        self.assertEqual(metric["fragment_fallbacks"], 0)
        self.assertEqual(
            [item["request_kind"] for item in metric["requests"]],
            ["full_chunk", "focused_fragment", "strict_spoken_attribution"],
        )
        self.assertIn("'narrator'", ollama.calls[2][0])
        self.assertIn("forbidden", ollama.calls[2][0])

    def test_spoken_dialogue_requires_meaningful_speaker_evidence(self) -> None:
        fragment = SourceFragment(text='"Hello."', start=0, end=8)
        with self.assertRaisesRegex(ValueError, "source-grounded"):
            ScriptGenerator._validate_metadata_speakers(
                {
                    "lines": [
                        {
                            "id": 0,
                            "speaker": "speaker",
                            "speaker_confidence": 0.95,
                            "speaker_evidence": "cue",
                            "dialogue_kind": "spoken",
                        }
                    ]
                },
                [fragment],
                {"narrator", "speaker"},
            )

    def test_named_tag_is_repaired_without_another_full_model_call(self) -> None:
        source = '"Wait," Vathi quietly said.'
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        initial = self._metadata_response(fragments, "dusk")
        ollama = FakeScriptOllama([initial])
        generator = ScriptGenerator(ollama=ollama, group_utterances=False)

        script = generator._process_fragments(
            fragments,
            1,
            "One",
            self._attribution_registry(),
            "",
            0,
        )

        self.assertEqual(script.lines[0].speaker, "vathi")
        self.assertEqual(len(ollama.calls), 1)
        self.assertEqual(generator.call_metrics[-1]["full_attempts"], 1)
        self.assertEqual(generator.call_metrics[-1]["local_repairs"], 1)
        self.assertEqual(generator.call_metrics[-1]["focused_retries"], 0)
        self.assertEqual(generator.call_metrics[-1]["full_semantic_retries"], 0)
        self.assertEqual(
            [
                item["request_kind"]
                for item in generator.call_metrics[-1]["requests"]
            ],
            ["full_chunk"],
        )
        assert_script_covers_source(script, source)

    def test_generic_boy_tag_repairs_child_female_to_child_male(self) -> None:
        source = '"I followed the star," the boy said, folding his arms.'
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        initial = self._metadata_response(fragments, "child_female")
        ollama = FakeScriptOllama([initial])
        generator = ScriptGenerator(ollama=ollama, group_utterances=False)

        script = generator._process_fragments(
            fragments,
            1,
            "One",
            self._attribution_registry(),
            "",
            0,
        )

        self.assertEqual(script.lines[0].speaker, "child_male")
        self.assertEqual(len(ollama.calls), 1)
        self.assertEqual(generator.call_metrics[-1]["local_repairs"], 1)

    def test_ambiguous_pronoun_uses_one_fragment_focused_retry(self) -> None:
        source = '"Wait," she said.'
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        initial = self._metadata_response(fragments, "dusk")
        corrected = {
            "lines": [
                {
                    "id": 0,
                    "speaker": "vathi",
                    "speaker_confidence": 0.95,
                    "speaker_evidence": "Local conversation establishes Vathi.",
                    "emotion": "angry demand",
                    "speed": 1.25,
                }
            ]
        }
        ollama = FakeScriptOllama([initial, corrected])
        generator = ScriptGenerator(ollama=ollama, group_utterances=False)

        script = generator._process_fragments(
            fragments,
            1,
            "One",
            self._attribution_registry(),
            "",
            0,
        )

        self.assertEqual(script.lines[0].speaker, "vathi")
        self.assertEqual(script.lines[0].emotion, "neutral")
        self.assertEqual(script.lines[0].speed, 1.0)
        self.assertEqual(len(ollama.calls), 2)
        self.assertIn("listed suspect audiobook fragments", ollama.calls[1][0])
        metric = generator.call_metrics[-1]
        self.assertEqual(metric["full_attempts"], 1)
        self.assertEqual(metric["focused_retries"], 1)
        self.assertEqual(metric["fragment_fallbacks"], 0)
        self.assertEqual(
            [item["request_kind"] for item in metric["requests"]],
            ["full_chunk", "focused_fragment"],
        )

    def test_focused_retry_accepts_chapter_global_id_and_canonicalizes_it(self) -> None:
        source = '"Wait," she said.'
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        initial = self._metadata_response(fragments, "dusk")
        corrected = {
            "lines": [
                {
                    "id": 40,
                    "speaker": "vathi",
                    "speaker_confidence": 0.95,
                    "speaker_evidence": "Local conversation establishes Vathi.",
                    "dialogue_kind": "spoken",
                }
            ]
        }
        ollama = FakeScriptOllama([initial, corrected])
        generator = ScriptGenerator(ollama=ollama, group_utterances=False)

        script = generator._process_fragments(
            fragments,
            1,
            "One",
            self._attribution_registry(),
            "",
            id_offset=40,
        )

        self.assertEqual(script.lines[0].speaker, "vathi")
        self.assertEqual(script.lines[0].source_fragment_id, 40)
        self.assertEqual(len(ollama.calls), 2)
        focused_prompt = ollama.calls[1][0]
        self.assertIn('"local_id": 0', focused_prompt)
        self.assertIn('"chapter_fragment_id": 40', focused_prompt)
        self.assertIn('"id":0', focused_prompt)

    def test_unresolved_attribution_uses_review_fallback_without_full_retry(self) -> None:
        source = '"Wait," she said.'
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        initial = self._metadata_response(fragments, "dusk")
        invalid_focused = {"lines": [{"id": 99, "speaker": "vathi"}]}
        ollama = FakeScriptOllama([initial, invalid_focused])
        generator = ScriptGenerator(ollama=ollama, group_utterances=False)

        script = generator._process_fragments(
            fragments,
            1,
            "One",
            self._attribution_registry(),
            "",
        )

        self.assertEqual(len(ollama.calls), 2)
        self.assertTrue(script.lines[0].attribution_review_required)
        self.assertLess(
            script.lines[0].speaker_confidence,
            generator.speaker_confidence_threshold,
        )
        metric = generator.call_metrics[-1]
        self.assertEqual(metric["full_attempts"], 1)
        self.assertEqual(metric["structural_retries"], 0)
        self.assertEqual(metric["fragment_fallbacks"], 1)
        self.assertEqual(
            [item["request_kind"] for item in metric["requests"]],
            ["full_chunk", "focused_fragment"],
        )

    def test_failed_focused_retry_blocks_unresolved_spoken_fragment(self) -> None:
        source = '"Wait," she said. The room fell silent.'
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        initial = self._metadata_response(fragments, "dusk")
        invalid_focused = {"lines": [{"id": 99, "speaker": "vathi"}]}
        ollama = FakeScriptOllama([initial, invalid_focused])
        generator = ScriptGenerator(ollama=ollama, group_utterances=False)

        with self.assertRaises(MetadataAttributionError) as raised:
            generator._process_fragments(
                fragments,
                1,
                "One",
                self._attribution_registry(),
                "",
                0,
                strict_validation=True,
            )

        self.assertTrue(
            any(
                issue.kind == "gender_contradiction"
                for issue in raised.exception.issues
            )
        )
        self.assertEqual(len(ollama.calls), 2)

    def test_selective_repair_batches_untagged_conversation_turns(self) -> None:
        source = '"Wait," she said. "Go," he replied.'
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        dialogue_indexes = [
            index
            for index, fragment in enumerate(fragments)
            if ScriptGenerator._is_dialogue_fragment(fragment.text)
        ]
        old_script = ScriptChapter(
            chapter_number=1,
            chapter_title="One",
            lines=[
                ScriptLine(
                    line_id=f"ch01_{index:04d}",
                    speaker="narrator",
                    speaker_confidence=0.95,
                    speaker_evidence="Legacy fallback metadata.",
                    text=fragment.text,
                    source_fragment_id=index,
                    source_fragment_ids=[index],
                    source_start=fragment.start,
                    source_end=fragment.end,
                )
                for index, fragment in enumerate(fragments)
            ],
        )
        response = {
            "lines": [
                {
                    "id": dialogue_indexes[0],
                    "speaker": "vathi",
                    "speaker_confidence": 0.98,
                    "speaker_evidence": "The attached she-said tag identifies Vathi.",
                    "dialogue_kind": "spoken",
                },
                {
                    "id": dialogue_indexes[1],
                    "speaker": "dusk",
                    "speaker_confidence": 0.98,
                    "speaker_evidence": "The alternating reply and he-replied tag identify Dusk.",
                    "dialogue_kind": "spoken",
                },
            ]
        }
        ollama = FakeScriptOllama([response])
        generator = ScriptGenerator(ollama=ollama, group_utterances=False)
        chapter = ExtractedChapter(
            number=1,
            title="One",
            text=source,
            word_count=len(source.split()),
        )

        repaired, metrics = generator.repair_chapter_attribution(
            chapter,
            old_script,
            self._attribution_registry(),
        )

        speakers = {
            line.source_fragment_id: line.speaker for line in repaired.lines
        }
        self.assertEqual(speakers[dialogue_indexes[0]], "vathi")
        self.assertEqual(speakers[dialogue_indexes[1]], "dusk")
        self.assertEqual(metrics["changed_fragments"], dialogue_indexes)
        self.assertEqual(len(ollama.calls), 1)
        self.assertEqual(
            metrics["requests"][0]["request_kind"],
            "focused_attribution_batch",
        )

    def test_action_only_beat_is_not_treated_as_a_dialogue_tag(self) -> None:
        self.assertFalse(ScriptGenerator._is_pure_dialogue_tag("He nodded slowly."))
        self.assertFalse(ScriptGenerator._is_pure_dialogue_tag("Vathi smiled."))
        self.assertTrue(
            ScriptGenerator._is_pure_dialogue_tag(
                "the boy said, folding his arms."
            )
        )

    def test_collective_reported_speech_is_repaired_without_model_retry(self) -> None:
        source = 'They asked, "Why?" They said, "We should explain it."'
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        dialogue_indexes = [
            index
            for index, fragment in enumerate(fragments)
            if ScriptGenerator._is_dialogue_fragment(fragment.text)
        ]
        initial = self._metadata_response(fragments, "dusk")
        initial["lines"][dialogue_indexes[0]]["speaker"] = "narrator"
        initial["lines"][dialogue_indexes[0]]["dialogue_kind"] = "spoken"
        ollama = FakeScriptOllama([initial])
        generator = ScriptGenerator(ollama=ollama, group_utterances=False)

        script = generator._process_fragments(
            fragments,
            1,
            "One",
            self._attribution_registry(),
            "",
            0,
        )

        repaired = [script.lines[index] for index in dialogue_indexes]
        self.assertTrue(all(line.speaker == "narrator" for line in repaired))
        self.assertTrue(
            all(
                line.dialogue_kind == "reported_collective_speech"
                for line in repaired
            )
        )
        self.assertEqual(len(ollama.calls), 1)
        self.assertEqual(generator.call_metrics[-1]["local_repairs"], 2)

    def test_narrator_quote_requires_explicit_non_spoken_classification(self) -> None:
        fragments = ScriptGenerator._split_into_fragment_spans('The sign read "STOP".')
        raw = self._metadata_response(fragments, "narrator")
        quote_index = next(
            index
            for index, fragment in enumerate(fragments)
            if ScriptGenerator._is_dialogue_fragment(fragment.text)
        )
        issues = ScriptGenerator._collect_metadata_speaker_issues(
            raw,
            fragments,
            set(self._attribution_registry().characters),
            registry=self._attribution_registry(),
        )
        self.assertEqual(issues[0].kind, "narrator_spoken_dialogue")

        raw["lines"][quote_index].update(
            {
                "dialogue_kind": "non_spoken_quote",
                "speaker_evidence": (
                    "The source explicitly says the word is written on a sign."
                ),
            }
        )
        self.assertEqual(
            ScriptGenerator._collect_metadata_speaker_issues(
                raw,
                fragments,
                set(self._attribution_registry().characters),
                registry=self._attribution_registry(),
            ),
            [],
        )

    def test_structural_metadata_failure_still_retries_full_chunk(self) -> None:
        source = '"Wait," Vathi said.'
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        invalid = self._metadata_response(fragments, "vathi")
        invalid["lines"] = [invalid["lines"][0], invalid["lines"][0]]
        valid = self._metadata_response(fragments, "vathi")
        ollama = FakeScriptOllama([invalid, valid])
        generator = ScriptGenerator(ollama=ollama, group_utterances=False)

        script = generator._process_fragments(
            fragments,
            1,
            "One",
            self._attribution_registry(),
            "",
            0,
        )

        self.assertEqual(script.lines[0].speaker, "vathi")
        self.assertEqual(len(ollama.calls), 2)
        metric = generator.call_metrics[-1]
        self.assertEqual(metric["full_attempts"], 2)
        self.assertEqual(metric["structural_failures"], 1)
        self.assertEqual(metric["structural_retries"], 1)

    def test_generation_limit_skips_duplicate_full_chunk_retries(self) -> None:
        source = '"Wait," Vathi said.'
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        ollama = FakeScriptOllama([
            OllamaGenerationLimitError("repetition loop")
        ])
        generator = ScriptGenerator(ollama=ollama, group_utterances=False)

        script = generator._process_fragments(
            fragments,
            1,
            "One",
            self._attribution_registry(),
            "",
            0,
        )

        self.assertEqual(len(ollama.calls), 1)
        self.assertTrue(generator.call_metrics[-1]["used_fallback"])
        self.assertTrue(script.lines)

    def test_generation_limit_adaptively_splits_before_fallback(self) -> None:
        source = "One. Two. Three. Four. Five. Six. Seven. Eight."
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        generator = ScriptGenerator(
            ollama=None,
            group_utterances=False,
            adaptive_split_min_fragments=8,
        )
        split_index = generator._adaptive_fragment_split_index(fragments)
        ollama = FakeScriptOllama([
            OllamaGenerationLimitError("wall-clock limit"),
            self._metadata_response(fragments[:split_index], "vathi"),
            self._metadata_response(fragments[split_index:], "vathi"),
        ])
        generator.ollama = ollama

        script = generator._process_fragments(
            fragments,
            1,
            "One",
            self._attribution_registry(),
            "",
            chapter_text=source,
        )

        self.assertEqual(len(ollama.calls), 3)
        assert_script_covers_source(script, source)
        self.assertEqual(
            [line.source_fragment_id for line in script.lines],
            list(range(len(fragments))),
        )
        self.assertTrue(generator.call_metrics[0]["adaptive_split_triggered"])
        self.assertFalse(any(
            metric["used_fallback"] for metric in generator.call_metrics
        ))

    def test_adaptive_split_avoids_dialogue_tag_boundary(self) -> None:
        fragments = [
            SourceFragment("Before. ", 0, 8),
            SourceFragment('"Wait." ', 8, 16),
            SourceFragment("Vathi said. ", 16, 28),
            SourceFragment("After.", 28, 34),
        ]
        split_index = ScriptGenerator._adaptive_fragment_split_index(fragments)
        self.assertFalse(
            ScriptGenerator._is_dialogue_fragment(
                fragments[split_index - 1].text
            )
        )

    def test_explicit_boy_tag_adds_missing_male_child_role(self) -> None:
        source = '"I followed the star," the boy said, folding his arms.'
        book = ExtractedBook(
            metadata=BookMetadata(title="Book", author="Author", total_chapters=1),
            chapters=[
                ExtractedChapter(
                    number=1,
                    title="One",
                    text=source,
                    word_count=len(source.split()),
                )
            ],
        )
        analyzer = CharacterAnalyzer(FakeCharacterOllama())

        registry = analyzer.analyze(book)

        self.assertIn("child_male", registry.characters)
        self.assertEqual(registry.characters["child_male"].gender, Gender.MALE)
        self.assertEqual(registry.characters["child_male"].voice_id, "child_male")
        self.assertEqual(registry.characters["child_male"].dialogue_count, 1)

    def test_reference_augments_only_existing_speakers_with_verbatim_evidence(self) -> None:
        evidence = "Mara, a seasoned captain, stays calm under fire."
        description = (
            "female speaker, adult age, medium pitch, moderate volume, measured speed, "
            "neutral English accent, clear texture and high clarity, natural fluency, "
            "warm emotion, decisive tone, pragmatic personality."
        )
        ollama = FakeReferenceAugmentationOllama({"patches": [
            {
                "character_id": "mara", "confidence": 0.97,
                "evidence": evidence, "revised_voice_description": description,
                "personality_traits": ["calm", "pragmatic"],
                "aliases": ["Captain Mara"], "speaking_style": "Measured command speech",
            },
            {
                "character_id": "glossary_only_person", "confidence": 0.99,
                "evidence": evidence, "revised_voice_description": description,
                "personality_traits": [], "aliases": [], "speaking_style": "",
            },
        ]})
        book = ExtractedBook(
            metadata=BookMetadata(title="Book", author="Author", total_chapters=1),
            chapters=[ExtractedChapter(
                number=1, title="One", text='"Steady," Mara said.', word_count=3,
            )],
            reference_material={"Cast": evidence + " She is also called Captain Mara."},
        )
        with TemporaryDirectory() as temporary:
            audit_path = Path(temporary) / "reference-audit.json"
            registry = CharacterAnalyzer(ollama).analyze(
                book, reference_audit_path=audit_path,
            )
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(set(registry.characters), {"mara", "narrator"})
        self.assertEqual(registry.characters["mara"].voice_description, description)
        self.assertIn("Captain Mara", registry.characters["mara"].aliases)
        self.assertEqual(len(audit["accepted"]), 1)
        self.assertEqual(audit["rejected"][0]["reason"], "unknown_or_unproven_character")
        self.assertNotIn("AUTHOR REFERENCE", ollama.calls[0][0])
        self.assertIn("AUTHOR REFERENCE", ollama.calls[1][0])

    def test_reference_augmentation_failure_preserves_narrative_registry(self) -> None:
        ollama = FakeReferenceAugmentationOllama(fail_augmentation=True)
        book = ExtractedBook(
            metadata=BookMetadata(title="Book", author="Author", total_chapters=1),
            chapters=[ExtractedChapter(
                number=1, title="One", text='"Steady," Mara said.', word_count=3,
            )],
            reference_material={"Cast": "Mara is the ship captain."},
        )
        with TemporaryDirectory() as temporary:
            audit_path = Path(temporary) / "reference-audit.json"
            registry = CharacterAnalyzer(ollama).analyze(
                book, reference_audit_path=audit_path,
            )
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertIn("mara", registry.characters)
        self.assertEqual(audit["status"], "unavailable")

    def test_voice_redesign_does_not_invalidate_script_fingerprint(self) -> None:
        chapter = ExtractedChapter(
            number=1,
            title="One",
            text='"Hello," Speaker said.',
        )
        registry = CharacterRegistry(
            book_title="Test",
            book_author="Author",
            characters={
                "speaker": Character(
                    id="speaker",
                    name="Speaker",
                    gender=Gender.FEMALE,
                    age_range="adult",
                    voice_description="bright soprano",
                    speaking_style="measured",
                )
            },
        )
        generator = ScriptGenerator(ollama=None)
        original = generator.chapter_fingerprint(chapter, registry)

        redesigned = registry.model_copy(deep=True)
        redesigned.characters["speaker"].voice_description = "low contralto"
        redesigned.characters["speaker"].voice_id = "alternate_voice"
        self.assertEqual(
            original,
            generator.chapter_fingerprint(chapter, redesigned),
        )

        attribution_changed = registry.model_copy(deep=True)
        attribution_changed.characters["speaker"].speaking_style = "rapid"
        self.assertNotEqual(
            original,
            generator.chapter_fingerprint(chapter, attribution_changed),
        )

    def test_scripting_policy_revision_invalidates_chapter_fingerprint(self) -> None:
        chapter = ExtractedChapter(
            number=1,
            title="One",
            text='"Hello," Speaker said.',
        )
        registry = CharacterRegistry(
            characters={
                "speaker": Character(
                    id="speaker",
                    name="Speaker",
                    gender=Gender.FEMALE,
                    age_range="adult",
                    voice_description="clear voice",
                    speaking_style="measured",
                )
            }
        )
        generator = ScriptGenerator(ollama=None)
        original = generator.chapter_fingerprint(chapter, registry)

        with patch(
            "brain.director.script_generator.JOINT_SCRIPT_ANALYSIS_REVISION",
            5,
        ):
            self.assertNotEqual(
                original,
                generator.chapter_fingerprint(chapter, registry),
            )

        with patch(
            "brain.director.script_generator.DIALOGUE_DELIVERY_POLICY_REVISION",
            2,
        ):
            self.assertNotEqual(
                original,
                generator.chapter_fingerprint(chapter, registry),
            )

        row_tuned = ScriptGenerator(
            ollama=None,
            max_fragments_per_chunk=20,
        )
        self.assertNotEqual(
            original,
            row_tuned.chapter_fingerprint(chapter, registry),
        )
        adaptive_tuned = ScriptGenerator(
            ollama=None,
            adaptive_split_max_depth=3,
        )
        self.assertNotEqual(
            original,
            adaptive_tuned.chapter_fingerprint(chapter, registry),
        )
        schema_v5 = ScriptGenerator(
            ollama=None,
            dialogue_focused_schema=True,
        )
        self.assertNotEqual(
            original,
            schema_v5.chapter_fingerprint(chapter, registry),
        )

    def test_unrelated_character_does_not_invalidate_chapter_fingerprint(self) -> None:
        chapter = ExtractedChapter(
            number=1,
            title="One",
            text='"Hello," Alice said.',
        )
        registry = CharacterRegistry(characters={
            "alice": Character(
                id="alice", name="Alice", gender=Gender.FEMALE,
                age_range="adult", voice_description="clear voice",
                speaking_style="measured",
            ),
        })
        generator = ScriptGenerator(ollama=None)
        original = generator.chapter_fingerprint(chapter, registry)
        expanded = registry.model_copy(deep=True)
        expanded.characters["bob"] = Character(
            id="bob", name="Bob", gender=Gender.MALE,
            age_range="adult", voice_description="low voice",
            speaking_style="brisk",
        )
        self.assertEqual(original, generator.chapter_fingerprint(chapter, expanded))
        expanded.characters["alice"].aliases.append("Captain Alice")
        self.assertNotEqual(original, generator.chapter_fingerprint(chapter, expanded))

    def test_chunked_processing_reuses_full_chapter_speaker_scope(self) -> None:
        generator = ScriptGenerator(
            ollama=None,
            chunk_size_words=10_000,
            max_fragments_per_chunk=1,
        )
        registry = CharacterRegistry(characters={
            "alice": Character(
                id="alice", name="Alice", gender=Gender.FEMALE,
                age_range="adult", voice_description="clear", speaking_style="measured",
            ),
            "bob": Character(
                id="bob", name="Bob", gender=Gender.MALE,
                age_range="adult", voice_description="low", speaking_style="brisk",
            ),
        })
        captured: list[set[str]] = []

        def fake_process(fragments, chapter_number, chapter_title, registry, previous_summary, **kwargs):
            captured.append(set(kwargs["allowed_speakers"]))
            return ScriptChapter(
                chapter_number=chapter_number,
                chapter_title=chapter_title,
                lines=[ScriptLine(
                    line_id=f"line-{len(captured)}",
                    speaker="narrator",
                    text=fragments[0].text,
                    source_start=fragments[0].start,
                    source_end=fragments[0].end,
                )],
            )

        generator._process_fragments = fake_process
        generator.generate_chapter_script(
            ExtractedChapter(
                number=1,
                title="One",
                text='Alice entered. "I am ready," she said.',
            ),
            registry,
            "",
        )
        self.assertGreater(len(captured), 1)
        self.assertTrue(all("alice" in speakers for speakers in captured))
        self.assertTrue(all("bob" not in speakers for speakers in captured))

    def test_fragment_chunks_have_an_independent_row_limit(self) -> None:
        generator = ScriptGenerator(
            ollama=None,
            chunk_size_words=10_000,
            max_fragments_per_chunk=3,
        )
        fragments = [
            SourceFragment(text=f"Sentence {index}.", start=index, end=index + 1)
            for index in range(8)
        ]
        chunks = generator._chunk_fragments(fragments)
        self.assertEqual([len(chunk) for chunk in chunks], [3, 3, 2])

    def test_manifest_keeps_one_entry_per_line(self) -> None:
        chapter = ScriptChapter(
            chapter_number=3,
            chapter_title="Three",
            lines=[
                ScriptLine(
                    line_id=f"ch03_{index:04d}",
                    speaker="narrator",
                    text=text,
                )
                for index, text in enumerate(("First.", "Second.", "Third."))
            ],
        )
        manifest = build_segment_manifest("book", chapter)
        self.assertEqual(
            [item["line_id"] for item in manifest["segments"]],
            ["ch03_0000", "ch03_0001", "ch03_0002"],
        )

    def test_generation_settings_invalidate_segment_manifest(self) -> None:
        chapter = ScriptChapter(
            chapter_number=1,
            chapter_title="One",
            lines=[
                ScriptLine(
                    line_id="ch01_0000",
                    speaker="narrator",
                    text="A sentence.",
                )
            ],
        )
        eager = build_segment_manifest(
            "book",
            chapter,
            {"tts": {"attn_implementation": "eager"}},
        )
        sdpa = build_segment_manifest(
            "book",
            chapter,
            {"tts": {"attn_implementation": "sdpa"}},
        )
        self.assertNotEqual(eager["dependency_hash"], sdpa["dependency_hash"])

    def test_spoken_text_invalidates_manifest_but_preserves_source_hash(self) -> None:
        original = ScriptChapter(
            chapter_number=1,
            chapter_title="One",
            lines=[
                ScriptLine(
                    line_id="ch01_0000",
                    speaker="narrator",
                    text="Patji arrived.",
                )
            ],
        )
        pronounced = original.model_copy(deep=True)
        pronounced.lines[0].spoken_text = "Pah-chee arrived."

        original_manifest = build_segment_manifest("book", original)
        pronounced_manifest = build_segment_manifest("book", pronounced)

        self.assertNotEqual(
            original_manifest["dependency_hash"],
            pronounced_manifest["dependency_hash"],
        )
        self.assertEqual(
            original_manifest["segments"][0]["text_hash"],
            pronounced_manifest["segments"][0]["text_hash"],
        )
        self.assertNotIn("spoken_text_hash", original_manifest["segments"][0])
        self.assertIn("spoken_text_hash", pronounced_manifest["segments"][0])

    def test_only_used_voice_reference_invalidates_segment_manifest(self) -> None:
        chapter = ScriptChapter(
            chapter_number=1,
            chapter_title="One",
            lines=[
                ScriptLine(
                    line_id="ch01_0000",
                    speaker="hero",
                    voice_id="hero_voice",
                    text="A sentence.",
                )
            ],
        )
        original = build_segment_manifest(
            "book",
            chapter,
            {
                "tts": {"model": "test"},
                "voice_reference_hashes": {
                    "hero_voice": "hero-v1",
                    "unused_voice": "unused-v1",
                },
            },
        )
        unused_changed = build_segment_manifest(
            "book",
            chapter,
            {
                "tts": {"model": "test"},
                "voice_reference_hashes": {
                    "hero_voice": "hero-v1",
                    "unused_voice": "unused-v2",
                },
            },
        )
        hero_changed = build_segment_manifest(
            "book",
            chapter,
            {
                "tts": {"model": "test"},
                "voice_reference_hashes": {
                    "hero_voice": "hero-v2",
                    "unused_voice": "unused-v1",
                },
            },
        )
        self.assertEqual(
            original["dependency_hash"],
            unused_changed["dependency_hash"],
        )
        self.assertNotEqual(
            original["dependency_hash"],
            hero_changed["dependency_hash"],
        )

    def test_adjacent_utterances_group_without_crossing_paragraphs(self) -> None:
        source = "One sentence. Two sentences.\n\nThree sentences."
        starts = [
            source.index("One"),
            source.index("Two"),
            source.index("Three"),
        ]
        texts = ["One sentence.", "Two sentences.", "Three sentences."]
        chapter = ScriptChapter(
            chapter_number=1,
            chapter_title="Grouped",
            lines=[
                ScriptLine(
                    line_id=f"ch01_{index:04d}",
                    speaker="narrator",
                    text=text,
                    source_fragment_id=index,
                    source_fragment_ids=[index],
                    source_start=starts[index],
                    source_end=starts[index] + len(text),
                )
                for index, text in enumerate(texts)
            ],
        )
        generator = ScriptGenerator(
            ollama=None,
            utterance_target_chars=100,
            utterance_max_words=20,
        )

        grouped = generator._group_adjacent_utterances(chapter, source)

        self.assertEqual(len(grouped.lines), 2)
        self.assertEqual(grouped.lines[0].text, "One sentence. Two sentences.")
        self.assertEqual(grouped.lines[0].source_fragment_ids, [0, 1])
        self.assertEqual(grouped.lines[1].text, "Three sentences.")
        assert_script_covers_source(grouped, source)

    def test_dialogue_tag_keeps_narrator_voice_in_tight_utterance_group(self) -> None:
        source = '"You\'re bored, I suppose," she said. Then she paused. "Listen."'
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        raw = {
            "chapter_summary": "Vathi challenges Dusk.",
            "lines": [
                {
                    "id": index,
                    "speaker": "vathi" if index in (0, 3) else "narrator",
                    "speaker_confidence": 0.95,
                    "speaker_evidence": "Vathi is the active speaker.",
                    "emotion": "controlled",
                    "speed": 1.0,
                    "pause_before_ms": 0,
                    "pause_after_ms": 300,
                }
                for index in range(len(fragments))
            ],
        }
        chapter = ScriptGenerator._parse_script_chapter(
            raw,
            8,
            "Eight",
            fragments,
            allowed_speakers={"narrator", "vathi"},
        )

        grouped = ScriptGenerator(ollama=None)._group_adjacent_utterances(
            chapter,
            source,
        )

        self.assertEqual([line.speaker for line in grouped.lines], ["vathi", "narrator", "vathi"])
        self.assertEqual(grouped.lines[1].text, "she said. Then she paused.")
        self.assertEqual(grouped.lines[0].utterance_group_id, "utterance_ch08_0000")
        self.assertEqual(grouped.lines[1].utterance_group_id, "utterance_ch08_0000")
        self.assertEqual(grouped.lines[0].pause_after_ms, 0)
        self.assertEqual(grouped.lines[1].pause_before_ms, 0)
        assert_script_covers_source(grouped, source)

    def test_adaptive_grouping_keeps_expressive_lines_shorter(self) -> None:
        texts = [
            "A measured sentence carries softly across the quiet room.",
            "Another measured sentence follows it without any interruption.",
            "A final measured sentence closes the thoughtful observation.",
        ]
        source = " ".join(texts)
        starts = [source.index(text) for text in texts]

        def chapter(emotion: str) -> ScriptChapter:
            return ScriptChapter(
                chapter_number=1,
                chapter_title="Adaptive",
                lines=[
                    ScriptLine(
                        line_id=f"ch01_{index:04d}",
                        speaker="narrator",
                        text=text,
                        emotion=emotion,
                        source_fragment_id=index,
                        source_fragment_ids=[index],
                        source_start=starts[index],
                        source_end=starts[index] + len(text),
                    )
                    for index, text in enumerate(texts)
                ],
            )

        generator = ScriptGenerator(
            ollama=None,
            utterance_target_chars=100,
            utterance_max_words=20,
            narrator_target_chars=220,
            narrator_max_words=40,
            expressive_target_chars=100,
            expressive_max_words=20,
        )
        neutral = generator._group_adjacent_utterances(
            chapter("reflective narration"),
            source,
        )
        expressive = generator._group_adjacent_utterances(
            chapter("panicked shout"),
            source,
        )

        self.assertEqual(len(neutral.lines), 1)
        self.assertEqual(len(expressive.lines), 3)
        assert_script_covers_source(neutral, source)
        assert_script_covers_source(expressive, source)

    def test_grouping_preserves_material_prosody_transitions(self) -> None:
        first = "He considered the quiet horizon."
        second = "Run now!"
        source = f"{first} {second}"
        chapter = ScriptChapter(
            chapter_number=1,
            chapter_title="Transition",
            lines=[
                ScriptLine(
                    line_id="ch01_0000",
                    speaker="narrator",
                    text=first,
                    emotion="calm reflective narration",
                    speed=0.88,
                    source_fragment_id=0,
                    source_fragment_ids=[0],
                    source_start=0,
                    source_end=len(first),
                ),
                ScriptLine(
                    line_id="ch01_0001",
                    speaker="narrator",
                    text=second,
                    emotion="urgent panicked shout",
                    speed=1.2,
                    source_fragment_id=1,
                    source_fragment_ids=[1],
                    source_start=len(first) + 1,
                    source_end=len(source),
                ),
            ],
        )
        generator = ScriptGenerator(
            ollama=None,
            narrator_target_chars=300,
            narrator_max_words=60,
        )

        grouped = generator._group_adjacent_utterances(chapter, source)

        self.assertEqual(len(grouped.lines), 2)
        self.assertEqual(grouped.lines[0].emotion, "calm reflective narration")
        self.assertEqual(grouped.lines[1].emotion, "urgent panicked shout")
        assert_script_covers_source(grouped, source)

    def test_grouping_preserves_quote_classification_boundaries(self) -> None:
        source = 'The sign read "STOP". Then he left.'
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        lines = []
        for index, fragment in enumerate(fragments):
            is_quote = ScriptGenerator._is_dialogue_fragment(fragment.text)
            lines.append(
                ScriptLine(
                    line_id=f"ch01_{index:04d}",
                    speaker="narrator",
                    speaker_confidence=0.99 if is_quote else None,
                    speaker_evidence=(
                        "The source presents STOP as text written on a sign."
                        if is_quote else ""
                    ),
                    dialogue_kind="non_spoken_quote" if is_quote else None,
                    text=fragment.text,
                    source_fragment_id=index,
                    source_fragment_ids=[index],
                    source_start=fragment.start,
                    source_end=fragment.end,
                )
            )
        generator = ScriptGenerator(
            ollama=FakeScriptOllama([]),
            group_utterances=True,
        )
        grouped = generator._group_adjacent_utterances(
            ScriptChapter(chapter_number=1, chapter_title="One", lines=lines),
            source,
        )
        quote_line = next(
            line
            for line in grouped.lines
            if any(
                ScriptGenerator._is_dialogue_fragment(fragments[index].text)
                for index in line.source_fragment_ids
            )
        )
        self.assertEqual(quote_line.dialogue_kind, "non_spoken_quote")
        assert_script_covers_source(grouped, source)

    def test_large_single_chapter_is_analyzed_in_multiple_units(self) -> None:
        ollama = FakeCharacterOllama()
        analyzer = CharacterAnalyzer(ollama, single_pass_threshold=25000)
        text = "A complete sentence. " * 2000
        registry = analyzer.analyze(
            ExtractedBook(
                metadata=BookMetadata(
                    title="Long",
                    author="Author",
                    total_chapters=1,
                ),
                chapters=[
                    ExtractedChapter(
                        number=1,
                        title="Only",
                        text=text,
                        word_count=len(text.split()),
                    )
                ],
            )
        )
        self.assertGreater(ollama.calls, 1)
        self.assertIn("narrator", registry.characters)

    def test_character_checkpoint_is_bound_to_dependency_fingerprint(self) -> None:
        ollama = FakeCharacterOllama()
        analyzer = CharacterAnalyzer(ollama, single_pass_threshold=1)
        book = ExtractedBook(
            metadata=BookMetadata(title="Long", author="Author", total_chapters=1),
            chapters=[
                ExtractedChapter(
                    number=1,
                    title="Only",
                    text="A complete sentence. " * 20,
                    word_count=60,
                )
            ],
        )
        with TemporaryDirectory() as temporary:
            checkpoint = Path(temporary) / "characters.checkpoint.json"
            checkpoint.write_text(
                json.dumps(
                    {
                        "fingerprint": "old-dependencies",
                        "last_completed_unit": 999,
                        "accumulated_chars": {},
                        "tone_desc": "",
                    }
                ),
                encoding="utf-8",
            )
            analyzer.analyze(
                book,
                checkpoint_path=checkpoint,
                checkpoint_fingerprint="new-dependencies",
            )
        self.assertGreater(ollama.calls, 0)

    def test_generate_request_keeps_validate_wire_alias(self) -> None:
        chapter = ScriptChapter(
            chapter_number=1,
            chapter_title="One",
            lines=[],
        )
        request = GenerateChapterRequest.model_validate(
            {
                "project_id": "book",
                "chapter_number": chapter.chapter_number,
                "lines": chapter.model_dump()["lines"],
                "validate": False,
            }
        )
        self.assertFalse(request.validation_enabled)
        self.assertIn("validate", request.model_dump(by_alias=True))

    def test_line_id_cannot_escape_segment_directory(self) -> None:
        with self.assertRaises(ValueError):
            ScriptLine(
                line_id="../outside",
                speaker="narrator",
                text="Unsafe",
            )


class PartialGenerationTests(unittest.TestCase):
    def test_script_file_discovery_excludes_metadata_sidecars(self) -> None:
        with TemporaryDirectory() as temp_dir:
            scripts_dir = Path(temp_dir)
            for name in (
                "chapter_001.json",
                "chapter_001.meta.json",
                "chapter_notes.json",
            ):
                (scripts_dir / name).touch()
            self.assertEqual(
                [path.name for path in Pipeline._script_files(scripts_dir)],
                ["chapter_001.json"],
            )

    def test_non_contiguous_partial_name(self) -> None:
        self.assertEqual(format_chapter_set([5, 1, 2, 4, 2]), "1-2_4-5")

    def test_empty_slug_uses_safe_project_id(self) -> None:
        self.assertEqual(Pipeline._make_project_id("📚 !!!"), "book")

    def test_cross_midnight_window_uses_start_day(self) -> None:
        window = {
            "days": ["Monday"],
            "start": "22:00",
            "end": "07:00",
        }
        self.assertTrue(
            Pipeline._window_contains(
                window,
                datetime(2026, 7, 21, 1, 0),  # Tuesday
            )
        )
        self.assertFalse(
            Pipeline._window_contains(
                window,
                datetime(2026, 7, 22, 1, 0),  # Wednesday
            )
        )

    def test_empty_days_and_zero_duration_fail_closed(self) -> None:
        monday = datetime(2026, 7, 20, 12, 0)
        self.assertFalse(Pipeline._window_contains(
            {"days": [], "start": "08:00", "end": "18:00"}, monday
        ))
        self.assertFalse(Pipeline._window_contains(
            {"days": ["Monday"], "start": "12:00", "end": "12:00"}, monday
        ))

    def test_schedule_end_boundary_is_exclusive(self) -> None:
        window = {"days": ["Monday"], "start": "08:00", "end": "12:00"}
        self.assertTrue(Pipeline._window_contains(
            window, datetime(2026, 7, 20, 8, 0)
        ))
        self.assertFalse(Pipeline._window_contains(
            window, datetime(2026, 7, 20, 12, 0)
        ))

    def test_character_overrides_reapply_after_analysis(self) -> None:
        registry = CharacterRegistry(
            characters={
                "speaker": Character(
                    id="speaker",
                    name="Speaker",
                    gender=Gender.OTHER,
                    age_range="adult",
                    voice_description="A neutral speaking voice",
                    speaking_style="measured",
                )
            }
        )
        with TemporaryDirectory() as directory:
            project_dir = Path(directory)
            (project_dir / "character_overrides.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "characters": {
                            "speaker": {
                                "gender": "female",
                                "age_range": "30s",
                                "speaking_style": "calm and deliberate",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            changed = Pipeline._apply_character_overrides(project_dir, registry)

        self.assertTrue(changed)
        self.assertEqual(registry.characters["speaker"].gender, Gender.FEMALE)
        self.assertEqual(registry.characters["speaker"].age_range, "30s")
        self.assertEqual(
            registry.characters["speaker"].speaking_style,
            "calm and deliberate",
        )

    def test_detect_new_characters_generic_speaker_autoprovisioned(self) -> None:
        registry = CharacterRegistry(
            characters={
                "narrator": Character(
                    id="narrator",
                    name="Narrator",
                    gender=Gender.FEMALE,
                    age_range="adult",
                    voice_description="Narrator voice",
                    speaking_style="clear",
                )
            }
        )
        script = ScriptChapter(
            chapter_number=1,
            chapter_title="Chapter 1",
            lines=[
                ScriptLine(line_id="1", speaker="character_female", text="Help me!"),
                ScriptLine(line_id="2", speaker="unnamed_man", text="Stay back!"),
            ],
        )
        generator = ScriptGenerator(ollama=None)
        generator._detect_new_characters(script, registry)
        
        self.assertEqual(script.lines[0].speaker, "minor_female")
        self.assertEqual(script.lines[1].speaker, "minor_male")
        self.assertIn("minor_female", registry.characters)
        self.assertIn("minor_male", registry.characters)
        self.assertEqual(registry.characters["minor_female"].gender, Gender.FEMALE)
        self.assertEqual(registry.characters["minor_male"].gender, Gender.MALE)


if __name__ == "__main__":
    unittest.main()
