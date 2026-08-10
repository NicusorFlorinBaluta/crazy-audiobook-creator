from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from brain.director.character_analyzer import CharacterAnalyzer
from brain.director.script_generator import ScriptGenerator, SourceFragment
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


class ScriptFidelityTests(unittest.TestCase):
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

    def test_voice_redesign_does_not_invalidate_script_fingerprint(self) -> None:
        chapter = ExtractedChapter(
            number=1,
            title="One",
            text='"Hello," she said.',
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


if __name__ == "__main__":
    unittest.main()
