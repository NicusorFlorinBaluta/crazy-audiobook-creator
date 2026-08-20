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

    def test_evidence_contradicts_speaker_detection(self) -> None:
        # LLM evidence mentions alien's dialogue while assigning soil
        evidence = "Continuation of the alien's dialogue, maintaining the same voice and tone as Second of the Soil."
        contra, implied = ScriptGenerator._evidence_contradicts_speaker(
            evidence, "soil", self.registry
        )
        self.assertTrue(contra)
        self.assertEqual(implied, "drominadian")

        # LLM evidence consistent with speaker
        clean_evidence = "Vathi speaks directly to Dusk about the treaty."
        contra_clean, implied_clean = ScriptGenerator._evidence_contradicts_speaker(
            clean_evidence, "vathi", self.registry
        )
        self.assertFalse(contra_clean)
        self.assertIsNone(implied_clean)

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

    def test_m4b_chapter_title_formatting(self) -> None:
        # Numbered chapters with text titles
        self.assertEqual(
            M4BExporter._format_chapter_title(8, "Chapter Seven"),
            "Chapter 8: Chapter Seven",
        )
        self.assertEqual(
            M4BExporter._format_chapter_title(9, "Five Years Ago"),
            "Chapter 9: Five Years Ago",
        )
        self.assertEqual(
            M4BExporter._format_chapter_title(10, "Chapter Nine"),
            "Chapter 10: Chapter Nine",
        )
        # Empty title defaults to Chapter {N}
        self.assertEqual(
            M4BExporter._format_chapter_title(3, ""),
            "Chapter 3",
        )
        # Title already starting with Chapter {N} preserves exact title
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
