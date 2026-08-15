from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
    AudiobookMetadata,
    ExportConfig,
    MasterChapterRequest,
    MasterSegmentInfo,
    ExportChapterInfo,
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
from shared.single_instance import SingleInstanceLock


class SingleInstanceTests(unittest.TestCase):
    def test_pipeline_lock_rejects_a_second_owner_without_erasing_pid(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"TEMP": directory}
        ):
            first = SingleInstanceLock("pipeline-test.lock")
            second = SingleInstanceLock("pipeline-test.lock")
            try:
                self.assertTrue(first.acquire())
                expected_pid = str(os.getpid())
                self.assertFalse(second.acquire())
                # Windows denies a second open while byte zero is locked, so
                # inspect the owner metadata through the lock owner's handle.
                first.handle.seek(0)
                self.assertEqual(first.handle.read(), expected_pid)
                first.release()
                self.assertTrue(second.acquire())
            finally:
                first.release()
                second.release()


class JobQueueTests(unittest.TestCase):
    def test_new_jobs_require_one_time_voice_review_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue = JobQueue(str(Path(directory) / "state.db"))
            queue.create_job("book", {"status": "created"})
            state = queue.get_job("book")
            self.assertEqual(state["voice_review_policy"], "required_once")
            self.assertEqual(state["voice_review_status"], "pending")
            self.assertFalse(state["voice_review_approved"])

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

    def test_project_quality_summary_uses_explicit_selected_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue = JobQueue(str(Path(directory) / "state.db"))
            queue.create_job("book", {"status": "created"})
            queue.log_quality(
                "book", "line-1", 1, 1, 0.0, 0.95, "pass",
                details={"selected": True},
            )
            queue.log_quality(
                "book", "line-1", 1, 2, 0.5, 0.10, "fail",
                details={"selected": False},
            )

            summary = queue.get_project_quality_summary("book")

            self.assertEqual(summary["passed"], 1)
            self.assertEqual(summary["failed"], 0)
            self.assertEqual(summary["total_retries"], 1)
            self.assertEqual(summary["average_wer"], 0.0)

    def test_review_disposition_is_persisted_and_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue = JobQueue(str(Path(directory) / "state.db"))
            queue.create_job("book", {"status": "created"})
            queue.set_review_item("book", "join", "ch01-2", "acceptable")
            queue.set_review_item(
                "book", "join", "ch01-2", "needs_remaster", "Level jump",
            )

            items = queue.get_review_items("book", "join")

            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["disposition"], "needs_remaster")
            self.assertEqual(items[0]["note"], "Level jump")


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
            self.assertGreater(metrics["f0_range_hz"], 0)
            self.assertGreater(metrics["spectral_centroid_hz"], 0)
            self.assertIn("silence_ratio", metrics)
            self.assertIn("clipping_fraction", metrics)
            self.assertTrue(any("unusually high" in item for item in warnings))

    def test_cast_pair_diagnostic_does_not_trust_requested_gender(self) -> None:
        diagnostic = VoiceDesigner._cast_pair_diagnostic(
            "male_requested",
            "female_requested",
            0.985,
            {
                "median_f0_hz": 175.0,
                "f0_range_hz": 55.0,
                "spectral_centroid_hz": 1450.0,
            },
            {
                "median_f0_hz": 180.0,
                "f0_range_hz": 58.0,
                "spectral_centroid_hz": 1500.0,
            },
            0.97,
        )

        self.assertEqual(diagnostic.status, "similar")
        self.assertFalse(diagnostic.warning_suppressed)

    def test_cast_pair_diagnostic_records_objective_contrast(self) -> None:
        diagnostic = VoiceDesigner._cast_pair_diagnostic(
            "left",
            "right",
            0.985,
            {
                "median_f0_hz": 110.0,
                "f0_range_hz": 45.0,
                "spectral_centroid_hz": 1050.0,
            },
            {
                "median_f0_hz": 230.0,
                "f0_range_hz": 100.0,
                "spectral_centroid_hz": 1900.0,
            },
            0.97,
        )

        self.assertEqual(diagnostic.status, "distinct")
        self.assertTrue(diagnostic.warning_suppressed)
        self.assertGreater(diagnostic.pitch_delta_hz, 40.0)


class ArtifactStateTests(unittest.TestCase):
    def test_mastering_resolves_approved_narrator_voice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_dir = Path(directory)
            atomic_write_json(
                project_dir / "voice_cast.json",
                {
                    "voices": {
                        "narrator_female": {
                            "assigned_characters": [],
                        },
                        "narrator_male": {
                            "assigned_characters": ["narrator"],
                        },
                    }
                },
            )

            selected = Pipeline._selected_narrator_voice_id(project_dir)

        self.assertEqual(selected, "narrator_male")

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


class BookLoudnessTests(unittest.TestCase):
    def test_consistent_chapters_pass_report_only_gate(self) -> None:
        report = M4BExporter._book_loudness_report(
            [
                ExportChapterInfo(number=1, title="One", file="one.wav", lufs=-19.2, peak_dbfs=-1.2),
                ExportChapterInfo(number=2, title="Two", file="two.wav", lufs=-18.8, peak_dbfs=-1.0),
                ExportChapterInfo(number=3, title="Three", file="three.wav", lufs=-19.0, peak_dbfs=-1.1),
            ]
        )

        self.assertEqual(report["status"], "consistent")
        self.assertAlmostEqual(report["spread_lu"], 0.4)
        self.assertEqual(report["outlier_chapters"], [])

    def test_loudness_spread_reports_outlier_without_rejecting_export(self) -> None:
        report = M4BExporter._book_loudness_report(
            [
                ExportChapterInfo(number=1, title="One", file="one.wav", lufs=-19.0),
                ExportChapterInfo(number=2, title="Two", file="two.wav", lufs=-21.0),
                ExportChapterInfo(number=3, title="Three", file="three.wav", lufs=-19.1),
            ]
        )

        self.assertEqual(report["status"], "warning")
        self.assertGreater(report["spread_lu"], 1.0)
        self.assertEqual(report["outlier_chapters"], [2])


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

    def test_join_diagnostics_flag_large_segment_loudness_change(self) -> None:
        assembler = AudioAssembler(crossfade_ms=0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sf.write(root / "quiet.wav", np.full(2400, 0.02, dtype=np.float32), 24000)
            sf.write(root / "loud.wav", np.full(2400, 0.4, dtype=np.float32), 24000)
            result = assembler.assemble_chapter(
                [
                    MasterSegmentInfo(
                        line_id="quiet",
                        file="quiet.wav",
                        pause_after_ms=300,
                    ),
                    MasterSegmentInfo(line_id="loud", file="loud.wav"),
                ],
                root,
            )

        self.assertEqual(result["join_warnings"], 1)
        self.assertEqual(
            result["join_diagnostics"][0]["reasons"],
            ["segment_loudness_delta"],
        )

    def test_join_diagnostics_keep_similar_segments_clean(self) -> None:
        assembler = AudioAssembler(crossfade_ms=0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sf.write(root / "one.wav", np.full(2400, 0.08, dtype=np.float32), 24000)
            sf.write(root / "two.wav", np.full(2400, 0.09, dtype=np.float32), 24000)
            result = assembler.assemble_chapter(
                [
                    MasterSegmentInfo(
                        line_id="one",
                        file="one.wav",
                        pause_after_ms=250,
                    ),
                    MasterSegmentInfo(line_id="two", file="two.wav"),
                ],
                root,
            )

        self.assertEqual(result["join_warnings"], 0)
        self.assertEqual(result["join_diagnostics"][0]["status"], "clean")

    def test_utterance_group_joins_without_pause_or_crossfade(self) -> None:
        assembler = AudioAssembler(crossfade_ms=20)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sf.write(root / "dialogue.wav", np.full(2400, 0.08, dtype=np.float32), 24000)
            sf.write(root / "tag.wav", np.full(2400, 0.09, dtype=np.float32), 24000)
            result = assembler.assemble_chapter(
                [
                    MasterSegmentInfo(
                        line_id="dialogue",
                        file="dialogue.wav",
                        pause_after_ms=400,
                        utterance_group_id="utterance_dialogue",
                    ),
                    MasterSegmentInfo(
                        line_id="tag",
                        file="tag.wav",
                        pause_before_ms=400,
                        utterance_group_id="utterance_dialogue",
                    ),
                ],
                root,
            )

        self.assertEqual(len(result["audio"]), 76800)
        diagnostic = result["join_diagnostics"][0]
        self.assertEqual(diagnostic["gap_ms"], 0)
        self.assertEqual(diagnostic["utterance_group_id"], "utterance_dialogue")
        self.assertFalse(diagnostic["crossfade_applied"])


class AudioAnalyzerTests(unittest.TestCase):
    def test_metrics_are_json_serializable_native_scalars(self) -> None:
        from voice.validator.audio_analyzer import AudioAnalyzer

        sample_rate = 24000
        audio = np.full(sample_rate, 0.05, dtype=np.float32)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native-scalars.wav"
            sf.write(path, audio, sample_rate)
            result = AudioAnalyzer().analyze(str(path), "hello world")

        json.dumps(result)
        self.assertIs(type(result["clipping_detected"]), bool)
        self.assertIs(type(result["duration_ok"]), bool)
        self.assertIs(type(result["peak_dbfs"]), float)
        self.assertIs(type(result["sample_rate"]), int)

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


class LoudnessNormalizerTests(unittest.TestCase):
    def test_soft_limiter_preserves_samples_below_knee(self) -> None:
        from voice.mastering.normalizer import LoudnessNormalizer

        audio = np.array([-0.3, -0.1, 0.0, 0.1, 0.3], dtype=np.float64)
        limited = LoudnessNormalizer._apply_soft_limiter(audio, 0.8)

        np.testing.assert_allclose(limited, audio)

    def test_soft_limiter_reduces_peaks_without_global_attenuation(self) -> None:
        from voice.mastering.normalizer import LoudnessNormalizer

        audio = np.array([0.1, 0.4, 0.75, 1.2], dtype=np.float64)
        limited = LoudnessNormalizer._apply_soft_limiter(audio, 0.8)

        self.assertEqual(limited[0], audio[0])
        self.assertEqual(limited[1], audio[1])
        self.assertLess(limited[-1], 0.8)
        self.assertGreater(limited[-1], limited[-2])


class FingerprintTests(unittest.TestCase):
    def test_synthesized_audio_can_resume_before_validation_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EmbeddingStore(root / "cache.db")
            audio = root / "line.wav"
            sf.write(audio, np.ones(2400, dtype=np.float32) * 0.01, 24000)
            context = {"voice_reference_hash": "voice-a", "model": "model"}
            store.save_synthesis_fingerprint(
                project_id="book",
                line_id="line",
                text="Text",
                speaker="speaker",
                emotion="neutral",
                speed=1.0,
                fx_dict=None,
                output_path=audio,
                duration_seconds=0.1,
                generation_context=context,
            )

            self.assertFalse(store.line_needs_synthesis(
                project_id="book", line_id="line", text="Text",
                speaker="speaker", emotion="neutral", speed=1.0,
                fx_dict=None, output_path=audio, generation_context=context,
            ))
            self.assertTrue(store.line_needs_regeneration(
                project_id="book", line_id="line", text="Text",
                speaker="speaker", emotion="neutral", speed=1.0,
                fx_dict=None, output_path=audio, generation_context=context,
            ))

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

    def test_validation_cache_is_independent_and_hash_checked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EmbeddingStore(root / "cache.db")
            audio = root / "line.wav"
            sf.write(audio, np.ones(2400, dtype=np.float32) * 0.01, 24000)
            context = {"validation_schema": "3", "whisper_model": "medium"}
            result = {"line_id": "line", "status": "pass", "wer": 0.0}
            store.save_validation_result(
                project_id="book",
                line_id="line",
                output_path=audio,
                expected_text="Text",
                validation_context=context,
                result=result,
            )
            self.assertEqual(
                store.get_validation_result(
                    project_id="book",
                    line_id="line",
                    output_path=audio,
                    expected_text="Text",
                    validation_context=context,
                ),
                result,
            )
            self.assertIsNone(
                store.get_validation_result(
                    project_id="book",
                    line_id="line",
                    output_path=audio,
                    expected_text="Changed",
                    validation_context=context,
                )
            )
            self.assertIsNone(
                store.get_validation_result(
                    project_id="book",
                    line_id="line",
                    output_path=audio,
                    expected_text="Text",
                    validation_context=dict(context, validation_schema="4"),
                )
            )
            sf.write(audio, np.ones(4800, dtype=np.float32) * 0.02, 24000)
            self.assertIsNone(
                store.get_validation_result(
                    project_id="book",
                    line_id="line",
                    output_path=audio,
                    expected_text="Text",
                    validation_context=context,
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

    def test_replacing_voice_cleans_superseded_audio_and_embedding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            library = VoiceLibraryManager(directory)
            project_dir = Path(directory) / "book"
            project_dir.mkdir()
            first = project_dir / "narrator_old.wav"
            first.write_bytes(b"old audio")
            first.with_suffix(".pt").write_bytes(b"old embedding")
            second = project_dir / "narrator_new.wav"
            second.write_bytes(b"new audio")

            values = {
                "project_id": "book",
                "character_id": "narrator",
                "name": "Narrator",
                "description": "Clear",
                "gender": "other",
                "duration_seconds": 1.0,
                "sample_rate": 24000,
                "ref_text": "Text",
            }
            library.register_voice(file_path=str(first), **values)
            library.register_voice(file_path=str(second), **values)

            self.assertFalse(first.exists())
            self.assertFalse(first.with_suffix(".pt").exists())
            self.assertTrue(second.exists())

    def test_delete_voice_cleans_embedding_after_registry_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            library = VoiceLibraryManager(directory)
            project_dir = Path(directory) / "book"
            project_dir.mkdir()
            audio = project_dir / "speaker.wav"
            audio.write_bytes(b"audio")
            audio.with_suffix(".pt").write_bytes(b"embedding")
            library.register_voice(
                project_id="book",
                character_id="speaker",
                name="Speaker",
                description="Clear",
                gender="other",
                file_path=str(audio),
                duration_seconds=1.0,
                sample_rate=24000,
                ref_text="Text",
            )

            library.delete_voice("book", "speaker")

            self.assertFalse(audio.exists())
            self.assertFalse(audio.with_suffix(".pt").exists())


class ExportMetadataTests(unittest.TestCase):
    def test_ffmetadata_control_characters_are_escaped(self) -> None:
        escaped = M4BExporter._escape_ffmetadata("A=B;C#D\\E\nF\r")
        self.assertEqual(escaped, "A\\=B\\;C\\#D\\\\E\\\nF")

    def test_export_embeds_reviewed_isbn(self) -> None:
        exporter = M4BExporter()
        with patch("voice.mastering.m4b_exporter.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            exporter._run_ffmpeg(
                concat_file=Path("concat.txt"),
                metadata_file=Path("chapters.txt"),
                output_file=Path("output.m4b"),
                book_metadata=AudiobookMetadata(
                    title="Book",
                    author="Author",
                    isbn="9780000000001",
                ),
                cover_art=None,
                config=ExportConfig(),
            )

        command = run.call_args.args[0]
        self.assertIn("isbn=9780000000001", command)
        self.assertIn("grouping=ISBN 9780000000001", command)
        self.assertIn("+faststart", command)


if __name__ == "__main__":
    unittest.main()
