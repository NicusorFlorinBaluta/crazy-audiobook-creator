from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from shared.constants import ValidationStatus
from shared.models import ScriptLine
from voice.validator.validation_loop import ValidationLoop
from voice.tts_server.embedding_store import EmbeddingStore


class FakeEngine:
    sample_rate = 24000
    model_name = "fake"
    generation_config = {}

    def __init__(self, fail_text: str | None = None):
        self.fail_text = fail_text
        self.is_loaded = True
        self.calls: list[str] = []
        self.similarity_calls = 0
        self.load_calls = 0
        self.unload_calls = 0

    def load(self) -> None:
        self.load_calls = getattr(self, "load_calls", 0) + 1
        self.is_loaded = True

    def unload(self) -> None:
        self.unload_calls = getattr(self, "unload_calls", 0) + 1
        self.is_loaded = False

    def generate_speech(self, *, text: str, output_path: Path, **kwargs):
        self.calls.append(text)
        if text == self.fail_text:
            raise RuntimeError("synthetic failure")
        audio = np.ones(2400, dtype=np.float32) * 0.01
        sf.write(output_path, audio, self.sample_rate)
        return audio

    def speaker_similarity(self, generated_audio_path, reference_audio_path) -> float:
        self.similarity_calls += 1
        return 0.95


class FakeWhisper:
    is_loaded = False

    def __init__(self) -> None:
        self.load_calls = 0
        self.unload_calls = 0

    def load(self) -> None:
        self.load_calls = getattr(self, "load_calls", 0) + 1
        self.is_loaded = True

    def unload(self) -> None:
        self.unload_calls = getattr(self, "unload_calls", 0) + 1
        self.is_loaded = False

    def transcribe(self, audio_file: str) -> str:
        return "hello"

    def calculate_wer(self, reference: str, hypothesis: str) -> float:
        return 0.0 if reference.lower() == hypothesis.lower() else 1.0

    def calculate_text_similarity(
        self,
        reference: str,
        hypothesis: str,
    ) -> float:
        return 1.0 if reference.lower() == hypothesis.lower() else 0.0

    @staticmethod
    def is_orthographic_segmentation_match(
        reference: str,
        hypothesis: str,
    ) -> bool:
        compact_reference = "".join(
            char for char in reference.casefold() if char.isalnum()
        )
        compact_hypothesis = "".join(
            char for char in hypothesis.casefold() if char.isalnum()
        )
        return bool(
            compact_reference
            and compact_reference == compact_hypothesis
        )

    @staticmethod
    def _normalize_text(text: str) -> str:
        return text.lower()


class FakeAnalyzer:
    noise_threshold = -50.0

    def analyze(self, audio_file: str, expected_text: str, speed: float):
        return {
            "artifact_score": 1.0,
            "duration_score": 1.0,
            "duration_seconds": 0.1,
            "expected_duration_seconds": 0.1,
            "duration_ok": True,
            "peak_dbfs": -20.0,
            "noise_floor_db": -80.0,
            "clipping_detected": False,
            "has_long_silence": False,
            "pacing_anomaly": False,
        }


class FlaggingAnalyzer(FakeAnalyzer):
    def analyze(self, audio_file: str, expected_text: str, speed: float):
        result = super().analyze(audio_file, expected_text, speed)
        result["duration_ok"] = False
        return result


class FakeLibrary:
    def __init__(self, reference: Path):
        self.reference = reference

    def get_voice_path(self, project_id: str, speaker: str) -> Path:
        return self.reference

    def get_voice_ref_text(self, project_id: str, speaker: str) -> str:
        return "reference"


class ValidationLoopTests(unittest.TestCase):
    def make_loop(self, root: Path, fail_text: str | None = None):
        reference = root / "reference.wav"
        sf.write(reference, np.ones(2400, dtype=np.float32) * 0.01, 24000)
        engine = FakeEngine(fail_text=fail_text)
        loop = ValidationLoop(
            whisper=FakeWhisper(),
            analyzer=FakeAnalyzer(),
            engine=engine,
            library=FakeLibrary(reference),
            max_retries=2,
        )
        return loop, engine

    def test_retries_progressively_reduce_extreme_delivery(self) -> None:
        line = ScriptLine(
            line_id="ch01_0000",
            speaker="starling",
            text="Uncle!",
            emotion="excited shout",
            speed=1.25,
        )

        emotion2, speed2, fx2 = ValidationLoop._retry_delivery(line, 2)
        emotion3, speed3, fx3 = ValidationLoop._retry_delivery(line, 3)

        self.assertEqual(emotion2, "neutral clear articulation")
        self.assertGreater(speed2, 1.0)
        self.assertLess(speed2, line.speed)
        self.assertIs(fx2, line.voice_fx)
        self.assertEqual(emotion3, "neutral clear articulation")
        self.assertEqual(speed3, 1.0)
        self.assertIsNone(fx3)

    def test_risk_aware_first_attempt_is_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loop, _ = self.make_loop(Path(directory))
            line = ScriptLine(
                line_id="ch01_0000",
                speaker="starling",
                text="UNCLE!",
                emotion="panicked shout",
                speed=1.25,
            )
            text, emotion, speed, _, reason = loop._initial_delivery(line, set())

        self.assertEqual(text, "UNCLE!")
        self.assertEqual(emotion, "panicked shout")
        self.assertEqual(speed, 1.25)
        self.assertIsNone(reason)

    def test_spoken_text_is_used_for_synthesis_without_changing_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loop, _ = self.make_loop(Path(directory))
            line = ScriptLine(
                line_id="ch01_0000",
                speaker="narrator",
                text="Patji arrived.",
                spoken_text="Pah-chee arrived.",
            )

            text, emotion, speed, fx, reason = loop._initial_delivery(line, set())

        self.assertEqual(text, "Pah-chee arrived.")
        self.assertEqual(line.text, "Patji arrived.")
        self.assertEqual(emotion, line.emotion)
        self.assertEqual(speed, line.speed)
        self.assertEqual(fx, line.voice_fx)
        self.assertIsNone(reason)

    def test_spoken_text_is_also_used_as_validation_reference(self) -> None:
        class SpokenTextWhisper(FakeWhisper):
            def transcribe(self, audio_file: str) -> str:
                return "Pah-chee arrived."

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loop, _ = self.make_loop(root)
            loop.whisper = SpokenTextWhisper()
            response = loop.process_chapter(
                project_id="book",
                chapter_number=1,
                lines=[
                    ScriptLine(
                        line_id="ch01_0000",
                        speaker="narrator",
                        text="Patji arrived.",
                        spoken_text="Pah-chee arrived.",
                    )
                ],
                workspace=root,
            )

        self.assertEqual(response.status, "success")
        self.assertEqual(response.failed_line_ids, [])

    def test_risk_aware_short_shout_prefers_clear_emphatic_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loop, _ = self.make_loop(Path(directory))
            loop.risk_aware_first_attempt = True
            line = ScriptLine(
                line_id="ch01_0000",
                speaker="starling",
                text="UNCLE!",
                emotion="panicked shout",
                speed=1.25,
            )
            text, emotion, speed, fx, reason = loop._initial_delivery(line, set())

        self.assertEqual(text, "uncle!")
        self.assertEqual(emotion, "clear emphatic delivery")
        self.assertEqual(speed, 1.0)
        self.assertIsNone(fx)
        self.assertEqual(reason, "short_expressive")

    def test_risk_aware_policy_does_not_modify_dense_glossary_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loop, _ = self.make_loop(Path(directory))
            loop.risk_aware_first_attempt = True
            line = ScriptLine(
                line_id="ch03_0120",
                speaker="narrator",
                text="Patji. King of the Pantheon. God of the Eelakin.",
                emotion="solemn",
                speed=1.0,
            )
            text, emotion, speed, _, reason = loop._initial_delivery(
                line,
                {"Patji", "Eelakin"},
            )

        self.assertEqual(text, line.text)
        self.assertEqual(emotion, "solemn")
        self.assertEqual(speed, 1.0)
        self.assertIsNone(reason)

    def test_final_retry_uses_plain_normalized_synthesis_text(self) -> None:
        class PlainTextWhisper(FakeWhisper):
            @staticmethod
            def _normalize_text(text: str) -> str:
                return "uncle she shouted"

        line = ScriptLine(
            line_id="ch01_0046",
            speaker="narrator",
            text="\"UNCLE!\" she shouted.",
            emotion="panicked shout",
            speed=1.15,
        )

        with tempfile.TemporaryDirectory() as directory:
            loop, _ = self.make_loop(Path(directory))
            loop.whisper = PlainTextWhisper()
            self.assertEqual(loop._retry_synthesis_text(line, 2), line.text)
            self.assertEqual(
                loop._retry_synthesis_text(line, 3),
                "uncle she shouted",
            )

    def test_concatenated_repetitions_are_separated_for_synthesis(self) -> None:
        self.assertEqual(
            ValidationLoop._prepare_synthesis_text(
                "\"Letsgoletsgoletsgo!\""
            ),
            "\"Let's go, let's go, let's go!\"",
        )

    def test_two_part_word_is_not_rewritten_for_synthesis(self) -> None:
        self.assertEqual(
            ValidationLoop._prepare_synthesis_text("couscous"),
            "couscous",
        )

    def test_stretched_single_letter_is_not_rewritten_for_synthesis(self) -> None:
        self.assertEqual(
            ValidationLoop._prepare_synthesis_text("Nooooo!"),
            "Nooooo!",
        )

    def test_word_boundary_only_transcript_difference_passes(self) -> None:
        class SegmentationWhisper(FakeWhisper):
            def transcribe(self, audio_file: str) -> str:
                return "Let's go, let's go, let's go!"

            def calculate_wer(self, reference: str, hypothesis: str) -> float:
                return 1.0

            def calculate_text_similarity(
                self,
                reference: str,
                hypothesis: str,
            ) -> float:
                return 0.85

            @staticmethod
            def _normalize_text(text: str) -> str:
                return text.casefold()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loop, _ = self.make_loop(root)
            loop.whisper = SegmentationWhisper()
            audio = root / "line.wav"
            sf.write(audio, np.ones(2400, dtype=np.float32) * 0.01, 24000)
            result = loop._validate_segment(
                str(audio),
                "Letsgoletsgoletsgo!",
                "ch01_0068",
                1.0,
            )

        self.assertEqual(result.status, ValidationStatus.PASS)
        self.assertEqual(result.effective_text_error, 0.0)
        self.assertEqual(
            result.acceptance_reason,
            "orthographic_segmentation_equivalent",
        )

    def test_changed_word_is_not_a_segmentation_match(self) -> None:
        class ChangedWordWhisper(FakeWhisper):
            def transcribe(self, audio_file: str) -> str:
                return "Let's go, let's go, let's stop!"

            def calculate_wer(self, reference: str, hypothesis: str) -> float:
                return 1.0

            def calculate_text_similarity(
                self,
                reference: str,
                hypothesis: str,
            ) -> float:
                return 0.80

            @staticmethod
            def _normalize_text(text: str) -> str:
                return text.casefold()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loop, _ = self.make_loop(root)
            loop.whisper = ChangedWordWhisper()
            audio = root / "line.wav"
            sf.write(audio, np.ones(2400, dtype=np.float32) * 0.01, 24000)
            result = loop._validate_segment(
                str(audio),
                "Letsgoletsgoletsgo!",
                "ch01_0068",
                1.0,
            )

        self.assertEqual(result.status, ValidationStatus.FAIL)

    def test_configured_wer_threshold_applies_to_longer_lines(self) -> None:
        class ThresholdWhisper(FakeWhisper):
            def transcribe(self, audio_file: str) -> str:
                return "synthetic transcript"

            def calculate_wer(self, reference: str, hypothesis: str) -> float:
                return 0.143

            def calculate_text_similarity(
                self,
                reference: str,
                hypothesis: str,
            ) -> float:
                return 0.5

            @staticmethod
            def _normalize_text(text: str) -> str:
                return text

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loop, _ = self.make_loop(root)
            loop.whisper = ThresholdWhisper()
            audio = root / "line.wav"
            sf.write(audio, np.ones(2400, dtype=np.float32) * 0.01, 24000)
            result = loop._validate_segment(
                str(audio),
                "one two three four five six seven",
                "ch01_0000",
                1.0,
            )

        self.assertEqual(result.status, ValidationStatus.PASS)

    def test_spelling_variant_can_pass_when_compact_text_matches(self) -> None:
        class VariantWhisper(FakeWhisper):
            def transcribe(self, audio_file: str) -> str:
                return "Tuca noted"

            def calculate_wer(self, reference: str, hypothesis: str) -> float:
                return 0.5

            def calculate_text_similarity(
                self,
                reference: str,
                hypothesis: str,
            ) -> float:
                return 0.89

            @staticmethod
            def _normalize_text(text: str) -> str:
                return text.lower()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loop, _ = self.make_loop(root)
            loop.whisper = VariantWhisper()
            audio = root / "line.wav"
            sf.write(audio, np.ones(2400, dtype=np.float32) * 0.01, 24000)
            result = loop._validate_segment(
                str(audio),
                "Tuka noted",
                "ch01_0000",
                1.0,
                validation_terms={"Tuka"},
            )

        self.assertEqual(result.status, ValidationStatus.PASS)

    def test_spelling_variant_without_approved_term_is_rejected(self) -> None:
        class VariantWhisper(FakeWhisper):
            def transcribe(self, audio_file: str) -> str:
                return "ordinary noted"

            def calculate_wer(self, reference: str, hypothesis: str) -> float:
                return 0.5

            def calculate_text_similarity(
                self,
                reference: str,
                hypothesis: str,
            ) -> float:
                return 0.89

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loop, _ = self.make_loop(root)
            loop.whisper = VariantWhisper()
            audio = root / "line.wav"
            sf.write(audio, np.ones(2400, dtype=np.float32) * 0.01, 24000)
            result = loop._validate_segment(
                str(audio),
                "ordinarie noted",
                "ch01_0000",
                1.0,
            )

        self.assertEqual(result.status, ValidationStatus.FAIL)

    def test_multiple_phonetic_glossary_spellings_preserve_sentence(self) -> None:
        class FictionalNamesWhisper(FakeWhisper):
            def transcribe(self, audio_file: str) -> str:
                return "Pachi King of the Pantheon, God of the Ilekin."

            def calculate_wer(self, reference: str, hypothesis: str) -> float:
                return 2 / 9

            def calculate_text_similarity(
                self,
                reference: str,
                hypothesis: str,
            ) -> float:
                return 0.84

            @staticmethod
            def _normalize_text(text: str) -> str:
                return re.sub(r"[^a-z\s]", "", text.lower())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loop, _ = self.make_loop(root)
            loop.whisper = FictionalNamesWhisper()
            audio = root / "line.wav"
            sf.write(audio, np.ones(2400, dtype=np.float32) * 0.01, 24000)
            result = loop._validate_segment(
                str(audio),
                "Patji. King of the Pantheon. God of the Eelakin.",
                "ch03_0120",
                1.0,
                validation_terms={"Patji", "Eelakin"},
            )

        self.assertEqual(result.status, ValidationStatus.PASS)
        self.assertEqual(result.effective_text_error, 0.0)
        self.assertEqual(
            result.acceptance_reason,
            "approved_glossary_spelling_variant",
        )

    def test_glossary_spelling_does_not_hide_ordinary_word_change(self) -> None:
        class ChangedProseWhisper(FakeWhisper):
            def transcribe(self, audio_file: str) -> str:
                return "Pachi Queen of the Pantheon, Lord of the Ilekin."

            def calculate_wer(self, reference: str, hypothesis: str) -> float:
                return 4 / 9

            def calculate_text_similarity(
                self,
                reference: str,
                hypothesis: str,
            ) -> float:
                return 0.80

            @staticmethod
            def _normalize_text(text: str) -> str:
                return re.sub(r"[^a-z\s]", "", text.lower())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loop, _ = self.make_loop(root)
            loop.whisper = ChangedProseWhisper()
            audio = root / "line.wav"
            sf.write(audio, np.ones(2400, dtype=np.float32) * 0.01, 24000)
            result = loop._validate_segment(
                str(audio),
                "Patji. King of the Pantheon. God of the Eelakin.",
                "ch03_0120",
                1.0,
                validation_terms={"Patji", "Eelakin"},
            )

        self.assertEqual(result.status, ValidationStatus.FAIL)

    def test_missing_generation_is_reported_as_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loop, _ = self.make_loop(root, fail_text="fail")
            response = loop.process_chapter(
                project_id="book",
                chapter_number=1,
                lines=[
                    ScriptLine(
                        line_id="ch01_0000",
                        speaker="narrator",
                        text="fail",
                    )
                ],
                workspace=root,
                max_retries=2,
            )
            self.assertEqual(response.status, "failed")
            self.assertEqual(response.generated, 0)
            self.assertEqual(response.failed_line_ids, ["ch01_0000"])

    def test_cache_skip_does_not_shift_progress_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loop, engine = self.make_loop(root)
            segments = root / "book" / "segments"
            segments.mkdir(parents=True)
            sf.write(
                segments / "ch01_0000.wav",
                np.ones(2400, dtype=np.float32) * 0.01,
                24000,
            )
            progress: list[dict] = []
            response = loop.process_chapter(
                project_id="book",
                chapter_number=1,
                lines=[
                    ScriptLine(
                        line_id="ch01_0000",
                        speaker="narrator",
                        text="hello",
                    ),
                    ScriptLine(
                        line_id="ch01_0001",
                        speaker="narrator",
                        text="hello",
                    ),
                ],
                workspace=root,
                progress_callback=progress.append,
            )
            self.assertEqual(response.status, "success")
            self.assertEqual(
                response.generated_line_ids,
                ["ch01_0000", "ch01_0001"],
            )
            self.assertEqual(
                [
                    message["line_id"]
                    for message in progress
                    if message.get("phase") == "synthesis"
                ],
                ["ch01_0000", "ch01_0001"],
            )
            self.assertEqual(
                [message["line_id"] for message in progress if message.get("phase") == "validation"],
                ["ch01_0000", "ch01_0001"],
            )
            self.assertEqual(engine.calls, ["hello"])

    def test_accepted_validation_cache_skips_whisper_and_similarity(self) -> None:
        class CountingWhisper(FakeWhisper):
            model_name = "fake"

            def __init__(self) -> None:
                self.transcriptions = 0

            def transcribe(self, audio_file: str) -> str:
                self.transcriptions += 1
                return "hello"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.wav"
            sf.write(reference, np.ones(2400, dtype=np.float32) * 0.01, 24000)
            engine = FakeEngine()
            whisper = CountingWhisper()
            loop = ValidationLoop(
                whisper=whisper,
                analyzer=FakeAnalyzer(),
                engine=engine,
                library=FakeLibrary(reference),
                embedding_store=EmbeddingStore(root / "cache.db"),
            )
            line = ScriptLine(
                line_id="ch01_0000",
                speaker="narrator",
                text="hello",
            )

            first = loop.process_chapter(
                project_id="book",
                chapter_number=1,
                lines=[line],
                workspace=root,
            )
            synthesis_calls = list(engine.calls)
            similarity_calls = engine.similarity_calls
            second = loop.process_chapter(
                project_id="book",
                chapter_number=1,
                lines=[line],
                workspace=root,
            )

            self.assertEqual(first.validation_cache_hits, 0)
            self.assertEqual(first.validation_cache_misses, 1)
            self.assertEqual(second.validation_cache_hits, 1)
            self.assertEqual(second.validation_cache_misses, 0)
            self.assertEqual(engine.calls, synthesis_calls)
            self.assertEqual(engine.similarity_calls, similarity_calls)
            self.assertEqual(whisper.transcriptions, 1)
            self.assertGreater(first.timings_seconds["total"], 0.0)
            self.assertIn("whisper_transcription", first.timings_seconds)
            self.assertIn("validation_cache_lookup", second.timings_seconds)
            self.assertNotIn("whisper_transcription", second.timings_seconds)
            measured = sum(
                value
                for key, value in first.timings_seconds.items()
                if key != "total"
            )
            self.assertLessEqual(measured, first.timings_seconds["total"] * 1.1)

            third = loop.process_chapter(
                project_id="book",
                chapter_number=1,
                lines=[line],
                workspace=root,
                validation_terms={"hello"},
            )
            self.assertEqual(third.validation_cache_hits, 0)
            self.assertEqual(third.validation_cache_misses, 1)
            self.assertEqual(engine.calls, synthesis_calls)
            self.assertEqual(whisper.transcriptions, 2)

            fourth = loop.process_chapter(
                project_id="book",
                chapter_number=1,
                lines=[line],
                workspace=root,
                validation_terms={"hello"},
                validation_revision="manual-reset-1",
            )
            self.assertEqual(fourth.validation_cache_hits, 0)
            self.assertEqual(fourth.validation_cache_misses, 1)
            self.assertEqual(engine.calls, synthesis_calls)
            self.assertEqual(whisper.transcriptions, 3)

    def test_resume_reuses_line_checkpointed_before_callback_crash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.wav"
            sf.write(reference, np.ones(2400, dtype=np.float32) * 0.01, 24000)
            engine = FakeEngine()
            store = EmbeddingStore(root / "cache.db")
            loop = ValidationLoop(
                whisper=FakeWhisper(), analyzer=FakeAnalyzer(), engine=engine,
                library=FakeLibrary(reference), embedding_store=store,
            )
            lines = [
                ScriptLine(line_id="ch01_0000", speaker="narrator", text="hello"),
                ScriptLine(line_id="ch01_0001", speaker="narrator", text="hello"),
            ]

            def crash_after_first_checkpoint(message: dict) -> None:
                if message.get("phase") == "validation":
                    raise RuntimeError("injected callback crash")

            with self.assertRaisesRegex(RuntimeError, "injected callback crash"):
                loop.process_chapter(
                    project_id="book", chapter_number=1, lines=lines,
                    workspace=root, progress_callback=crash_after_first_checkpoint,
                )

            response = loop.process_chapter(
                project_id="book", chapter_number=1, lines=lines, workspace=root,
            )

            self.assertEqual(response.status, "success")
            self.assertEqual(response.synthesis_cache_hits, 2)
            self.assertEqual(response.synthesis_cache_misses, 0)
            self.assertEqual(response.validation_cache_hits, 1)
            self.assertEqual(response.validation_cache_misses, 1)
            self.assertEqual(engine.calls, ["hello", "hello"])

    def test_perfect_transcript_with_soft_duration_warning_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loop, _ = self.make_loop(root)
            loop.analyzer = FlaggingAnalyzer()
            response = loop.process_chapter(
                project_id="book",
                chapter_number=1,
                lines=[
                    ScriptLine(
                        line_id="ch01_0000",
                        speaker="narrator",
                        text="hello",
                    )
                ],
                workspace=root,
            )
            self.assertEqual(response.status, "success")
            self.assertEqual(response.failed_line_ids, [])
            self.assertEqual(response.accepted_with_warning, 1)
            final = response.quality_results[-1]
            self.assertEqual(
                final.status,
                ValidationStatus.ACCEPTED_WITH_WARNING,
            )
            self.assertEqual(
                final.acceptance_reason,
                "accepted_soft_audio_warning",
            )

    def test_chapter_boundary_defers_nonresident_tts_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loop, engine = self.make_loop(root)

            response = loop.process_chapter(
                project_id="book",
                chapter_number=1,
                lines=[
                    ScriptLine(
                        line_id="ch01_0000",
                        speaker="narrator",
                        text="hello",
                    )
                ],
                workspace=root,
            )

        self.assertEqual(response.status, "success")
        self.assertEqual(engine.unload_calls, 1)
        self.assertEqual(engine.load_calls, 0)
        self.assertFalse(engine.is_loaded)

    def test_co_residency_is_released_at_chapter_boundary(self) -> None:
        class WrongWhisper(FakeWhisper):
            def transcribe(self, audio_file: str) -> str:
                return "different"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.wav"
            sf.write(reference, np.ones(2400, dtype=np.float32) * 0.01, 24000)
            engine = FakeEngine()
            whisper = WrongWhisper()
            loop = ValidationLoop(
                whisper=whisper,
                analyzer=FakeAnalyzer(),
                engine=engine,
                library=FakeLibrary(reference),
                max_retries=2,
                keep_models_resident=True,
            )

            loop.process_chapter(
                project_id="book",
                chapter_number=1,
                lines=[
                    ScriptLine(
                        line_id="ch01_0000",
                        speaker="narrator",
                        text="hello",
                    )
                ],
                workspace=root,
            )

        self.assertEqual(engine.unload_calls, 0)
        self.assertEqual(whisper.unload_calls, 1)
        self.assertFalse(whisper.is_loaded)
        self.assertTrue(engine.is_loaded)


if __name__ == "__main__":
    unittest.main()
