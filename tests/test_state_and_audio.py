from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from brain.orchestrator.job_queue import JobQueue
from brain.orchestrator.pipeline import Pipeline
from shared.artifacts import (
    atomic_write_json,
    build_segment_manifest,
    finalize_segment_manifest,
    hash_file,
    manifest_path,
    master_manifest_path,
)
from shared.models import (
    MasterChapterRequest,
    MasterSegmentInfo,
    ScriptChapter,
    ScriptLine,
)
from voice.mastering.assembler import AudioAssembler
from voice.mastering.m4b_exporter import M4BExporter
from voice.tts_server.embedding_store import EmbeddingStore
from voice.tts_server.voice_designer import VoiceDesigner
from voice.tts_server.voice_library import VoiceLibraryManager
from shared.constants import Gender
from shared.models import Character


class JobQueueTests(unittest.TestCase):
    def test_delete_missing_job_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue = JobQueue(str(Path(directory) / "state.db"))
            with self.assertRaises(KeyError):
                queue.delete_job("missing")

    def test_project_quality_summary_uses_only_final_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue = JobQueue(str(Path(directory) / "state.db"))
            queue.create_job("book", {"status": "created"})
            queue.log_quality("book", "line-1", 1, 1, 0.8, 0.2, "fail")
            queue.log_quality("book", "line-1", 1, 2, 0.1, 0.9, "pass")
            queue.log_quality("book", "line-2", 1, 1, 0.0, 1.0, "pass")

            summary = queue.get_project_quality_summary("book")

            self.assertEqual(summary["total_segments"], 2)
            self.assertEqual(summary["passed"], 2)
            self.assertEqual(summary["total_retries"], 1)
            self.assertAlmostEqual(summary["average_wer"], 0.05)


class VoiceDiagnosticTests(unittest.TestCase):
    def test_obvious_pitch_mismatch_is_reported_as_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice.wav"
            sample_rate = 24000
            timeline = np.arange(sample_rate, dtype=np.float32) / sample_rate
            sf.write(path, 0.1 * np.sin(2 * np.pi * 300 * timeline), sample_rate)
            character = Character(
                id="speaker",
                name="Speaker",
                gender=Gender.MALE,
                age_range="adult",
                voice_description="low adult male voice",
            )

            metrics, warnings = VoiceDesigner._acoustic_diagnostics(
                path,
                character,
            )

            self.assertGreater(metrics["median_f0_hz"], 240)
            self.assertTrue(any("unusually high" in item for item in warnings))


class ArtifactStateTests(unittest.TestCase):
    def test_generated_and_mastered_are_reconciled_independently(self) -> None:
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.chdir(root)
            try:
                project_id = "book"
                project_dir = root / "projects" / project_id
                script_dir = project_dir / "script"
                script_dir.mkdir(parents=True)
                line = ScriptLine(
                    line_id="ch01_0000",
                    speaker="narrator",
                    voice_id="narrator",
                    text="Hello.",
                )
                chapter = ScriptChapter(
                    chapter_number=1,
                    chapter_title="One",
                    lines=[line],
                )
                script_file = script_dir / "chapter_001.json"
                script_file.write_text(
                    chapter.model_dump_json(),
                    encoding="utf-8",
                )
                segment = root / "workspace" / project_id / "segments" / "ch01_0000.wav"
                segment.parent.mkdir(parents=True)
                sf.write(segment, np.ones(2400, dtype=np.float32) * 0.01, 24000)
                manifest = finalize_segment_manifest(
                    build_segment_manifest(project_id, chapter),
                    root / "workspace",
                )
                atomic_write_json(manifest_path(project_dir, 1), manifest)

                pipeline = object.__new__(Pipeline)
                generated, mastered = pipeline._reconcile_artifacts(
                    project_id,
                    project_dir,
                    [script_file],
                )
                self.assertEqual(generated, [1])
                self.assertEqual(mastered, [])

                chapter_audio = (
                    root / "workspace" / project_id / "chapters" / "chapter_001.wav"
                )
                chapter_audio.parent.mkdir(parents=True)
                sf.write(
                    chapter_audio,
                    np.ones(4800, dtype=np.float32) * 0.01,
                    24000,
                )
                atomic_write_json(
                    master_manifest_path(project_dir, 1),
                    {
                        "segment_manifest_hash": manifest["manifest_hash"],
                        "dependency_hash": pipeline._master_dependency_hash(
                            project_id,
                            manifest["manifest_hash"],
                            MasterChapterRequest(
                                project_id=project_id,
                                chapter_number=1,
                                chapter_title="One",
                                segments=[
                                    MasterSegmentInfo(
                                        line_id="ch01_0000",
                                        file="book/segments/ch01_0000.wav",
                                    )
                                ],
                            ),
                        ),
                        "output_hash": hash_file(chapter_audio),
                    },
                )
                generated, mastered = pipeline._reconcile_artifacts(
                    project_id,
                    project_dir,
                    [script_file],
                )
                self.assertEqual(generated, [1])
                self.assertEqual(mastered, [1])

                sf.write(
                    segment,
                    np.ones(2400, dtype=np.float32) * 0.02,
                    24000,
                )
                generated, mastered = pipeline._reconcile_artifacts(
                    project_id,
                    project_dir,
                    [script_file],
                )
                self.assertEqual(generated, [])
                self.assertEqual(mastered, [])
            finally:
                os.chdir(old_cwd)


class AssemblerTests(unittest.TestCase):
    def test_missing_segment_is_fatal(self) -> None:
        assembler = AudioAssembler()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                assembler.assemble_chapter(
                    [
                        MasterSegmentInfo(
                            line_id="missing",
                            file="missing.wav",
                        )
                    ],
                    Path(directory),
                )


class AudioAnalyzerTests(unittest.TestCase):
    def test_short_spoken_line_allows_fixed_onset_duration(self) -> None:
        from voice.validator.audio_analyzer import AudioAnalyzer

        analyzer = AudioAnalyzer(duration_tolerance=0.6)
        sample_rate = 24000
        audio = np.full(int(sample_rate * 1.12), 0.05, dtype=np.float32)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "short.wav"
            sf.write(path, audio, sample_rate)
            result = analyzer.analyze(str(path), '"Now,"')

        self.assertTrue(result["duration_ok"])
        self.assertFalse(result["pacing_anomaly"])
        self.assertEqual(result["duration_score"], 1.0)

    def test_expected_duration_counts_hyphen_separated_words(self) -> None:
        from voice.validator.audio_analyzer import AudioAnalyzer

        self.assertEqual(
            AudioAnalyzer._expected_duration(
                "Starling-finally-felt that she belonged.",
                1.0,
            ),
            AudioAnalyzer._expected_duration(
                "Starling finally felt that she belonged.",
                1.0,
            ),
        )


class FingerprintTests(unittest.TestCase):
    def test_voice_reference_change_invalidates_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EmbeddingStore(root / "cache.db")
            audio = root / "line.wav"
            sf.write(audio, np.ones(2400, dtype=np.float32) * 0.01, 24000)
            context = {
                "voice_reference_hash": "voice-a",
                "model": "model",
            }
            store.save_generation_fingerprint(
                project_id="book",
                line_id="line",
                text="Text",
                speaker="speaker",
                emotion="neutral",
                speed=1.0,
                fx_dict=None,
                output_path=audio,
                validation_status="pass",
                generation_context=context,
            )
            self.assertFalse(
                store.line_needs_regeneration(
                    project_id="book",
                    line_id="line",
                    text="Text",
                    speaker="speaker",
                    emotion="neutral",
                    speed=1.0,
                    fx_dict=None,
                    output_path=audio,
                    generation_context=context,
                )
            )
            changed = dict(context, voice_reference_hash="voice-b")
            self.assertTrue(
                store.line_needs_regeneration(
                    project_id="book",
                    line_id="line",
                    text="Text",
                    speaker="speaker",
                    emotion="neutral",
                    speed=1.0,
                    fx_dict=None,
                    output_path=audio,
                    generation_context=changed,
                )
            )


class VoiceLibrarySafetyTests(unittest.TestCase):
    def test_project_and_character_traversal_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            library = VoiceLibraryManager(directory)
            with self.assertRaises(ValueError):
                library.get_voice_path("../outside", "narrator")
            with self.assertRaises(ValueError):
                library.get_voice_path("book", "../narrator")
            with self.assertRaises(ValueError):
                library.register_voice(
                    project_id="book",
                    character_id="narrator",
                    name="Narrator",
                    description="Clear",
                    gender="other",
                    file_path=str(Path(directory).parent / "outside.wav"),
                    duration_seconds=1.0,
                    sample_rate=24000,
                    ref_text="Text",
                )


class ExportMetadataTests(unittest.TestCase):
    def test_ffmetadata_control_characters_are_escaped(self) -> None:
        escaped = M4BExporter._escape_ffmetadata("A=B;C#D\\E\nF\r")
        self.assertEqual(escaped, "A\\=B\\;C\\#D\\\\E\\\nF")


if __name__ == "__main__":
    unittest.main()
