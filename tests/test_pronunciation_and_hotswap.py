"""Unit and integration tests for pronunciation replacements, previewing, and audio hot-swapping."""

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from shared.pronunciation import (
    apply_pronunciations,
    build_pronunciation_inventory,
    load_pronunciation_dictionary,
)
from shared.models import (
    ScriptLine,
    ScriptChapter,
    GenerateLineRequest,
    GenerateLineResponse,
    QualityResult,
)
import brain.dashboard.api.main as dashboard
from voice.mastering.assembler import AudioAssembler


def _make_dummy_wav(path: Path, duration_s: float = 0.5, sample_rate: int = 24000) -> None:
    """Create a minimal valid PCM WAV file for audio tests."""
    import wave
    import struct
    path.parent.mkdir(parents=True, exist_ok=True)
    num_samples = int(duration_s * sample_rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        # Write quiet sine/tone samples
        data = struct.pack(f"<{num_samples}h", *([1000] * num_samples))
        wf.writeframes(data)


class PronunciationAndHotSwapTests(unittest.IsolatedAsyncioTestCase):
    """Test suite covering pronunciation replacements, preview API, line-level caching, and hot-swap."""

    def test_pronunciation_replacement_rules(self) -> None:
        """Verify word-boundary aware case-insensitive pronunciation replacement."""
        p_dict = {
            "homeisle": "home-aisle",
            "homeisler": "home-aisler",
            "homeisles": "home-aisles",
            "homeislers": "home-aislers",
            "kokerlii": "Koh-ker-lee",
        }

        # Exact match
        self.assertEqual(
            apply_pronunciations("The homeisle was quiet.", p_dict),
            "The home-aisle was quiet.",
        )
        # Plural and agent noun matches
        self.assertEqual(
            apply_pronunciations("Two Homeislers met on the homeisles.", p_dict),
            "Two home-aislers met on the home-aisles.",
        )
        # Capitalized proper noun
        self.assertEqual(
            apply_pronunciations("Kokerlii flew overhead.", p_dict),
            "Koh-ker-lee flew overhead.",
        )
        # Substring inside another word must NOT be erroneously replaced
        self.assertEqual(
            apply_pronunciations("The word unhomeisled is untouched.", p_dict),
            "The word unhomeisled is untouched.",
        )

    def test_pronunciation_inventory_indexing(self) -> None:
        """Verify scanning book script chapters for pronunciation candidates."""
        with tempfile.TemporaryDirectory() as directory:
            project_dir = Path(directory)
            book_script = {
                "metadata": {"title": "Test Book", "author": "Author"},
                "character_registry": {"characters": {}},
                "chapters": [
                    {
                        "chapter_number": 1,
                        "chapter_title": "One",
                        "lines": [
                            {"line_id": "ch01_0001", "speaker": "narrator", "text": "The homeisler sailed away."},
                            {"line_id": "ch01_0002", "speaker": "dusk", "text": "Farewell, homeisle."},
                        ]
                    }
                ]
            }
            (project_dir / "book_script.json").write_text(json.dumps(book_script), encoding="utf-8")
            (project_dir / "pronunciation_dict.json").write_text(
                json.dumps({"homeisler": "home-aisler"}), encoding="utf-8"
            )

            inv = build_pronunciation_inventory(project_dir)
            terms = {item["term"]: item for item in inv["candidates"]}

            self.assertIn("homeisler", terms)
            self.assertEqual(terms["homeisler"]["status"], "verified")
            self.assertEqual(terms["homeisler"]["spoken_text"], "home-aisler")

    async def test_preview_endpoint_with_tts_generation(self) -> None:
        """Verify preview endpoint synthesizes or returns preview audio URL."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_dir = root / "project"
            workspace_dir = root / "workspace"
            project_dir.mkdir(parents=True)
            workspace_dir.mkdir(parents=True)

            cast = {
                "voices": {
                    "narrator_male": {"name": "Narrator Male"}
                }
            }
            (project_dir / "voice_cast.json").write_text(json.dumps(cast), encoding="utf-8")

            class FakeVoiceClient:
                def health_check_once(self, timeout_seconds: float = 0.8) -> MagicMock:
                    return MagicMock(status="ok")

                def generate_line(self, req: GenerateLineRequest) -> GenerateLineResponse:
                    # Write dummy segment wav
                    out_path = workspace_dir / "segments" / f"{req.line.line_id}.wav"
                    _make_dummy_wav(out_path, duration_s=0.3)
                    return GenerateLineResponse(
                        status="success",
                        line_id=req.line.line_id,
                        audio_file=str(out_path),
                        duration_seconds=0.3,
                    )

            fake_pipeline = MagicMock()
            fake_pipeline.voice_client = FakeVoiceClient()

            with (
                patch.object(dashboard, "job_queue", MagicMock()),
                patch.object(dashboard, "_require_job", return_value={"project_id": "test"}),
                patch.object(dashboard, "_project_dir", return_value=project_dir),
                patch.object(dashboard, "_workspace_project_dir", return_value=workspace_dir),
                patch.object(dashboard, "pipeline", fake_pipeline),
            ):
                req = dashboard.PronunciationPreviewRequest(
                    term="homeisle",
                    spoken_text="home-aisle",
                )
                res = await dashboard.preview_pronunciation("test", req)

                self.assertEqual(res["status"], "success")
                self.assertTrue(res["has_tts"])
                self.assertIn("api/projects/test/pronunciations/preview/", res["audio_url"])

                # Now test fetching the preview audio
                preview_id = res["audio_url"].split("/preview/")[1].split("/audio")[0]
                audio_res = await dashboard.get_pronunciation_preview_audio("test", preview_id)
                self.assertEqual(audio_res.media_type, "audio/wav")

    async def test_preview_endpoint_offline_fallback(self) -> None:
        """Verify preview endpoint returns fallback status when TTS server is offline."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_dir = root / "project"
            workspace_dir = root / "workspace"
            project_dir.mkdir(parents=True)
            workspace_dir.mkdir(parents=True)

            with (
                patch.object(dashboard, "job_queue", MagicMock()),
                patch.object(dashboard, "_require_job", return_value={"project_id": "test"}),
                patch.object(dashboard, "_project_dir", return_value=project_dir),
                patch.object(dashboard, "_workspace_project_dir", return_value=workspace_dir),
                patch.object(dashboard, "pipeline", None),
            ):
                req = dashboard.PronunciationPreviewRequest(
                    term="homeisle",
                    spoken_text="home-aisle",
                )
                res = await dashboard.preview_pronunciation("test", req)

                self.assertEqual(res["status"], "fallback_webspeech")
                self.assertFalse(res["has_tts"])
                self.assertEqual(res["spoken_text"], "home-aisle")

    def test_line_caching_and_hot_swap_regeneration(self) -> None:
        """Verify changing pronunciation text invalidates only affected segment hashes, allowing instant hot swap."""
        from voice.tts_server.embedding_store import EmbeddingStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "embeddings.sqlite3"
            segments_dir = root / "segments"
            segments_dir.mkdir()
            store = EmbeddingStore(db_path=db_path)

            line1_orig = "The homeisle was peaceful."
            line2_orig = "The sun rose above the horizon."
            line1_wav = segments_dir / "ch01_0001.wav"
            line2_wav = segments_dir / "ch01_0002.wav"

            _make_dummy_wav(line1_wav, 0.4)
            _make_dummy_wav(line2_wav, 0.5)

            # Record initial generation context in sqlite store
            ctx_line1_v1 = {"synthesis_text": line1_orig, "model": "qwen3"}
            ctx_line2 = {"synthesis_text": line2_orig, "model": "qwen3"}

            store.save_synthesis_fingerprint(
                project_id="test",
                line_id="ch01_0001",
                text=line1_orig,
                speaker="narrator",
                emotion="",
                speed=1.0,
                fx_dict=None,
                output_path=line1_wav,
                duration_seconds=0.4,
                generation_context=ctx_line1_v1,
            )
            store.save_synthesis_fingerprint(
                project_id="test",
                line_id="ch01_0002",
                text=line2_orig,
                speaker="narrator",
                emotion="",
                speed=1.0,
                fx_dict=None,
                output_path=line2_wav,
                duration_seconds=0.5,
                generation_context=ctx_line2,
            )

            # 1. Check with unchanged text: both should be CACHE HITS (needs_synthesis=False)
            needs_gen_1 = store.line_needs_synthesis(
                project_id="test",
                line_id="ch01_0001",
                text=line1_orig,
                speaker="narrator",
                output_path=line1_wav,
                generation_context=ctx_line1_v1,
            )
            needs_gen_2 = store.line_needs_synthesis(
                project_id="test",
                line_id="ch01_0002",
                text=line2_orig,
                speaker="narrator",
                output_path=line2_wav,
                generation_context=ctx_line2,
            )
            self.assertFalse(needs_gen_1, "Line 1 should be a cache hit initially")
            self.assertFalse(needs_gen_2, "Line 2 should be a cache hit initially")

            # 2. Update pronunciation for homeisle -> home-aisle
            line1_spoken = apply_pronunciations(line1_orig, {"homeisle": "home-aisle"})
            self.assertEqual(line1_spoken, "The home-aisle was peaceful.")
            ctx_line1_v2 = {"synthesis_text": line1_spoken, "model": "qwen3"}

            # Line 1 context has changed: MUST require regeneration
            needs_gen_1_updated = store.line_needs_synthesis(
                project_id="test",
                line_id="ch01_0001",
                text=line1_orig,
                speaker="narrator",
                output_path=line1_wav,
                generation_context=ctx_line1_v2,
            )
            # Line 2 context has NOT changed: MUST remain a cache hit
            needs_gen_2_unchanged = store.line_needs_synthesis(
                project_id="test",
                line_id="ch01_0002",
                text=line2_orig,
                speaker="narrator",
                output_path=line2_wav,
                generation_context=ctx_line2,
            )

            self.assertTrue(needs_gen_1_updated, "Line 1 with updated pronunciation MUST trigger regeneration")
            self.assertFalse(needs_gen_2_unchanged, "Line 2 untouched line MUST remain a cache hit (0 compute)")

            # 3. Hot-swap the regenerated segment and re-assemble chapter audio
            _make_dummy_wav(line1_wav, 0.45) # simulated newly generated WAV
            store.save_synthesis_fingerprint(
                project_id="test",
                line_id="ch01_0001",
                text=line1_orig,
                speaker="narrator",
                emotion="",
                speed=1.0,
                fx_dict=None,
                output_path=line1_wav,
                duration_seconds=0.45,
                generation_context=ctx_line1_v2,
            )

            # Master assembly test
            from shared.models import MasterSegmentInfo
            assembler = AudioAssembler(sample_rate=24000)
            lines_data = [
                MasterSegmentInfo(line_id="ch01_0001", file=str(line1_wav)),
                MasterSegmentInfo(line_id="ch01_0002", file=str(line2_wav)),
            ]
            result = assembler.assemble_chapter(lines_data, root)
            self.assertIn("audio", result)
            self.assertGreater(len(result["audio"]), 0, "Assembled audio must not be empty")


if __name__ == "__main__":
    unittest.main()
