from __future__ import annotations

import unittest

from brain.director.attribution_audit import (
    audit_book_attribution,
    queue_attribution_audit_issues,
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


class AttributionAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = CharacterRegistry(
            book_title="Book",
            book_author="Author",
            characters={
                "narrator": Character(
                    id="narrator",
                    name="Narrator",
                    gender=Gender.MALE,
                    age_range="adult",
                    voice_description="neutral narrator",
                ),
                "vathi": Character(
                    id="vathi",
                    name="Vathi",
                    gender=Gender.FEMALE,
                    age_range="adult",
                    voice_description="clear voice",
                ),
                "dusk": Character(
                    id="dusk",
                    name="Dusk",
                    gender=Gender.MALE,
                    age_range="adult",
                    voice_description="measured voice",
                ),
                "minor_female": Character(
                    id="minor_female",
                    name="Unnamed Woman",
                    gender=Gender.FEMALE,
                    age_range="adult",
                    voice_description="generic female voice",
                ),
            },
        )

    def _artifacts(self, source: str, speaker: str, **updates):
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        lines = []
        for index, fragment in enumerate(fragments):
            is_dialogue = ScriptGenerator._is_dialogue_fragment(fragment.text)
            values = {
                "line_id": f"ch01_{index:04d}",
                "speaker": speaker if is_dialogue else "narrator",
                "speaker_confidence": 0.95 if is_dialogue else None,
                "speaker_evidence": "The local conversation identifies the speaker.",
                "dialogue_kind": "spoken" if is_dialogue else None,
                "text": fragment.text,
                "source_fragment_id": index,
                "source_fragment_ids": [index],
                "source_start": fragment.start,
                "source_end": fragment.end,
            }
            if is_dialogue:
                values.update(updates)
            lines.append(ScriptLine(**values))
        book = ExtractedBook(
            metadata=BookMetadata(title="Book", author="Author", total_chapters=1),
            chapters=[ExtractedChapter(number=1, title="One", text=source, word_count=3)],
        )
        script = ScriptChapter(chapter_number=1, chapter_title="One", lines=lines)
        return book, [script]

    def test_blocks_narrator_owned_spoken_quote(self) -> None:
        book, scripts = self._artifacts('"Wait," she said.', "narrator")
        report = audit_book_attribution(book, self.registry, scripts)
        self.assertFalse(report["passed"])
        self.assertEqual(report["issues"][0]["kind"], "narrator_spoken_dialogue")

    def test_allows_evidenced_non_spoken_quote(self) -> None:
        book, scripts = self._artifacts(
            'The sign read "STOP".',
            "narrator",
            dialogue_kind="non_spoken_quote",
            speaker_evidence="The source explicitly presents STOP as text on a sign.",
        )
        report = audit_book_attribution(book, self.registry, scripts)
        self.assertTrue(report["passed"], report["issues"])

    def test_action_beat_does_not_create_false_tag_contradiction(self) -> None:
        book, scripts = self._artifacts('"Wait." Vathi smiled.', "vathi")
        report = audit_book_attribution(book, self.registry, scripts)
        self.assertTrue(report["passed"], report["issues"])

    def test_allows_explicitly_tagged_reported_collective_speech(self) -> None:
        book, scripts = self._artifacts(
            'They said, "We should explain it."',
            "narrator",
            dialogue_kind="reported_collective_speech",
            speaker_evidence="The leading source tag explicitly says the anonymous group said it.",
        )
        report = audit_book_attribution(book, self.registry, scripts)
        self.assertTrue(report["passed"], report["issues"])

    def test_blocks_collective_quote_assigned_to_named_character(self) -> None:
        book, scripts = self._artifacts(
            'They said, "We should explain it."',
            "dusk",
        )
        report = audit_book_attribution(book, self.registry, scripts)
        self.assertFalse(report["passed"])
        self.assertEqual(
            report["issues"][0]["kind"],
            "collective_speech_character_contradiction",
        )

    def test_checks_named_speech_tag_before_the_quote(self) -> None:
        book, scripts = self._artifacts('Vathi said, "Wait."', "dusk")
        report = audit_book_attribution(book, self.registry, scripts)
        self.assertFalse(report["passed"])
        self.assertEqual(report["issues"][0]["kind"], "named_tag")

    def test_repairs_unique_registered_named_tag_before_external_validation(self) -> None:
        book, scripts = self._artifacts('Vathi said, "Wait."', "dusk")

        result = repair_deterministic_named_attribution(
            book,
            self.registry,
            scripts,
        )

        dialogue = next(line for line in scripts[0].lines if line.dialogue_kind == "spoken")
        self.assertEqual(len(result["repaired"]), 1)
        self.assertEqual(dialogue.speaker, "vathi")
        self.assertEqual(dialogue.speaker_confidence, 1.0)
        self.assertEqual(dialogue.attribution_resolver, "deterministic_named_tag")
        self.assertTrue(audit_book_attribution(book, self.registry, scripts)["passed"])

    def test_repairs_generic_scene_after_registered_self_identity_reveal(self) -> None:
        source = (
            '"Can you help?" the woman asked. '
            'Dusk waited. "My name is Vathi," the woman said. '
            '"Please listen," she continued.'
        )
        book, scripts = self._artifacts(source, "minor_female")

        before = audit_book_attribution(book, self.registry, scripts)
        self.assertFalse(before["passed"])
        self.assertIn(
            "self_identified_generic_speaker",
            {issue["kind"] for issue in before["issues"]},
        )

        result = repair_deterministic_named_attribution(
            book,
            self.registry,
            scripts,
        )

        dialogue = [line for line in scripts[0].lines if line.dialogue_kind == "spoken"]
        self.assertEqual(len(result["repaired"]), 3)
        self.assertEqual({line.speaker for line in dialogue}, {"vathi"})
        self.assertEqual(
            {line.attribution_resolver for line in dialogue},
            {"deterministic_identity_reveal"},
        )
        self.assertTrue(audit_book_attribution(book, self.registry, scripts)["passed"])

    def test_does_not_repair_identity_reveal_with_gender_conflict(self) -> None:
        source = '"My name is Dusk," the woman said.'
        book, scripts = self._artifacts(source, "minor_female")

        result = repair_deterministic_named_attribution(
            book,
            self.registry,
            scripts,
        )

        dialogue = next(line for line in scripts[0].lines if line.dialogue_kind == "spoken")
        self.assertEqual(result["repaired"], [])
        self.assertEqual(dialogue.speaker, "minor_female")
        self.assertIn(dialogue.line_id, result["conflicted_line_ids"])

    def test_repairs_bare_name_answer_after_identity_question(self) -> None:
        source = '"What is your name?" Dusk asked. "Vathi," the woman replied.'
        book, scripts = self._artifacts(source, "minor_female")
        dialogue = [line for line in scripts[0].lines if line.dialogue_kind == "spoken"]
        dialogue[0].speaker = "dusk"

        result = repair_deterministic_named_attribution(
            book,
            self.registry,
            scripts,
        )

        self.assertEqual(dialogue[1].speaker, "vathi")
        self.assertTrue(result["repaired"])

    def test_bare_name_without_identity_question_is_not_repaired(self) -> None:
        source = '"Vathi!" the woman shouted.'
        book, scripts = self._artifacts(source, "minor_female")

        result = repair_deterministic_named_attribution(
            book,
            self.registry,
            scripts,
        )

        dialogue = next(line for line in scripts[0].lines if line.dialogue_kind == "spoken")
        self.assertEqual(dialogue.speaker, "minor_female")
        self.assertEqual(result["repaired"], [])

    def test_audit_contradiction_is_queued_for_external_validation(self) -> None:
        book, scripts = self._artifacts('"Wait," he said.', "vathi")
        report = audit_book_attribution(book, self.registry, scripts)

        queued = queue_attribution_audit_issues(
            report,
            scripts,
            confidence_threshold=0.55,
        )

        dialogue = next(line for line in scripts[0].lines if line.dialogue_kind == "spoken")
        self.assertEqual(queued, [dialogue.line_id])
        self.assertTrue(dialogue.attribution_review_required)
        self.assertLess(dialogue.speaker_confidence, 0.55)
        self.assertIn("pronoun_gender", dialogue.attribution_review_reason)

    def test_trailing_tag_is_not_reused_for_the_next_turn(self) -> None:
        source = '"Just for me?" she whispered. "Just for you."'
        fragments = ScriptGenerator._split_into_fragment_spans(source)
        dialogue_indexes = [
            index for index, fragment in enumerate(fragments) if ScriptGenerator._is_dialogue_fragment(fragment.text)
        ]
        lines = []
        for index, fragment in enumerate(fragments):
            speaker = "narrator"
            confidence = None
            kind = None
            if index == dialogue_indexes[0]:
                speaker, confidence, kind = "vathi", 0.95, "spoken"
            elif index == dialogue_indexes[1]:
                speaker, confidence, kind = "dusk", 0.95, "spoken"
            lines.append(
                ScriptLine(
                    line_id=f"ch01_{index:04d}",
                    speaker=speaker,
                    speaker_confidence=confidence,
                    speaker_evidence="Conversation turn and attached tag evidence.",
                    dialogue_kind=kind,
                    text=fragment.text,
                    source_fragment_id=index,
                    source_fragment_ids=[index],
                    source_start=fragment.start,
                    source_end=fragment.end,
                )
            )
        book = ExtractedBook(
            metadata=BookMetadata(title="Book", author="Author", total_chapters=1),
            chapters=[ExtractedChapter(number=1, title="One", text=source, word_count=7)],
        )
        report = audit_book_attribution(
            book,
            self.registry,
            [ScriptChapter(chapter_number=1, chapter_title="One", lines=lines)],
        )
        self.assertTrue(report["passed"], report["issues"])

    def test_embedded_scare_quote_cannot_become_character_dialogue(self) -> None:
        book, scripts = self._artifacts(
            'He warned as many of their "trappers" as possible.',
            "dusk",
        )
        report = audit_book_attribution(book, self.registry, scripts)
        self.assertFalse(report["passed"])
        self.assertEqual(report["issues"][0]["kind"], "embedded_quoted_term")

    def test_embedded_lexical_quote_can_be_explicit_narration(self) -> None:
        book, scripts = self._artifacts(
            'Her name meant "child"; everyone remembered it.',
            "narrator",
            dialogue_kind="non_spoken_quote",
            speaker_evidence="The source uses child as the lexical meaning of the name.",
        )
        report = audit_book_attribution(book, self.registry, scripts)
        self.assertTrue(report["passed"], report["issues"])


if __name__ == "__main__":
    unittest.main()
