import array
import math
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

from shared.constants import Gender
from shared.models import Character, CharacterRegistry, ScriptChapter, ScriptLine
from shared.voice_casting import (
    build_voice_cast,
    compile_effective_voice_prompt,
    required_voice_character_ids,
    speaking_character_ids,
)


def _character(
    character_id: str,
    *,
    gender: Gender,
    description: str,
    voice_id: str | None = None,
) -> Character:
    return Character(
        id=character_id,
        name=character_id.replace("_", " ").title(),
        gender=gender,
        age_range="adult",
        personality_traits=[],
        voice_description=description,
        voice_id=voice_id,
    )


class VoiceCastingTests(unittest.TestCase):
    def test_gender_metadata_repairs_contradictory_register(self) -> None:
        prompt, warnings = compile_effective_voice_prompt(
            gender=Gender.FEMALE,
            age_range="elderly",
            source_description="deep baritone, measured and deliberate",
        )

        self.assertTrue(prompt.startswith("A clearly female elderly speaker."))
        self.assertNotIn("baritone", prompt.lower())
        self.assertIn("contralto", prompt.lower())
        self.assertTrue(warnings)

    def test_compiling_an_effective_prompt_does_not_duplicate_wrappers(self) -> None:
        source = "high-pitched and slightly nasal with youthful curiosity"
        first, _ = compile_effective_voice_prompt(
            gender=Gender.FEMALE,
            age_range="child",
            source_description=source,
            speaking_style="quick and inquisitive",
        )
        compiled = first + " Distinguishing direction: Smooth dark resonance and relaxed articulation."

        second, warnings = compile_effective_voice_prompt(
            gender=Gender.FEMALE,
            age_range="child",
            source_description=compiled,
            speaking_style="quick and inquisitive",
        )

        self.assertEqual(second.count("A clearly female child speaker."), 1)
        self.assertEqual(second.count("Speaking style:"), 1)
        self.assertEqual(second.count("Maintain this vocal identity"), 1)
        self.assertNotIn("Distinguishing direction:", second)
        self.assertTrue(any("previously compiled" in item for item in warnings))

    def test_cast_excludes_non_speaking_registry_entries(self) -> None:
        registry = CharacterRegistry(
            book_title="Test",
            book_author="Author",
            characters={
                "narrator": _character(
                    "narrator",
                    gender=Gender.FEMALE,
                    description="warm clear alto with measured pacing",
                ),
                "speaker": _character(
                    "speaker",
                    gender=Gender.MALE,
                    description="bright tenor with crisp articulation",
                ),
                "island": _character(
                    "island",
                    gender=Gender.OTHER,
                    description="ancient and mysterious",
                ),
            },
        )
        chapters = [
            ScriptChapter(
                chapter_number=1,
                chapter_title="Prologue",
                lines=[
                    ScriptLine(
                        line_id="ch01_0001",
                        speaker="narrator",
                        text="The story began.",
                    ),
                    ScriptLine(
                        line_id="ch01_0002",
                        speaker="speaker",
                        text="I was there.",
                    ),
                ],
            )
        ]
        speaking = speaking_character_ids(chapters)
        cast = build_voice_cast(
            project_id="test",
            registry=registry,
            speaking_ids=speaking,
            design_model="test-model",
        )

        self.assertEqual(speaking, {"narrator", "speaker"})
        self.assertEqual(
            set(cast["voices"]),
            {"narrator_female", "narrator_male", "speaker"},
        )
        self.assertEqual(
            cast["voices"]["narrator_male"]["assigned_characters"],
            ["narrator"],
        )
        self.assertEqual(
            cast["voices"]["narrator_female"]["assigned_characters"],
            [],
        )
        self.assertEqual(cast["voices"]["narrator_female"]["gender"], "female")
        self.assertEqual(cast["voices"]["narrator_male"]["gender"], "male")
        self.assertEqual(cast["non_speaking_characters"], ["island"])

    def test_narrator_is_required_for_announcements_without_narration_lines(self) -> None:
        registry = CharacterRegistry(
            book_title="Dialogue only",
            book_author="Author",
            characters={
                "narrator": _character(
                    "narrator",
                    gender=Gender.MALE,
                    description="warm measured baritone",
                ),
                "speaker": _character(
                    "speaker",
                    gender=Gender.FEMALE,
                    description="clear bright alto",
                ),
            },
        )
        chapters = [
            ScriptChapter(
                chapter_number=1,
                chapter_title="Only dialogue",
                lines=[
                    ScriptLine(
                        line_id="ch01_0001",
                        speaker="speaker",
                        text="Hello.",
                    )
                ],
            )
        ]

        required = required_voice_character_ids(chapters, registry)

        self.assertEqual(required, {"narrator", "speaker"})

    def test_selected_narrator_alternative_survives_cast_rebuild(self) -> None:
        narrator = _character(
            "narrator",
            gender=Gender.FEMALE,
            description="warm clear alto with measured pacing",
            voice_id="narrator_male",
        )
        registry = CharacterRegistry(
            book_title="Test",
            book_author="Author",
            characters={"narrator": narrator},
        )

        cast = build_voice_cast(
            project_id="test",
            registry=registry,
            speaking_ids={"narrator"},
            design_model="test-model",
        )

        self.assertEqual(
            set(cast["voices"]),
            {"narrator_female", "narrator_male"},
        )
        self.assertEqual(
            cast["voices"]["narrator_male"]["assigned_characters"],
            ["narrator"],
        )
        self.assertEqual(
            cast["voices"]["narrator_female"]["assigned_characters"],
            [],
        )

    def test_duplicate_profiles_receive_deterministic_contrast(self) -> None:
        description = "deep baritone, measured and deliberate with warmth"
        registry = CharacterRegistry(
            book_title="Test",
            book_author="Author",
            characters={
                "first": _character(
                    "first",
                    gender=Gender.MALE,
                    description=description,
                ),
                "second": _character(
                    "second",
                    gender=Gender.MALE,
                    description=description,
                ),
            },
        )

        cast = build_voice_cast(
            project_id="test",
            registry=registry,
            speaking_ids={"first", "second"},
            design_model="test-model",
        )

        first = cast["voices"]["first"]
        second = cast["voices"]["second"]
        self.assertNotEqual(
            first["effective_prompt"],
            second["effective_prompt"],
        )
        self.assertIn(
            "Distinguishing direction:",
            first["effective_prompt"],
        )
        self.assertIn(
            "Distinguishing direction:",
            second["effective_prompt"],
        )
        self.assertTrue(any("too similar" in warning for warning in second["warnings"]))
        self.assertNotEqual(
            first["design_fingerprint"],
            second["design_fingerprint"],
        )

    def test_near_duplicate_profiles_receive_contrast(self) -> None:
        registry = CharacterRegistry(
            book_title="Test",
            book_author="Author",
            characters={
                "starling": _character(
                    "starling",
                    gender=Gender.FEMALE,
                    description=("high-pitched and energetic with a hint of nervousness"),
                ),
                "tuka": _character(
                    "tuka",
                    gender=Gender.FEMALE,
                    description=("high-pitched and energetic with a touch of roughness"),
                ),
            },
        )

        cast = build_voice_cast(
            project_id="test",
            registry=registry,
            speaking_ids={"starling", "tuka"},
            design_model="test-model",
        )

        self.assertIn(
            "Distinguishing direction:",
            cast["voices"]["starling"]["effective_prompt"],
        )
        self.assertIn(
            "Distinguishing direction:",
            cast["voices"]["tuka"]["effective_prompt"],
        )
        self.assertTrue(cast["voices"]["tuka"]["warnings"])

    def test_every_cast_profile_receives_a_unique_palette_direction(self) -> None:
        registry = CharacterRegistry(
            book_title="Test",
            book_author="Author",
            characters={
                f"speaker_{index}": _character(
                    f"speaker_{index}",
                    gender=Gender.FEMALE,
                    description="clear female voice with measured pacing",
                )
                for index in range(12)
            },
        )
        cast = build_voice_cast(
            project_id="test",
            registry=registry,
            speaking_ids=set(registry.characters),
            design_model="test-model",
        )
        directions = {
            profile["effective_prompt"].split("Distinguishing direction: ", 1)[1] for profile in cast["voices"].values()
        }
        self.assertEqual(len(directions), 12)

    def test_shared_voice_uses_only_speaking_assignments(self) -> None:
        registry = CharacterRegistry(
            book_title="Test",
            book_author="Author",
            characters={
                "owner": _character(
                    "owner",
                    gender=Gender.FEMALE,
                    description="soft contralto with a calm cadence",
                ),
                "child": _character(
                    "child",
                    gender=Gender.FEMALE,
                    description="unused",
                    voice_id="owner",
                ),
                "silent": _character(
                    "silent",
                    gender=Gender.FEMALE,
                    description="unused",
                    voice_id="owner",
                ),
            },
        )
        cast = build_voice_cast(
            project_id="test",
            registry=registry,
            speaking_ids={"owner", "child"},
            design_model="test-model",
        )

        self.assertEqual(list(cast["voices"]), ["owner"])
        self.assertEqual(
            cast["voices"]["owner"]["assigned_characters"],
            ["child", "owner"],
        )
        self.assertEqual(cast["non_speaking_characters"], ["silent"])


class VoiceUploadValidationTests(unittest.TestCase):
    @staticmethod
    def _write_wave(path: Path, *, seconds: float, amplitude: int) -> None:
        sample_rate = 24000
        samples = array.array(
            "h",
            (
                int(amplitude * math.sin(2 * math.pi * 220 * index / sample_rate))
                for index in range(int(sample_rate * seconds))
            ),
        )
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(samples.tobytes())

    def test_clean_reference_is_accepted(self) -> None:
        from brain.dashboard.api.main import _inspect_pcm_voice

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "voice.wav"
            self._write_wave(path, seconds=3.2, amplitude=8000)
            info = _inspect_pcm_voice(path)

        self.assertEqual(info["sample_rate"], 24000)
        self.assertAlmostEqual(info["duration_seconds"], 3.2, places=1)

    def test_silent_reference_is_rejected(self) -> None:
        from brain.dashboard.api.main import _inspect_pcm_voice

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "silent.wav"
            self._write_wave(path, seconds=3.2, amplitude=0)
            with self.assertRaisesRegex(ValueError, "silent"):
                _inspect_pcm_voice(path)

    def test_uploaded_transcript_mismatch_fails_closed(self) -> None:
        from brain.dashboard.api.main import _uploaded_transcript_error

        error = _uploaded_transcript_error(
            SimpleNamespace(
                effective_text_error=0.45,
                wer=0.45,
                transcribed_text="  different   words  ",
            )
        )

        self.assertIn("does not match", error)
        self.assertIn("different words", error)

    def test_uploaded_transcript_orthographic_equivalence_is_accepted(self) -> None:
        from brain.dashboard.api.main import _uploaded_transcript_error

        error = _uploaded_transcript_error(
            SimpleNamespace(
                effective_text_error=0.0,
                wer=0.5,
                transcribed_text="lets go lets go lets go",
            )
        )

        self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
