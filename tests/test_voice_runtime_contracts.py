from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from shared.constants import Gender, ValidationStatus
from shared.models import (
    BookMetadata,
    Character,
    ExtractedBook,
    ExtractedChapter,
    ExportM4BResponse,
    QualityResult,
    MasterChapterResponse,
    VoiceFXSettings,
    BootstrapVoicesRequest,
    VoiceCandidate,
)
from brain.orchestrator.voice_client import VoiceClient
from brain.orchestrator.job_queue import JobQueue
from brain.orchestrator.pipeline import Pipeline
from voice.tts_server.qwen3_engine import Qwen3TTSEngine
from voice.tts_server.audio_effects import AudioPostProcessor
from voice.tts_server.voice_designer import VoiceDesigner


class _FakeLibrary:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.registered: dict | None = None

    def get_voice_path(self, project_id: str, character_id: str) -> Path:
        return self.root / project_id / f"{character_id}.wav"

    def register_voice(self, **values) -> None:
        self.registered = values


class VoiceModelResidencyTests(unittest.TestCase):
    def test_first_candidate_is_the_immediately_assignable_profile(self) -> None:
        self.assertEqual(VoiceDesigner._candidate_id("hero", 1), "hero")
        self.assertEqual(VoiceDesigner._candidate_id("hero", 2), "hero_cand2")
        with self.assertRaises(ValueError):
            VoiceDesigner._candidate_id("hero", 0)

    def test_voice_design_generation_does_not_load_clone_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = SimpleNamespace(is_loaded=False, load=Mock())
            library = _FakeLibrary(Path(directory))
            designer = VoiceDesigner(engine=engine, library=library)
            character = Character(
                id="speaker",
                name="Speaker",
                gender=Gender.FEMALE,
                age_range="adult",
                voice_description="clear contralto with measured pacing",
                test_sentence="This sentence is long enough for a voice reference.",
            )
            response = SimpleNamespace(status_code=200, text="")

            with patch("requests.post", return_value=response), patch(
                "soundfile.read", return_value=(np.zeros(24000), 24000)
            ):
                result = designer._generate_voice("project", "speaker", character)

            engine.load.assert_not_called()
            self.assertEqual(result.id, "speaker")
            self.assertIsNotNone(library.registered)

    def test_bootstrap_client_consumes_progress_stream(self) -> None:
        client = VoiceClient(retries=1)
        response = Mock()
        response.raise_for_status = Mock()
        response.iter_lines.return_value = [
            json.dumps(
                {
                    "type": "progress",
                    "data": {
                        "phase": "designing_references",
                        "completed": 1,
                        "total": 2,
                        "message": "Prepared 1 of 2 voice candidates",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "data": {
                        "status": "success",
                        "project_id": "book",
                        "voices_generated": {},
                        "cast_diagnostics": [],
                    },
                }
            ),
        ]
        stream_context = Mock()
        stream_context.__enter__ = Mock(return_value=response)
        stream_context.__exit__ = Mock(return_value=False)
        http_client = Mock()
        http_client.__enter__ = Mock(return_value=http_client)
        http_client.__exit__ = Mock(return_value=False)
        http_client.stream.return_value = stream_context
        progress = []

        with patch(
            "brain.orchestrator.voice_client.httpx.Client",
            return_value=http_client,
        ):
            result = client.bootstrap_voices(
                BootstrapVoicesRequest(project_id="book", characters={}),
                progress_callback=progress.append,
            )

        self.assertEqual(result.project_id, "book")
        self.assertEqual(progress[0]["phase"], "designing_references")
        self.assertEqual(progress[0]["completed"], 1)

    def test_elevated_wer_is_retained_with_warning_and_does_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock(speaker_embedding=Mock(return_value=[0.1, 0.2]), embedding_similarity=Mock(return_value=0.5))
            library = Mock(voice_exists=Mock(return_value=False), get_voice_path=Mock(return_value=Path(directory) / "speaker.wav"))
            validator = Mock(transcribe=Mock(return_value="transcribed words"), calculate_wer=Mock(return_value=0.45), is_loaded=False, unload=Mock())
            designer = VoiceDesigner(engine=engine, library=library, validator=validator, wer_threshold=0.20)
            character = Character(id="speaker", name="Speaker", gender=Gender.FEMALE, age_range="adult", voice_description="desc", test_sentence="some test words")

            with patch("subprocess.Popen") as mock_popen, patch("httpx.get") as mock_get, patch.object(designer, "_generate_voice") as mock_gen, patch.object(designer, "_acoustic_diagnostics", return_value=({}, [])):
                mock_proc = Mock(poll=Mock(return_value=None), terminate=Mock(), wait=Mock())
                mock_popen.return_value = mock_proc
                mock_get.return_value = Mock(status_code=200, json=Mock(return_value={"model_loaded": True}))

                wav_path = Path(directory) / "speaker.wav"
                wav_path.write_bytes(b"fake_wav_data")
                mock_gen.return_value = VoiceCandidate(id="speaker", file=str(wav_path), duration_seconds=5.0, sample_rate=24000)

                req = BootstrapVoicesRequest(project_id="test_proj", characters={"speaker": character})
                resp = designer.bootstrap_voices(req)
                self.assertEqual(resp.status, "success")
                speaker_result = resp.voices_generated["speaker"]
                self.assertEqual(len(speaker_result.candidates), 1)
                self.assertTrue(any("exceeded threshold" in w for w in speaker_result.candidates[0].warnings))



class CleanAudioPolicyTests(unittest.TestCase):
    def test_generation_skips_post_fx_by_default(self) -> None:
        engine = Qwen3TTSEngine(device="cpu")
        engine._is_loaded = True
        clean = np.linspace(-0.25, 0.25, 2400, dtype=np.float32)
        engine._generate = Mock(return_value=clean)
        engine.fx = Mock()

        result = engine.generate_speech(
            "A deliberately emotional line.",
            voice_reference_path="reference.wav",
            ref_text="Reference words.",
            emotion_instruction="somber reflective whisper",
            speed=0.85,
            voice_fx=VoiceFXSettings(pitch_semitones=-2.0, tone="warm"),
        )

        engine.fx.apply.assert_not_called()
        np.testing.assert_array_equal(result, clean)
        self.assertEqual(engine.last_generation_metrics["schema_version"], 1)
        self.assertEqual(engine.last_generation_metrics["text_parts"], 1)
        self.assertIn("autoregressive_generation_seconds", engine.last_generation_metrics)
        self.assertGreaterEqual(engine.last_generation_metrics["total_seconds"], 0.0)

    def test_generation_post_fx_requires_explicit_opt_in(self) -> None:
        engine = Qwen3TTSEngine(
            device="cpu",
            post_processing_config={"enabled": True},
        )
        engine._is_loaded = True
        clean = np.zeros(2400, dtype=np.float32)
        processed = np.ones(2400, dtype=np.float32) * 0.1
        engine._generate = Mock(return_value=clean)
        engine.fx = Mock()
        engine.fx.apply.return_value = processed

        result = engine.generate_speech(
            "A deliberately emotional line.",
            voice_reference_path="reference.wav",
            ref_text="Reference words.",
            emotion_instruction="somber reflective whisper",
        )

        engine.fx.apply.assert_called_once()
        np.testing.assert_array_equal(result, processed)

    def test_failed_generation_retains_partial_timing_metrics(self) -> None:
        engine = Qwen3TTSEngine(device="cpu")
        engine._is_loaded = True
        engine._generate = Mock(side_effect=RuntimeError("synthetic failure"))

        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
            engine.generate_speech(
                "A line that fails during synthesis.",
                voice_reference_path="reference.wav",
                ref_text="Reference words.",
            )

        self.assertGreaterEqual(
            engine.last_generation_metrics[
                "autoregressive_generation_seconds"
            ],
            0.0,
        )
        self.assertGreaterEqual(engine.last_generation_metrics["total_seconds"], 0.0)

    def test_phase_vocoder_is_not_an_implicit_fallback(self) -> None:
        processor = AudioPostProcessor()
        audio = np.linspace(-0.1, 0.1, 2400, dtype=np.float32)
        fx = VoiceFXSettings(speed=0.9)

        with (
            patch.object(processor, "_can_use_sox", return_value=False),
            patch.object(
                processor,
                "_apply_speed_pitch_librosa",
                wraps=processor._apply_speed_pitch_librosa,
            ) as phase_vocoder,
        ):
            result = processor.apply(audio, 24000, fx, blend_override=0.0)

        phase_vocoder.assert_not_called()
        np.testing.assert_array_equal(result, audio)

    def test_audio_policy_revision_changes_synthesis_context(self) -> None:
        clean = Qwen3TTSEngine(device="cpu").post_processing_context()
        experimental = Qwen3TTSEngine(
            device="cpu",
            post_processing_config={
                "enabled": True,
                "allow_phase_vocoder_fallback": True,
            },
        ).post_processing_context()

        self.assertEqual(clean["revision"], "clean-output-v1")
        self.assertFalse(clean["enabled"])
        self.assertNotEqual(clean, experimental)


class SpeakerEmbeddingTests(unittest.TestCase):
    def test_fresh_embedding_is_normalized_to_cpu_and_cached(self) -> None:
        try:
            import torch
        except ImportError:
            self.skipTest("torch is not installed in this environment")

        source_device = "cuda" if torch.cuda.is_available() else "cpu"

        class _SpeakerModel:
            speaker_encoder_sample_rate = 24000

            @staticmethod
            def extract_speaker_embedding(*, audio, sr):
                return torch.tensor([[1.0, 2.0, 3.0]], device=source_device)

        engine = Qwen3TTSEngine(device=source_device)
        engine._is_loaded = True
        engine._model = SimpleNamespace(model=_SpeakerModel())

        with tempfile.TemporaryDirectory() as directory:
            audio_path = Path(directory) / "reference.wav"
            with patch(
                "voice.tts_server.qwen3_engine.sf.read",
                return_value=(np.zeros(24000, dtype=np.float32), 24000),
            ):
                fresh = engine.speaker_embedding(audio_path)
            cached = engine.speaker_embedding(audio_path)

        self.assertEqual(fresh.device.type, "cpu")
        self.assertEqual(cached.device.type, "cpu")
        self.assertAlmostEqual(engine.embedding_similarity(fresh, cached), 1.0, places=6)


class QualityResultSchemaTests(unittest.TestCase):
    def test_validation_diagnostics_survive_serialization(self) -> None:
        result = QualityResult(
            line_id="line-1",
            status=ValidationStatus.PASS,
            wer=0.0,
            metrics={"duration_seconds": 1.5},
            passed_hard_gates=False,
        )

        payload = result.model_dump()
        self.assertEqual(payload["metrics"]["duration_seconds"], 1.5)
        self.assertFalse(payload["passed_hard_gates"])

    def test_mastering_join_diagnostics_survive_serialization(self) -> None:
        response = MasterChapterResponse(
            chapter_number=1,
            output_file="chapter.wav",
            join_warnings=1,
            join_diagnostics=[{"status": "warning", "line_id": "line-1"}],
        )

        payload = response.model_dump()
        self.assertEqual(payload["join_warnings"], 1)
        self.assertEqual(payload["join_diagnostics"][0]["line_id"], "line-1")

    def test_export_loudness_report_survives_serialization(self) -> None:
        response = ExportM4BResponse(
            output_file="book.m4b",
            book_loudness={"status": "consistent", "spread_lu": 0.3},
        )

        self.assertEqual(response.book_loudness["status"], "consistent")
        self.assertEqual(response.model_dump()["book_loudness"]["spread_lu"], 0.3)


class ProjectSourceContractTests(unittest.TestCase):
    def test_project_preserves_source_and_can_reextract_book(self) -> None:
        class _Parser:
            @staticmethod
            def parse(path):
                return ExtractedBook(
                    metadata=BookMetadata(
                        title="Source title",
                        author="Source author",
                        total_chapters=1,
                        total_words=2,
                    ),
                    chapters=[
                        ExtractedChapter(
                            number=1,
                            title="One",
                            text="Two words",
                            word_count=2,
                        )
                    ],
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "upload.epub"
            source.write_bytes(b"epub source")
            pipeline = Pipeline.__new__(Pipeline)
            pipeline.projects_dir = root / "projects"
            pipeline.projects_dir.mkdir()
            pipeline.config = {"dashboard": {"max_projects": 0}}
            pipeline.job_queue = JobQueue(str(root / "state.db"))
            pipeline.parser = _Parser()

            status = pipeline.create_project(source)
            project_dir = pipeline.projects_dir / status.project_id
            self.assertEqual(
                (project_dir / "source.epub").read_bytes(),
                b"epub source",
            )

            (project_dir / "book.json").unlink()
            pipeline.reextract_project(status.project_id)
            self.assertTrue((project_dir / "book.json").is_file())

    def test_reextract_rejects_legacy_project_without_deleting_book(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_dir = root / "legacy"
            project_dir.mkdir()
            book_path = project_dir / "book.json"
            book_path.write_text('{"preserved": true}', encoding="utf-8")
            pipeline = Pipeline.__new__(Pipeline)
            pipeline.projects_dir = root

            with self.assertRaises(FileNotFoundError):
                pipeline.reextract_project("legacy")

            self.assertEqual(book_path.read_text(encoding="utf-8"), '{"preserved": true}')


if __name__ == "__main__":
    unittest.main()
