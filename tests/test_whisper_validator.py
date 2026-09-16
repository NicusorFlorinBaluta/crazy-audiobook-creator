from __future__ import annotations

import sys
import unittest
from unittest.mock import Mock, patch

from voice.validator.whisper_validator import WhisperValidator


class WhisperValidatorTextTests(unittest.TestCase):
    def test_backend_selection_is_explicit_and_validated(self) -> None:
        validator = WhisperValidator(
            model_name="tiny",
            device="cpu",
            backend="openai_whisper",
        )
        self.assertEqual(validator.backend, "openai_whisper")
        with self.assertRaises(ValueError):
            WhisperValidator(backend="unsupported")

    def test_openai_backend_uses_raw_audio_when_vad_is_disabled(self) -> None:
        validator = WhisperValidator(
            model_name="tiny",
            device="cpu",
            backend="openai_whisper",
            vad_filter=False,
        )
        validator._backend = "openai_whisper"
        validator._is_loaded = True
        validator._model = Mock()
        validator._model.transcribe.return_value = {"text": " Uncle! "}

        result = validator.transcribe("short-line.wav", language="en")

        self.assertEqual(result, "Uncle!")
        validator._model.transcribe.assert_called_once()
        args, kwargs = validator._model.transcribe.call_args
        self.assertEqual(args, ("short-line.wav",), "raw audio, not a VAD-trimmed temp file")
        self.assertEqual(kwargs["language"], "en")

    def test_openai_backend_transcribes_deterministically(self) -> None:
        """The transcript is a measurement, so the instrument must not drift.

        Whisper's default `temperature` resamples at rising temperature when a
        segment trips its compression-ratio or logprob threshold, and
        `condition_on_previous_text` lets one line prime the next. Neither is
        wanted when the transcript is evidence about how a name was said.
        """
        validator = WhisperValidator(
            model_name="tiny",
            device="cpu",
            backend="openai_whisper",
            vad_filter=False,
        )
        validator._backend = "openai_whisper"
        validator._is_loaded = True
        validator._model = Mock()
        validator._model.transcribe.return_value = {"text": "Drizzit is defending."}

        validator.transcribe("line.wav", language="en")

        _args, kwargs = validator._model.transcribe.call_args
        self.assertEqual(kwargs["temperature"], 0.0)
        self.assertFalse(kwargs["condition_on_previous_text"])
        self.assertNotIn(
            "initial_prompt",
            kwargs,
            "priming Whisper with the glossary would erase the mispronunciation this measures",
        )

    def test_contraction_and_expanded_form_have_zero_wer(self) -> None:
        validator = WhisperValidator(model_name="tiny", device="cpu")
        self.assertEqual(
            validator.calculate_wer(
                "Let's go, let's go, let's go!",
                "Let us go! Let us go! Let us go!",
            ),
            0.0,
        )

    def test_contraction_normalization_does_not_require_whisper_package(self) -> None:
        validator = WhisperValidator(model_name="tiny", device="cpu")
        with patch.dict(sys.modules, {"whisper.normalizers": None}):
            self.assertEqual(
                validator.calculate_wer("Let's go!", "Let us go."),
                0.0,
            )

    def test_word_boundaries_and_punctuation_are_orthographically_equivalent(
        self,
    ) -> None:
        self.assertTrue(
            WhisperValidator.is_orthographic_segmentation_match(
                "Letsgoletsgoletsgo!",
                "Let's go, let's go, let's go!",
            )
        )

    def test_changed_letters_are_not_orthographically_equivalent(self) -> None:
        self.assertFalse(
            WhisperValidator.is_orthographic_segmentation_match(
                "Letsgoletsgoletsgo!",
                "Let's go, let's go, let's stop!",
            )
        )

    def test_empty_text_is_not_an_equivalent_spoken_line(self) -> None:
        self.assertFalse(WhisperValidator.is_orthographic_segmentation_match("", ""))


if __name__ == "__main__":
    unittest.main()
