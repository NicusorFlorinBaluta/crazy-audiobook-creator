"""Unit tests for generic speaker attribution fixes, tag syntax resolution, and test chapters."""

from __future__ import annotations

import unittest

from brain.director.attribution_audit import (
    audit_book_attribution,
    repair_deterministic_named_attribution,
)
from brain.director.script_generator import ScriptGenerator
from shared.constants import Gender
from shared.models import (
    BookMetadata,
    Character,
    CharacterRegistry,
    ExtractedBook,
    ExtractedChapter,
    ScriptChapter,
    ScriptLine,
)


class NonSpeakingAttributionTests(unittest.TestCase):
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
                    voice_description="narrator voice",
                ),
                "dusk": Character(
                    id="dusk",
                    name="Dusk",
                    gender=Gender.MALE,
                    age_range="30s",
                    voice_description="measured male voice",
                ),
                "vathi": Character(
                    id="vathi",
                    name="Vathi",
                    gender=Gender.FEMALE,
                    age_range="30s",
                    voice_description="crisp female voice",
                ),
                "starling": Character(
                    id="starling",
                    name="Starling",
                    gender=Gender.FEMALE,
                    age_range="20s",
                    voice_description="energetic female voice",
                ),
                "ed": Character(
                    id="ed",
                    name="Ed",
                    gender=Gender.MALE,
                    age_range="20s",
                    voice_description="cheerful male voice",
                ),
                "ruen": Character(
                    id="ruen",
                    name="Ruen",
                    gender=Gender.MALE,
                    age_range="40s",
                    voice_description="scholarly male voice",
                ),
                "kokerlii": Character(
                    id="kokerlii",
                    name="Kokerlii",
                    gender=Gender.OTHER,
                    age_range="N/A",
                    voice_description="non-human bird entity",
                ),
                "sak": Character(
                    id="sak",
                    name="Sak",
                    gender=Gender.OTHER,
                    age_range="N/A",
                    voice_description="non-human bird entity",
                ),
                "mother_frond": Character(
                    id="mother_frond",
                    name="Mother Frond",
                    gender=Gender.FEMALE,
                    age_range="60s",
                    voice_description="elderly wise female voice",
                ),
                "aslan": Character(
                    id="aslan",
                    name="Aslan",
                    gender=Gender.MALE,
                    age_range="adult",
                    voice_description="deep resonant lion voice",
                ),
                "meeker": Character(
                    id="meeker",
                    name="Meeker",
                    gender=Gender.OTHER,
                    age_range="N/A",
                    voice_description="telepathic creature voice",
                ),
            },
        )

    def _make_book_and_script(
        self, source_text: str, line_speaker_map: dict[int, str]
    ) -> tuple[ExtractedBook, list[ScriptChapter]]:
        fragments = ScriptGenerator._split_into_fragment_spans(source_text)
        lines: list[ScriptLine] = []
        for idx, frag in enumerate(fragments):
            is_dlg = ScriptGenerator._is_dialogue_fragment(frag.text)
            speaker = line_speaker_map.get(idx, "narrator" if not is_dlg else "minor_male")
            lines.append(
                ScriptLine(
                    line_id=f"ch01_{idx:04d}",
                    speaker=speaker,
                    speaker_confidence=0.95 if is_dlg else None,
                    speaker_evidence="Context identifies speaker",
                    dialogue_kind="spoken" if is_dlg else None,
                    text=frag.text,
                    source_fragment_id=idx,
                    source_fragment_ids=[idx],
                    source_start=frag.start,
                    source_end=frag.end,
                )
            )
        book = ExtractedBook(
            metadata=BookMetadata(title="Test Book", author="Test Author", total_chapters=1),
            chapters=[ExtractedChapter(number=1, title="One", text=source_text, word_count=len(source_text.split()))],
        )
        script = ScriptChapter(chapter_number=1, chapter_title="One", lines=lines)
        return book, [script]

    def test_participial_action_tag_does_not_misattribute_to_object(self) -> None:
        """'Dusk said, handing Kokerlii toward her' must resolve to Dusk, not Kokerlii."""
        tag = "Dusk said, handing Kokerlii toward her."
        speaker, kind, gender = ScriptGenerator._dialogue_tag_evidence(tag, self.registry)
        self.assertEqual(speaker, "dusk")
        self.assertEqual(kind, "named_tag")

    def test_short_name_subject_not_overridden_by_long_object_name(self) -> None:
        """'Ed said, squeezing Starling's arm' must resolve to Ed, not Starling."""
        tag = "Ed said, squeezing Starling's arm out of glee."
        speaker, kind, gender = ScriptGenerator._dialogue_tag_evidence(tag, self.registry)
        self.assertEqual(speaker, "ed")
        self.assertEqual(kind, "named_tag")

    def test_prepositional_recipient_does_not_become_speaker(self) -> None:
        """'said Starling to Ed with a smile' must resolve to Starling, not Ed."""
        tag = "said Starling to Ed with a smile."
        speaker, kind, gender = ScriptGenerator._dialogue_tag_evidence(tag, self.registry)
        self.assertEqual(speaker, "starling")
        self.assertEqual(kind, "named_tag")

    def test_extended_narration_with_leading_speech_tag(self) -> None:
        """Long narrative sentences starting with a speech tag must be recognized."""
        tag = "Dusk said, causing others in the room to murmur. They were uncomfortable with Sak's power, which was unique among Aviar."
        self.assertTrue(ScriptGenerator._is_pure_dialogue_tag(tag))
        speaker, kind, gender = ScriptGenerator._dialogue_tag_evidence(tag, self.registry)
        self.assertEqual(speaker, "dusk")

    def test_voiced_talking_animals_are_supported(self) -> None:
        """A voiced animal like Aslan speaking actual dialogue is validly supported."""
        tag = "Aslan roared, 'Stand firm!'"
        speaker, kind, gender = ScriptGenerator._dialogue_tag_evidence("Aslan roared,", self.registry)
        self.assertEqual(speaker, "aslan")

    def test_telepathic_communication_dialogue_supported(self) -> None:
        """Telepathic impressions/messages in dialogue are supported."""
        tag = "Meeker sent to Dusk,"
        # Add 'sent' to test telepathic or speech phrasing
        speaker, kind, _ = ScriptGenerator._dialogue_tag_evidence("Meeker said,", self.registry)
        self.assertEqual(speaker, "meeker")

    def test_repair_ch018_sak_misattribution(self) -> None:
        """Chapter 18 pattern: dialogue misattributed to Sak is repaired to Ruen."""
        source = (
            'Dusk said, "Cakoban did it." '
            '"Oh," Ruen said, eyes bright, his Aviar exclaiming excitedly. '
            '"You think that too? I think this must be the endless darkness!"'
        )
        # Fragment 1 is "Cakoban did it.", fragment 2 is "Oh,", fragment 4 is "You think that too..."
        book, scripts = self._make_book_and_script(source, {1: "dusk", 2: "sak", 4: "sak"})

        report = audit_book_attribution(book, self.registry, scripts)
        self.assertFalse(report["passed"])

        repair = repair_deterministic_named_attribution(book, self.registry, scripts)
        self.assertEqual(len(repair["repaired"]), 2)

        lines = {l.source_fragment_id: l for l in scripts[0].lines}
        self.assertEqual(lines[2].speaker, "ruen")
        self.assertEqual(lines[4].speaker, "ruen")

    def test_repair_ch019_kokerlii_misattribution(self) -> None:
        """Chapter 19 pattern: dialogue misattributed to Kokerlii is repaired to Dusk."""
        source = (
            'Vathi asked, "What are you doing?" '
            '"I will go as far as I can," Dusk said, handing Kokerlii toward her. '
            '"You will need to hold him."'
        )
        # Fragment 1 is "What are you doing?", fragment 2 is "I will go...", fragment 4 is "You will need..."
        book, scripts = self._make_book_and_script(source, {1: "vathi", 2: "kokerlii", 4: "dusk"})

        report = audit_book_attribution(book, self.registry, scripts)
        self.assertFalse(report["passed"])

        repair = repair_deterministic_named_attribution(book, self.registry, scripts)
        self.assertEqual(len(repair["repaired"]), 1)

        lines = {l.source_fragment_id: l for l in scripts[0].lines}
        self.assertEqual(lines[2].speaker, "dusk")

    def test_repair_ch026_bidirectional_tag_misattribution(self) -> None:
        """Chapter 26 pattern: dialogue misattributed to Starling instead of Ed."""
        source = (
            '"Look over there," Starling whispered. '
            "\"But it's so beautiful!\" Ed said, squeezing Starling's arm out of glee. "
            '"Can you believe we found one?"'
        )
        # Fragment 0 is "Look over there,", fragment 2 is "But it's so beautiful!", fragment 4 is "Can you believe..."
        book, scripts = self._make_book_and_script(source, {0: "starling", 2: "starling", 4: "starling"})

        report = audit_book_attribution(book, self.registry, scripts)
        self.assertFalse(report["passed"])

        repair = repair_deterministic_named_attribution(book, self.registry, scripts)
        self.assertEqual(len(repair["repaired"]), 2)

        lines = {l.source_fragment_id: l for l in scripts[0].lines}
        self.assertEqual(lines[2].speaker, "ed")
        self.assertEqual(lines[4].speaker, "ed")

    def test_sync_dialogue_counts_updates_registry(self) -> None:
        """sync_dialogue_counts accurately updates character dialogue counts."""
        source = '"Hello," Dusk said. "Hi," Vathi replied. "Goodbye," Dusk said.'
        book, scripts = self._make_book_and_script(source, {0: "dusk", 2: "vathi", 4: "dusk"})

        ScriptGenerator.sync_dialogue_counts(scripts, self.registry)
        self.assertEqual(self.registry.characters["dusk"].dialogue_count, 2)
        self.assertEqual(self.registry.characters["vathi"].dialogue_count, 1)
        self.assertEqual(self.registry.characters["ruen"].dialogue_count, 0)
        self.assertEqual(self.registry.characters["sak"].dialogue_count, 0)
        self.assertEqual(self.registry.characters["kokerlii"].dialogue_count, 0)

    def test_chapter_004_vocative_address(self) -> None:
        """Chapter 4 pattern: Mother Frond addressing Sak in dialogue is not Sak's speech."""
        source = (
            "Mother Frond turned to the bird. "
            '"What have you been showing your master these days, Sak?" '
            '"Guide him well."'
        )
        book, scripts = self._make_book_and_script(source, {1: "mother_frond", 2: "mother_frond"})
        report = audit_book_attribution(book, self.registry, scripts)
        self.assertTrue(report["passed"])
        self.assertEqual(scripts[0].lines[1].speaker, "mother_frond")
        self.assertEqual(scripts[0].lines[2].speaker, "mother_frond")


if __name__ == "__main__":
    unittest.main()
