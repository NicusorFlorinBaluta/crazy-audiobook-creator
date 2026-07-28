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


class ScriptFidelityTests(unittest.TestCase):
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

    def test_large_single_chapter_is_analyzed_in_multiple_units(self) -> None:
        ollama = FakeCharacterOllama()
        analyzer = CharacterAnalyzer(ollama)
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
