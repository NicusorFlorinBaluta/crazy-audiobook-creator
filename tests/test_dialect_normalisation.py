"""Tests for dialect normalisation, short-line interjections, and glossary-only failure handling."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from shared.constants import ValidationStatus
from voice.validator.validation_loop import ValidationLoop
from voice.validator.whisper_validator import WhisperValidator


class DialectNormalisationTests(unittest.TestCase):
    """Tests verifying dialect normalisation and substring collision guards."""

    def test_dialect_dwarf_lines_normalize_identically(self):
        norm1 = WhisperValidator._normalize_text("Ye comin' by this on yer own, are ye?")
        norm2 = WhisperValidator._normalize_text("You coming by this on your own, are you?")
        self.assertEqual(norm1, norm2)

        norm3 = WhisperValidator._normalize_text("She telled ye that, did she?")
        norm4 = WhisperValidator._normalize_text("She told you that, did she?")
        self.assertEqual(norm3, norm4)

    def test_dialect_negative_substring_collision_guards(self):
        """Word boundary rules must NEVER match dialect terms inside longer words or common phrases."""
        self.assertEqual(WhisperValidator._normalize_text("lawyer"), "lawyer")
        self.assertEqual(WhisperValidator._normalize_text("yellow"), "yellow")
        self.assertEqual(WhisperValidator._normalize_text("emblem"), "emblem")
        self.assertEqual(WhisperValidator._normalize_text("often"), "often")
        self.assertNotIn("of clock", WhisperValidator._normalize_text("It is five o'clock"))
        self.assertNotIn("of", WhisperValidator._normalize_text("O, hear my plea").split()[:1])

    def test_dialect_additional_table_entries(self):
        self.assertEqual(WhisperValidator._normalize_text("d'ye know?"), "do you know")
        self.assertEqual(WhisperValidator._normalize_text("It ain't right"), "it is not right")
        self.assertEqual(WhisperValidator._normalize_text("Nay, brother"), "no brother")
        self.assertEqual(WhisperValidator._normalize_text("Take 'em all"), "take them all")


class MockWhisper(WhisperValidator):
    def __init__(self):
        self._is_loaded = True
        self.transcribed_text = ""

    def transcribe_strict(self, audio_file: str, language: str | None = None) -> str:
        return self.transcribed_text

    def transcribe(self, audio_file: str, language: str | None = None) -> str:
        return self.transcribed_text


class InterjectionAndGlossaryValidationTests(unittest.TestCase):
    """Tests for single-word interjection homophones and glossary-only miss handling."""

    def setUp(self):
        self.whisper = MockWhisper()

        self.loop = ValidationLoop(
            whisper=self.whisper,
            analyzer=MagicMock(),
            engine=MagicMock(),
            library=MagicMock(),
            wer_threshold=0.20,
        )
        self.loop.analyzer.noise_threshold = -50.0
        self.loop.analyzer.clipping_threshold = -0.5
        self.loop.analyzer.max_silence_seconds = 3.0
        self.loop.analyzer.duration_tolerance = 0.3
        self.loop.prosody_scorer = MagicMock()
        self.loop.prosody_scorer.enabled = False
        self.loop.prosody_scorer.analyze = MagicMock(return_value={})

    def _mock_analysis(self, duration: float = 1.0, clipping: bool = False):
        return {
            "duration_seconds": duration,
            "expected_duration_seconds": duration,
            "peak_dbfs": -1.0 if not clipping else 0.5,
            "noise_floor_db": -60.0,
            "clipping_detected": clipping,
            "duration_ok": True,
            "has_long_silence": False,
            "pacing_anomaly": False,
            "artifact_score": 1.0,
            "duration_score": 1.0,
        }

    def test_aye_against_transcript_i_passes_when_acoustics_clean(self):
        self.loop.analyzer.analyze.return_value = self._mock_analysis(duration=0.8, clipping=False)
        self.loop.analyzer.estimate_expected_duration.return_value = 0.8
        self.whisper.transcribed_text = "I"

        result = self.loop._validate_segment(
            audio_file="dummy.wav",
            expected_text="Aye,",
            line_id="line_aye_001",
            speed=1.0,
        )
        self.assertEqual(result.status, ValidationStatus.PASS)
        self.assertEqual(result.acceptance_reason, "approved_interjection_homophone")

    def test_aye_against_transcript_i_fails_when_clipping_detected(self):
        self.loop.analyzer.analyze.return_value = self._mock_analysis(duration=0.8, clipping=True)
        self.loop.analyzer.estimate_expected_duration.return_value = 0.8
        self.whisper.transcribed_text = "I"

        result = self.loop._validate_segment(
            audio_file="dummy.wav",
            expected_text="Aye,",
            line_id="line_aye_002",
            speed=1.0,
        )
        self.assertEqual(result.status, ValidationStatus.FAIL)

    def test_authored_yes_against_transcript_i_fails(self):
        """Authored 'Yes.' against transcript 'I' must FAIL (not pass as homophone)."""
        self.loop.analyzer.analyze.return_value = self._mock_analysis(duration=0.8, clipping=False)
        self.loop.analyzer.estimate_expected_duration.return_value = 0.8
        self.whisper.transcribed_text = "I"

        result = self.loop._validate_segment(
            audio_file="dummy.wav",
            expected_text="Yes.",
            line_id="line_yes_001",
            speed=1.0,
        )
        self.assertEqual(result.status, ValidationStatus.FAIL)

    def test_real_wer_failure_still_fails(self):
        """Bedorijay fumed. -> Better a jay fumed. fails validation."""
        self.loop.analyzer.analyze.return_value = self._mock_analysis(duration=1.5, clipping=False)
        self.loop.analyzer.estimate_expected_duration.return_value = 1.5
        self.whisper.transcribed_text = "Better a jay fumed."

        result = self.loop._validate_segment(
            audio_file="dummy.wav",
            expected_text="Bedorijay fumed.",
            line_id="line_bedorijay_001",
            speed=1.0,
            validation_terms={"Bedorijay"},
        )
        self.assertEqual(result.status, ValidationStatus.FAIL)
        self.assertTrue(result.glossary_only_miss)
        self.assertEqual(result.acceptance_reason, "glossary_only_miss")

    def test_glossary_only_miss_helper(self):
        self.assertTrue(
            self.loop._is_glossary_only_miss(
                "bedorijay fumed",
                "better a jay fumed",
                {"Bedorijay"},
            )
        )
        # Difference in non-glossary word should not qualify as glossary-only miss
        self.assertFalse(
            self.loop._is_glossary_only_miss(
                "bedorijay fumed",
                "better a jay shouted",
                {"Bedorijay"},
            )
        )
        # Sentence without any glossary terms should not qualify
        self.assertFalse(
            self.loop._is_glossary_only_miss(
                "the dwarf spat",
                "the dwarf spoke",
                {"Bedorijay"},
            )
        )

    def test_glossary_only_miss_excluded_from_retry_candidates(self):
        from shared.models import ScriptLine

        line1 = ScriptLine(line_id="line_01", chapter_number=1, speaker="narrator", text="Regular fail line")
        line2 = ScriptLine(line_id="line_02", chapter_number=1, speaker="narrator", text="Bedorijay fumed.")
        line3 = ScriptLine(line_id="line_03", chapter_number=1, speaker="narrator", text="Passed line")

        # Fake quality results for line1, line2, line3
        res1 = MagicMock(status=ValidationStatus.FAIL, glossary_only_miss=False)
        res2 = MagicMock(status=ValidationStatus.FAIL, glossary_only_miss=True)
        res3 = MagicMock(status=ValidationStatus.PASS, glossary_only_miss=False)

        quality_by_id = {"line_01": res1, "line_02": res2, "line_03": res3}
        lines = [line1, line2, line3]

        candidates = [
            line
            for line in lines
            if not self.loop._is_accepted(quality_by_id[line.line_id].status)
            and not quality_by_id[line.line_id].glossary_only_miss
        ]
        self.assertEqual([c.line_id for c in candidates], ["line_01"])


if __name__ == "__main__":
    unittest.main()
