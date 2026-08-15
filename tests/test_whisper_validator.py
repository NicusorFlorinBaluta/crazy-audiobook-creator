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
        validator._model.transcribe.assert_called_once_with(
            "short-line.wav",
            language="en",
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
        self.assertFalse(
            WhisperValidator.is_orthographic_segmentation_match("", "")
        )


if __name__ == "__main__":
    unittest.main()
