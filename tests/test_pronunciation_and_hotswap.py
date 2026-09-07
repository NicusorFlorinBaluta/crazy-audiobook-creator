"""Unit and integration tests for pronunciation replacements, previewing, and audio hot-swapping."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from brain.dashboard.api import runtime as dashboard_runtime
from brain.dashboard.api.routers import pronunciations as pronunciation_routes
from shared.models import (
    GenerateLineRequest,
    GenerateLineResponse,
)
from shared.pronunciation import (
    apply_pronunciations,
    build_pronunciation_inventory,
)
from voice.mastering.assembler import AudioAssembler


def _make_dummy_wav(path: Path, duration_s: float = 0.5, sample_rate: int = 24000) -> None:
    """Create a minimal valid PCM WAV file for audio tests."""
    import struct
    import wave

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
        """Verify word-boundary aware case-insensitive pronunciation replacement with fluid syllable spacing."""
        p_dict = {
            "homeisle": "home-aisle",
            "homeisler": "home-aisler",
            "homeisles": "home-aisles",
            "homeislers": "home-aislers",
            "kokerlii": "Koh-ker-lee",
        }

        # Exact match (hyphens preserved, not forced to spaces)
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

    def test_generate_phonetic_recommendations(self) -> None:
        """Verify generation of 1 default (single word) and 1 alternate recommendation."""
        from shared.pronunciation import generate_phonetic_recommendations

        homeisle_rec = generate_phonetic_recommendations("Homeisle")
        self.assertEqual(homeisle_rec["default"], "Homeaisle")
        self.assertEqual(homeisle_rec["alternate"], "Home-aisle")

        kokerlii_rec = generate_phonetic_recommendations("Kokerlii")
        self.assertEqual(kokerlii_rec["default"], "Cokerlee")
        self.assertEqual(kokerlii_rec["alternate"], "Koh-ker-lee")

        pache_rec = generate_phonetic_recommendations("Pache")
        self.assertEqual(pache_rec["default"], "Pahchee")
        self.assertEqual(pache_rec["alternate"], "Paych")

    def test_english_dictionary_filtering(self) -> None:
        """Verify standard English words are excluded from unresolved candidate suggestions."""
        from shared.pronunciation import extract_concise_sentence, is_english_word

        self.assertTrue(is_english_word("tower"))
        self.assertTrue(is_english_word("less"))
        self.assertTrue(is_english_word("other"))
        self.assertTrue(is_english_word("listen"))
        self.assertTrue(is_english_word("read"))
        self.assertTrue(is_english_word("time"))
        self.assertTrue(is_english_word("princess"))

        self.assertFalse(is_english_word("drizzt"))
        self.assertFalse(is_english_word("jarlaxle"))
        self.assertFalse(is_english_word("homeisle"))
        self.assertFalse(is_english_word("shimmergloom"))

        # Test concise sentence extraction
        long_text = (
            "The ancient high tower loomed against the dark stormy sky, casting a long shadow "
            "over the courtyard where the guards assembled their gear for the dawn patrol."
        )
        concise = extract_concise_sentence(long_text, "tower", max_chars=80)
        self.assertLessEqual(len(concise), 85)
        self.assertIn("tower", concise.lower())

    def test_pronunciation_inventory_indexing(self) -> None:
        """Verify scanning book script chapters for pronunciation candidates with recommendations."""
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
                            {"line_id": "ch01_0002", "speaker": "dusk", "text": "Farewell, Homeisle."},
                            {"line_id": "ch01_0003", "speaker": "narrator", "text": "And Homeisle faded from sight."},
                            # Dictionary words should be filtered out
                            {"line_id": "ch01_0004", "speaker": "narrator", "text": "The Tower was tall. The Tower loomed."},
                            {"line_id": "ch01_0005", "speaker": "narrator", "text": "The Less they knew, the Less they cared."},
                        ],
                    }
                ],
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

            # Check recommendation on unresolved term
            self.assertIn("Homeisle", terms)
            self.assertEqual(terms["Homeisle"]["status"], "review_required")
            self.assertEqual(terms["Homeisle"]["recommendation_default"], "Homeaisle")
            self.assertTrue(bool(terms["Homeisle"]["recommendation_alternate"]))

            # Confirm English dictionary words like Tower and Less were filtered out
            self.assertNotIn("Tower", terms)
            self.assertNotIn("tower", terms)
            self.assertNotIn("Less", terms)
            self.assertNotIn("less", terms)

    async def test_preview_endpoint_with_tts_generation(self) -> None:
        """Verify preview endpoint synthesizes native TTS audio with carrier sentence and isolated modes."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_dir = root / "project"
            workspace_dir = root / "workspace"
            project_dir.mkdir(parents=True)
            workspace_dir.mkdir(parents=True)

            cast = {"voices": {"narrator_male": {"name": "Narrator Male"}}}
            (project_dir / "voice_cast.json").write_text(json.dumps(cast), encoding="utf-8")

            class FakeVoiceClient:
                def health_check_once(self, timeout_seconds: float = 0.8) -> MagicMock:
                    return MagicMock(status="ok")

                def generate_line(
                    self, req: GenerateLineRequest, timeout: int | None = None
                ) -> GenerateLineResponse:
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
                patch.object(dashboard_runtime, "job_queue", MagicMock()),
                patch.object(dashboard_runtime, "require_job", return_value={"project_id": "test"}),
                patch.object(dashboard_runtime, "project_dir", return_value=project_dir),
                patch.object(dashboard_runtime, "workspace_project_dir", return_value=workspace_dir),
                patch.object(dashboard_runtime, "pipeline", fake_pipeline),
            ):
                req = pronunciation_routes.PronunciationPreviewRequest(
                    term="homeisle",
                    spoken_text="home-aisle",
                    in_sentence=True,
                )
                res = await pronunciation_routes.preview_pronunciation("test", req)

                self.assertEqual(res["status"], "success")
                self.assertTrue(res["has_tts"])
                self.assertEqual(res["spoken_text"], "home-aisle")
                self.assertEqual(res["text_spoken"], "The word is home-aisle.")
                self.assertIn("api/projects/test/pronunciations/preview/", res["audio_url"])

                # Now test fetching the preview audio
                preview_id = res["audio_url"].split("/preview/")[1].split("/audio")[0]
                audio_res = await pronunciation_routes.get_pronunciation_preview_audio("test", preview_id)
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
                patch.object(dashboard_runtime, "job_queue", MagicMock()),
                patch.object(dashboard_runtime, "require_job", return_value={"project_id": "test"}),
                patch.object(dashboard_runtime, "project_dir", return_value=project_dir),
                patch.object(dashboard_runtime, "workspace_project_dir", return_value=workspace_dir),
                patch.object(dashboard_runtime, "pipeline", None),
            ):
                req = pronunciation_routes.PronunciationPreviewRequest(
                    term="homeisle",
                    spoken_text="home-aisle",
                )
                res = await pronunciation_routes.preview_pronunciation("test", req)

                self.assertEqual(res["status"], "fallback_webspeech")
                self.assertFalse(res["has_tts"])
                self.assertEqual(res["spoken_text"], "home-aisle")

    async def test_batch_pronunciation_update(self) -> None:
        """Verify batch approval of multiple pronunciation recommendations in a single atomic request."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_dir = root / "project"
            workspace_dir = root / "workspace"
            project_dir.mkdir(parents=True)
            workspace_dir.mkdir(parents=True)

            book_script = {
                "metadata": {"title": "Test Book"},
                "character_registry": {"characters": {}},
                "chapters": [
                    {
                        "chapter_number": 1,
                        "lines": [
                            {"line_id": "ch01_0001", "speaker": "narrator", "text": "Kokerlii and homeisle."},
                        ],
                    }
                ],
            }
            (project_dir / "book_script.json").write_text(json.dumps(book_script), encoding="utf-8")

            with (
                patch.object(dashboard_runtime, "job_queue", MagicMock()),
                patch.object(dashboard_runtime, "require_job", return_value={"project_id": "test"}),
                patch.object(dashboard_runtime, "project_dir", return_value=project_dir),
                patch.object(dashboard_runtime, "workspace_project_dir", return_value=workspace_dir),
            ):
                req = pronunciation_routes.PronunciationBatchRequest(
                    entries={
                        "homeisle": "Home aisle",
                        "kokerlii": "Coker lee",
                    }
                )
                res = await pronunciation_routes.batch_update_pronunciations("test", req)
                self.assertEqual(res["status"], "success")

                dict_data = json.loads((project_dir / "pronunciation_dict.json").read_text(encoding="utf-8"))
                self.assertEqual(dict_data.get("homeisle"), "Home aisle")
                self.assertEqual(dict_data.get("kokerlii"), "Coker lee")

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

            # 2. Update pronunciation for homeisle -> home-aisle (hyphen preserved for fluid TTS)
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
            _make_dummy_wav(line1_wav, 0.45)  # simulated newly generated WAV
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

    async def test_preview_mode_toggle_and_pipeline_pause(self) -> None:
        """Verify preview mode pauses a running pipeline and calls warmup, then resumes on exit."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_dir = root / "project"
            project_dir.mkdir(parents=True)

            job_state = {
                "project_id": "test_proj",
                "running": True,
                "status": "generation",
                "active_stage": "generation",
            }
            fake_job_queue = MagicMock()
            fake_job_queue.get_job.return_value = job_state

            fake_voice_client = MagicMock()
            from shared.models import VoiceWarmupResponse

            fake_voice_client.health_check_once.return_value = MagicMock(status="ok")
            fake_voice_client.warmup_voice.return_value = VoiceWarmupResponse(
                status="ready",
                model_loaded="Qwen3-TTS",
                device="cuda",
                voice_id="narrator_female",
                prompt_primed=True,
            )

            fake_pipeline = MagicMock()
            fake_pipeline.voice_client = fake_voice_client
            start_pipeline_called = []

            async def fake_starter(pid, **kwargs):
                start_pipeline_called.append(pid)

            with (
                patch.object(dashboard_runtime, "job_queue", fake_job_queue),
                patch.object(dashboard_runtime, "require_job", return_value=job_state),
                patch.object(dashboard_runtime, "project_dir", return_value=project_dir),
                patch.object(dashboard_runtime, "pipeline", fake_pipeline),
                patch.object(dashboard_runtime, "_pipeline_starter", fake_starter),
            ):
                # 1. Enter preview mode
                enter_req = pronunciation_routes.PreviewModeRequest(enabled=True)
                res_enter = await pronunciation_routes.toggle_preview_mode("test_proj", enter_req)

                self.assertEqual(res_enter["status"], "success")
                self.assertTrue(res_enter["preview_mode"])
                self.assertTrue(res_enter["paused_pipeline"])
                fake_pipeline.stop.assert_called_once_with("test_proj")
                fake_voice_client.warmup_voice.assert_called_once()

                # Verify GET preview-mode
                status_res = await pronunciation_routes.get_preview_mode_status("test_proj")
                self.assertTrue(status_res["preview_mode"])
                self.assertTrue(status_res["paused_pipeline"])

                # 2. Exit preview mode with auto-resume
                exit_req = pronunciation_routes.PreviewModeRequest(enabled=False, resume_pipeline=True)
                res_exit = await pronunciation_routes.toggle_preview_mode("test_proj", exit_req)

                self.assertEqual(res_exit["status"], "success")
                self.assertFalse(res_exit["preview_mode"])
                self.assertTrue(res_exit["pipeline_resumed"])
                self.assertIn("test_proj", start_pipeline_called)

    async def test_pronunciation_inventory_mutation_force_refresh(self) -> None:
        """Verify update_pronunciation writes pronunciation_dict.json and forces inventory refresh."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_dir = root / "project"
            project_dir.mkdir(parents=True)
            (project_dir / "script").mkdir(parents=True)
            (project_dir / "book_script.json").write_text(
                json.dumps({"chapters": [{"chapter_number": 1, "lines": [{"text": "Hello world."}]}]}),
                encoding="utf-8",
            )

            # Pre-seed inventory file with an old term
            old_inv = {
                "version": 1,
                "project_id": "test_proj",
                "candidates": [
                    {
                        "term": "existing",
                        "status": "verified",
                        "spoken_text": "ex-ist-ing",
                        "phonetic_system": "syllable_spacing",
                        "recommendations": [],
                        "recommendation_default": None,
                        "context_sentences": [],
                        "occurrences": 1,
                    }
                ],
                "summary": {"total_candidates": 1, "unresolved": 0, "verified": 1},
            }
            (project_dir / "pronunciation_inventory.json").write_text(json.dumps(old_inv), encoding="utf-8")

            with (
                patch.object(dashboard_runtime, "job_queue", MagicMock()),
                patch.object(dashboard_runtime, "require_job", return_value={"project_id": "test_proj"}),
                patch.object(dashboard_runtime, "project_dir", return_value=project_dir),
                patch.object(dashboard_runtime, "pronunciation_llm", return_value=None),
            ):
                req = pronunciation_routes.PronunciationRequest(term="newterm", spoken_text="new-term")
                res = await pronunciation_routes.update_pronunciation("test_proj", req)

                self.assertEqual(res["status"], "success")
                inv = res["inventory"]
                term_names = [c["term"] for c in inv["candidates"]]
                self.assertIn("newterm", term_names, "Newly added term MUST be in refreshed inventory")
                dict_content = json.loads((project_dir / "pronunciation_dict.json").read_text(encoding="utf-8"))
                self.assertEqual(dict_content.get("newterm"), "new-term")

    def test_voice_library_resolve_voice_reference_narrator_variants(self) -> None:
        """Verify VoiceLibraryManager resolves narrator_female and narrator_male when narrator.wav is absent."""
        with tempfile.TemporaryDirectory() as directory:
            lib_dir = Path(directory)
            from voice.tts_server.voice_library import VoiceLibraryManager

            mgr = VoiceLibraryManager(library_dir=lib_dir)
            proj_dir = lib_dir / "test_proj"
            proj_dir.mkdir(parents=True)

            # Create narrator_female.wav and register it in voices.json
            ref_wav = proj_dir / "narrator_female.wav"
            _make_dummy_wav(ref_wav, duration_s=0.5)

            registry = {
                "project_id": "test_proj",
                "voices": {
                    "narrator_female": {
                        "name": "Narrator Female",
                        "file": str(ref_wav),
                        "ref_text": "This is the narrator reference text.",
                    }
                },
            }
            (proj_dir / "voices.json").write_text(json.dumps(registry), encoding="utf-8")

            # Resolving "narrator" should automatically find narrator_female and its ref_text
            p, vid, text = mgr.resolve_voice_reference("test_proj", "narrator")
            self.assertIsNotNone(p)
            self.assertEqual(vid, "narrator_female")
            self.assertEqual(text, "This is the narrator reference text.")
            self.assertTrue(p.is_file())

    def test_load_pronunciation_dictionary_with_implicit_defaults(self) -> None:
        """Verify load_pronunciation_dictionary layers recommendations under project overrides and honors keep-original."""
        from shared.pronunciation import load_pronunciation_dictionary

        with tempfile.TemporaryDirectory() as directory:
            proj_dir = Path(directory)
            # 1. Base recommendations
            recs = {
                "Bruenor": {"default": "Brue-nor", "alternate": "Brue-nor-alt"},
                "Drizzt": {"default": "Drizt", "alternate": "Driz-zt"},
                "Cattibrie": {"default": "Cat-ti-brie", "alternate": ""},
            }
            (proj_dir / "pronunciation_recommendations.json").write_text(json.dumps(recs), encoding="utf-8")

            # 2. Project overrides: custom for Drizzt, keep-original for Cattibrie (mapped to itself)
            proj_dict = {
                "Drizzt": "Drizzt-Custom",
                "Cattibrie": "Cattibrie",
            }
            (proj_dir / "pronunciation_dict.json").write_text(json.dumps(proj_dict), encoding="utf-8")

            # Test with include_defaults=True (generation behavior)
            active, sources = load_pronunciation_dictionary(proj_dir, include_defaults=True)
            self.assertEqual(active.get("Bruenor"), "Brue-nor")
            self.assertEqual(sources.get("Bruenor"), "default")

            self.assertEqual(active.get("Drizzt"), "Drizzt-Custom")
            self.assertEqual(sources.get("Drizzt"), "project")

            # Cattibrie was mapped to itself, so it must NOT be in active replacements
            self.assertNotIn("Cattibrie", active)
            self.assertEqual(sources.get("Cattibrie"), "project")

            # Test with include_defaults=False (inventory / UI verified behavior)
            verified_only, v_sources = load_pronunciation_dictionary(proj_dir, include_defaults=False)
            self.assertNotIn("Bruenor", verified_only)
            self.assertEqual(verified_only.get("Drizzt"), "Drizzt-Custom")

    async def test_export_pronunciations_with_scopes(self) -> None:
        """Verify export_pronunciations filters correctly by scope and case-insensitively overwrites defaults."""
        with tempfile.TemporaryDirectory() as directory:
            proj_dir = Path(directory)
            # Notice recs has lowercase keys (as stored in real recommendation caches)
            recs = {
                "bruenor": {"default": "Brue-nor"},
                "drizzt": {"default": "Drizt"},
                "jax": {"default": "Yax"},
                "janquay": {"default": "Yanquay"},
            }
            # Project overrides with TitleCase (including keep-original overrides)
            proj = {
                "Drizzt": "Drizzt-Custom",
                "Jarlaxle": "Jarlaxel",
                "Jax": "Jax",
                "Janquay": "Janquay",
            }
            (proj_dir / "pronunciation_recommendations.json").write_text(json.dumps(recs), encoding="utf-8")
            (proj_dir / "pronunciation_dict.json").write_text(json.dumps(proj), encoding="utf-8")

            with patch.object(dashboard_runtime, "require_job"), \
                 patch.object(dashboard_runtime, "project_dir", return_value=proj_dir):

                # 1. Scope: verified / custom only
                res_ver = await pronunciation_routes.export_pronunciations("test_proj", scope="verified")
                data_ver = json.loads(res_ver.body.decode("utf-8"))
                self.assertEqual(data_ver["scope"], "verified")
                self.assertEqual(data_ver["lexicon"], proj)
                self.assertNotIn("bruenor", data_ver["lexicon"])

                # 2. Scope: defaults only
                res_def = await pronunciation_routes.export_pronunciations("test_proj", scope="defaults")
                data_def = json.loads(res_def.body.decode("utf-8"))
                self.assertEqual(data_def["scope"], "defaults")
                self.assertIn("jax", data_def["lexicon"])
                self.assertEqual(data_def["lexicon"]["jax"], "Yax")
                self.assertNotIn("Jarlaxle", data_def["lexicon"])

                # 3. Scope: all (combined, verified overrides defaults case-insensitively!)
                res_all = await pronunciation_routes.export_pronunciations("test_proj", scope="all")
                data_all = json.loads(res_all.body.decode("utf-8"))
                self.assertEqual(data_all["scope"], "all")
                lexicon = data_all["lexicon"]

                # Case-insensitive overrides must win:
                self.assertEqual(lexicon.get("Jax"), "Jax")
                self.assertNotIn("jax", lexicon)
                self.assertEqual(lexicon.get("Janquay"), "Janquay")
                self.assertNotIn("janquay", lexicon)
                self.assertEqual(lexicon.get("Drizzt"), "Drizzt-Custom")
                self.assertNotIn("drizzt", lexicon)
                self.assertEqual(lexicon.get("Jarlaxle"), "Jarlaxel")
                self.assertEqual(lexicon.get("bruenor"), "Brue-nor")
                # Exactly 5 items (bruenor, Drizzt, Jarlaxle, Jax, Janquay), no duplicates!
                self.assertEqual(len(lexicon), 5)

    async def test_batch_update_pronunciations_case_insensitivity(self) -> None:
        """Verify batch_update_pronunciations removes conflicting case variations."""
        with tempfile.TemporaryDirectory() as directory:
            proj_dir = Path(directory)
            dict_path = proj_dir / "pronunciation_dict.json"
            dict_path.write_text(json.dumps({"Jax": "OldSpoken", "other": "val"}), encoding="utf-8")

            with patch.object(dashboard_runtime, "require_job"), \
                 patch.object(dashboard_runtime, "project_dir", return_value=proj_dir):
                req = pronunciation_routes.PronunciationBatchRequest(entries={"jax": "NewSpoken"})
                res = await pronunciation_routes.batch_update_pronunciations("test_proj", req)
                self.assertEqual(res["status"], "success")

                saved = json.loads(dict_path.read_text(encoding="utf-8"))
                # "Jax" should be replaced by "jax", no duplicate keys
                self.assertNotIn("Jax", saved)
                self.assertEqual(saved.get("jax"), "NewSpoken")
                self.assertEqual(saved.get("other"), "val")

    def test_auto_exit_preview_mode_on_pipeline_start(self) -> None:
        """Verify exit_preview_mode clears preview mode state."""
        pid = "test_preview_exit_proj"
        pronunciation_routes._active_preview_modes[pid] = {"active": True, "paused_by_us": True}
        self.assertTrue(dashboard_runtime.exit_preview_mode(pid))
        self.assertNotIn(pid, pronunciation_routes._active_preview_modes)
        # Second call returns False since already exited
        self.assertFalse(dashboard_runtime.exit_preview_mode(pid))


if __name__ == "__main__":
    unittest.main()

