from __future__ import annotations

import unittest

from voice.validator.whisper_validator import WhisperValidator


class WhisperValidatorTextTests(unittest.TestCase):
    def test_contraction_and_expanded_form_have_zero_wer(self) -> None:
        validator = WhisperValidator(model_name="tiny", device="cpu")
        self.assertEqual(
            validator.calculate_wer(
                "Let's go, let's go, let's go!",
                "Let us go! Let us go! Let us go!",
            ),
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
