import unittest
from brain.director.script_generator import ScriptGenerator, SourceFragment
from shared.models import Character, CharacterRegistry, Gender
from voice.mastering.m4b_exporter import M4BExporter


class AttributionGuardrailsEnhancedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = CharacterRegistry(
            book_title="Test Book",
            book_author="Test Author",
            characters={
                "narrator": Character(
                    id="narrator",
                    name="Narrator",
                    gender=Gender.MALE,
                    age_range="adult",
                    voice_description="Voice description.",
                    voice_id="narrator_male",
                ),
                "drominadian": Character(
                    id="drominadian",
                    name="Drominadian Stranger",
                    gender=Gender.OTHER,
                    age_range="adult",
                    voice_description="Voice description.",
                    aliases=["Drominadian Stranger", "Stranger", "Alien", "Armored Alien"],
                    voice_id="drominadian",
                ),
                "soil": Character(
                    id="soil",
                    name="Second of the Soil",
                    gender=Gender.MALE,
                    age_range="adult",
                    voice_description="Voice description.",
                    aliases=["Second of the Soil", "Soil"],
                    voice_id="soil",
                ),
                "vathi": Character(
                    id="vathi",
                    name="Vathi",
                    gender=Gender.FEMALE,
                    age_range="adult",
                    voice_description="Voice description.",
                    aliases=["Vathi"],
                    voice_id="vathi",
                ),
                "second_of_saplings": Character(
                    id="second_of_saplings",
                    name="Second of Saplings",
                    gender=Gender.MALE,
                    age_range="adult",
                    voice_description="Voice description.",
                    aliases=["Second of Saplings", "Saplings"],
                    voice_id="saplings",
                ),
            },
        )

    def test_same_paragraph_split_turn_continuity(self) -> None:
        fragments = [
            SourceFragment('"Progress is always sad,"', 0, 25),
            SourceFragment("he said.", 26, 34),
            SourceFragment('"There is no new life without death."', 35, 72),
        ]
        # "he said." establishes Gender.MALE in paragraph
        dialogue_map, tag_map = ScriptGenerator._paragraph_attribution_maps(
            fragments, self.registry
        )
        self.assertIn(0, tag_map)
        self.assertIn(2, tag_map)
        _, _, g0 = tag_map[0]
        _, _, g2 = tag_map[2]
        self.assertEqual(g0, Gender.MALE)
        self.assertEqual(g2, Gender.MALE)

        resolved = ScriptGenerator._resolve_dialogue_speaker(
            2,
            fragments,
            {0: {"speaker": "soil"}},
            {"soil", "vathi"},
            registry=self.registry,
            target_gender=Gender.MALE,
        )
        self.assertEqual(resolved, "soil")

    def test_dialogue_tag_evidence_expanded_verbs_and_bidirectional(self) -> None:
        # Tag with descriptive alias and verb: "the armored alien said,"
        exact, kind, _ = ScriptGenerator._dialogue_tag_evidence(
            "the armored alien said,", self.registry
        )
        self.assertEqual(exact, "drominadian")
        self.assertEqual(kind, "named_tag")

        # Tag with verb-subject order: "said the stranger"
        exact_rev, kind_rev, _ = ScriptGenerator._dialogue_tag_evidence(
            "said the stranger", self.registry
        )
        self.assertEqual(exact_rev, "drominadian")
        self.assertEqual(kind_rev, "named_tag")

        # Tag with newly added speech verb: "Saplings snapped,"
        exact_snap, kind_snap, _ = ScriptGenerator._dialogue_tag_evidence(
            "Saplings snapped,", self.registry
        )
        self.assertEqual(exact_snap, "second_of_saplings")
        self.assertEqual(kind_snap, "named_tag")

    def test_split_quote_speech_tag_attribution(self) -> None:
        # Fragment 0: "My people will give you back..."
        # Fragment 1: the armored alien said,
        # Fragment 2: "and will allow you to fight..."
        fragments = [
            SourceFragment(text='"My people will give you back one out of a hundred birds born,"', start=0, end=60),
            SourceFragment(text="the armored alien said,", start=61, end=84),
            SourceFragment(text='"and will allow you to fight alongside us."', start=85, end=128),
        ]
        raw_metadata = {
            "lines": [
                {
                    "id": 0,
                    "speaker": "vathi",  # Misattributed to vathi
                    "dialogue_kind": "spoken",
                    "speaker_evidence": "Vathi speaks.",
                    "speaker_confidence": 0.95,
                },
                {
                    "id": 2,
                    "speaker": "soil",  # Misattributed to soil
                    "dialogue_kind": "spoken",
                    "speaker_evidence": "Soil speaks.",
                    "speaker_confidence": 0.95,
                },
            ]
        }
        issues = ScriptGenerator._collect_metadata_speaker_issues(
            raw_metadata,
            fragments,
            allowed_speakers=set(self.registry.characters.keys()),
            registry=self.registry,
        )
        issue_fragments = {issue.fragment_index: issue for issue in issues}
        self.assertIn(0, issue_fragments)
        self.assertEqual(issue_fragments[0].exact_speaker, "drominadian")
        self.assertIn(2, issue_fragments)
        self.assertEqual(issue_fragments[2].exact_speaker, "drominadian")

    def test_period_ending_tag_carries_across_split_quote(self) -> None:
        source = '"First half," Vathi said. "Second half."\n\n"New turn," Soil said.'
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        dialogue_indexes = [
            index
            for index, fragment in enumerate(fragments)
            if ScriptGenerator._is_dialogue_fragment(fragment.text)
        ]
        raw_metadata = {
            "lines": [
                {
                    "id": dialogue_indexes[0],
                    "speaker": "vathi",
                    "dialogue_kind": "spoken",
                    "speaker_evidence": "The adjacent named tag identifies Vathi.",
                    "speaker_confidence": 0.99,
                },
                {
                    "id": dialogue_indexes[1],
                    "speaker": "soil",
                    "dialogue_kind": "spoken",
                    "speaker_evidence": "Incorrect generic conversation context.",
                    "speaker_confidence": 0.95,
                },
                {
                    "id": dialogue_indexes[2],
                    "speaker": "soil",
                    "dialogue_kind": "spoken",
                    "speaker_evidence": "The adjacent named tag identifies Soil.",
                    "speaker_confidence": 0.99,
                },
            ]
        }

        issues = ScriptGenerator._collect_metadata_speaker_issues(
            raw_metadata,
            fragments,
            allowed_speakers=set(self.registry.characters),
            registry=self.registry,
            chapter_text=source,
        )

        issue_by_fragment = {issue.fragment_index: issue for issue in issues}
        self.assertEqual(
            issue_by_fragment[dialogue_indexes[1]].exact_speaker,
            "vathi",
        )
        self.assertNotIn(dialogue_indexes[2], issue_by_fragment)

    def test_m4b_chapter_title_formatting(self) -> None:
        # Book chapter titles are preserved for rich audio track navigation.
        self.assertEqual(
            M4BExporter._format_chapter_title(1, "Prologue: Fifty-Seven Years Ago"),
            "Prologue: Fifty-Seven Years Ago",
        )
        self.assertEqual(
            M4BExporter._format_chapter_title(2, "Chapter One"),
            "Chapter One",
        )
        self.assertEqual(
            M4BExporter._format_chapter_title(3, "Chapter Two: Five Years Ago"),
            "Chapter Two: Five Years Ago",
        )
        # Empty title defaults to Chapter {N}
        self.assertEqual(
            M4BExporter._format_chapter_title(3, ""),
            "Chapter 3",
        )
        self.assertEqual(
            M4BExporter._format_chapter_title(5, "Chapter 5"),
            "Chapter 5",
        )
        self.assertEqual(
            M4BExporter._format_chapter_title(5, "Chapter 5: The Meeting"),
            "Chapter 5: The Meeting",
        )


if __name__ == "__main__":
    unittest.main()
