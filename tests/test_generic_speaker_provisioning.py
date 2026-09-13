"""A generic speaker introduced by attribution must still get a voice.

`_detect_new_characters` mints a cast entry for any generic speaker a line
uses -- `crowd`, `collective`, `minor_male` and the rest each carry a
voice_description and a test_sentence. It runs inside `generate_all_chapters`,
during Pass 2.

Attribution runs afterwards and can resolve a line to a generic speaker Pass 2
never saw. On `the-finest-edge-of-twilight-book` the Gemini web tier resolved
two shouted lines to `crowd` -- the right answer, since a crowd is not any
individual character -- and provisioning had already been and gone. `crowd`
never reached characters.json, so:

  * the line named a speaker with no cast entry, and therefore no voice
  * the review inbox's Approve button sent that speaker back and got
    `422 Unknown character speaker 'crowd'`, so the item could not be cleared
  * the speaker dropdown had no matching option and fell back to `narrator`
"""

from __future__ import annotations

import unittest

from brain.director.script_generator import _GENERIC_SPEAKER_DEFINITIONS, ScriptGenerator
from shared.constants import Gender
from shared.models import Character, CharacterRegistry, ScriptChapter, ScriptLine


def _registry() -> CharacterRegistry:
    return CharacterRegistry(
        characters={
            "narrator": Character(
                id="narrator",
                name="Narrator",
                gender=Gender.OTHER,
                age_range="adult",
                voice_description="narrator voice",
            )
        }
    )


def _chapter(speaker: str) -> ScriptChapter:
    return ScriptChapter(
        chapter_number=2,
        chapter_title="Two",
        lines=[ScriptLine(line_id="ch02_0038", speaker=speaker, text='"Hail, King Bruenor!"', dialogue_kind="spoken")],
    )


class GenericSpeakerProvisioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.generator = object.__new__(ScriptGenerator)

    def test_a_speaker_attribution_introduced_gets_a_cast_entry(self) -> None:
        registry = _registry()
        self.assertNotIn("crowd", registry.characters)

        added = self.generator.provision_generic_speakers([_chapter("crowd")], registry)

        self.assertEqual(added, ["crowd"])
        self.assertIn("crowd", registry.characters)

    def test_the_entry_is_actually_castable(self) -> None:
        """A registry row without a voice description cannot be cast."""
        registry = _registry()
        self.generator.provision_generic_speakers([_chapter("crowd")], registry)

        crowd = registry.characters["crowd"]
        self.assertEqual(crowd.name, "Crowd")
        self.assertTrue(crowd.voice_description, "no voice description means no voice")
        self.assertTrue(getattr(crowd, "test_sentence", ""), "validation needs a test sentence")

    def test_running_it_twice_changes_nothing(self) -> None:
        """The pipeline calls this after Pass 2 has already run it."""
        registry = _registry()
        chapter = _chapter("crowd")
        self.assertEqual(self.generator.provision_generic_speakers([chapter], registry), ["crowd"])
        self.assertEqual(self.generator.provision_generic_speakers([chapter], registry), [])

    def test_every_defined_generic_can_be_provisioned(self) -> None:
        """Whatever attribution picks, it must be castable -- not just crowd."""
        for speaker in _GENERIC_SPEAKER_DEFINITIONS:
            with self.subTest(speaker=speaker):
                registry = _registry()
                added = self.generator.provision_generic_speakers([_chapter(speaker)], registry)
                self.assertEqual(added, [speaker])
                self.assertTrue(registry.characters[speaker].voice_description)

    def test_a_real_character_is_left_alone(self) -> None:
        registry = _registry()
        registry.characters["dahlia"] = Character(
            id="dahlia", name="Dahlia", gender=Gender.FEMALE, age_range="adult", voice_description="a voice"
        )
        added = self.generator.provision_generic_speakers([_chapter("dahlia")], registry)
        self.assertEqual(added, [])
        self.assertEqual(len(registry.characters), 2)


if __name__ == "__main__":
    unittest.main()
