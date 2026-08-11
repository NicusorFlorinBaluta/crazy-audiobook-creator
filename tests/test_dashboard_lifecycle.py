from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import brain.dashboard.api.main as dashboard
from brain.extractor.metadata_fetcher import FetchedMetadata, MetadataFetcher
from shared.constants import PipelineStage


class DashboardLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        dashboard._dashboard_shutdown_task = None

    async def asyncTearDown(self) -> None:
        task = dashboard._dashboard_shutdown_task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        dashboard._dashboard_shutdown_task = None

    async def test_authenticated_shutdown_schedules_cleanup(self) -> None:
        blocker = asyncio.Event()

        async def controlled_shutdown() -> None:
            await blocker.wait()

        with patch.object(
            dashboard,
            "_shutdown_dashboard_process",
            side_effect=controlled_shutdown,
        ):
            response = await dashboard.shutdown_dashboard()
            self.assertEqual(response["status"], "shutting_down")
            self.assertIsNotNone(dashboard._dashboard_shutdown_task)
            blocker.set()
            await dashboard._dashboard_shutdown_task

    async def test_metadata_preview_is_cached_and_apply_preserves_epub_identity(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + (320).to_bytes(4, "big") + (480).to_bytes(4, "big")
        fetched = FetchedMetadata(
            status="matched",
            query_title="Original Title",
            query_author="Original Author",
            title="Provider Title",
            authors=["Provider Author"],
            description="Provider description",
            isbn="9780000000001",
            genre="Mystery",
            year="2024",
            provider_id="volume-1",
            confidence=0.98,
            cover_image_bytes=png,
            cover_mime_type="image/png",
            cover_width=320,
            cover_height=480,
        )
        with tempfile.TemporaryDirectory() as directory:
            project_dir = Path(directory)
            embedded_cover = project_dir / "embedded.jpg"
            embedded_cover.write_bytes(b"embedded")
            book_path = project_dir / "book.json"
            book_path.write_text(json.dumps({
                "metadata": {
                    "title": "Original Title",
                    "author": "Original Author",
                    "language": "en",
                    "description": "Embedded EPUB description",
                    "cover_image_path": str(embedded_cover),
                },
                "chapters": [],
            }), encoding="utf-8")

            with (
                patch.object(dashboard, "_project_dir", return_value=project_dir),
                patch.object(MetadataFetcher, "fetch", return_value=fetched) as fetch,
            ):
                preview = dashboard._fetch_metadata_sync("sample")
                automatic = dashboard._fetch_metadata_sync(
                    "sample", apply=True, only_missing=True
                )
                automatically_saved = json.loads(
                    book_path.read_text(encoding="utf-8")
                )["metadata"]
                applied = dashboard._fetch_metadata_sync("sample", apply=True)

            self.assertFalse(preview["cached"])
            self.assertTrue(automatic["applied"])
            self.assertTrue(applied["applied"])
            fetch.assert_called_once()
            saved = json.loads(book_path.read_text(encoding="utf-8"))["metadata"]
            self.assertEqual(saved["title"], "Original Title")
            self.assertEqual(saved["author"], "Original Author")
            self.assertEqual(saved["cover_image_path"], str(embedded_cover))
            self.assertEqual(
                automatically_saved["description"], "Embedded EPUB description"
            )
            self.assertEqual(saved["description"], "Provider description")
            self.assertEqual(saved["genre"], "Mystery")
            self.assertEqual(saved["metadata_provider_id"], "volume-1")

    async def test_schedule_api_rejects_empty_days_and_normalizes_order(self) -> None:
        invalid_request = AsyncMock()
        invalid_request.json.return_value = {
            "enabled": True,
            "timezone": "Europe/Bucharest",
            "windows": [{"days": [], "start": "08:00", "end": "18:00"}],
        }
        with self.assertRaises(dashboard.HTTPException) as raised:
            await dashboard.update_schedule(invalid_request)
        self.assertEqual(raised.exception.status_code, 422)

        request = AsyncMock()
        request.json.return_value = {
            "enabled": True,
            "timezone": "Europe/Bucharest",
            "windows": [{
                "days": ["Sunday", "Monday"],
                "start": "08:00",
                "end": "18:00",
            }],
        }
        with (
            patch.object(dashboard, "_replace_yaml_section") as replace,
            patch.object(dashboard, "pipeline", None),
        ):
            response = await dashboard.update_schedule(request)
        self.assertEqual(
            response["schedule"]["windows"][0]["days"],
            ["Monday", "Sunday"],
        )
        replace.assert_called_once()

    async def test_shutdown_helper_releases_resources_before_exit(self) -> None:
        released = AsyncMock()
        with (
            patch.object(dashboard, "_release_gpu_resources", released),
            patch.object(dashboard.asyncio, "sleep", AsyncMock()),
            patch.object(dashboard.os, "_exit") as process_exit,
        ):
            await dashboard._shutdown_dashboard_process()

        released.assert_awaited_once()
        process_exit.assert_called_once_with(0)

    async def test_immediate_interrupt_waits_for_worker_and_marks_paused(self) -> None:
        stopped = asyncio.Event()

        class FakeQueue:
            def __init__(self):
                self.state = {
                    "status": PipelineStage.SCRIPTING.value,
                    "active_stage": PipelineStage.SCRIPTING.value,
                    "running": True,
                }

            def get_job(self, project_id):
                return dict(self.state)

            def update_job(self, project_id, updates):
                self.state.update(updates)

        class FakePipeline:
            def stop(self, project_id):
                stopped.set()

            def _stop_ollama_server(self):
                return None

            def _stop_voice_server(self):
                return None

        async def worker():
            await stopped.wait()

        task = asyncio.create_task(worker())
        queue = FakeQueue()
        with (
            patch.object(dashboard, "pipeline", FakePipeline()),
            patch.object(dashboard, "job_queue", queue),
            patch.object(dashboard, "running_tasks", {"old": task}),
        ):
            await dashboard._interrupt_pipeline_worker(
                "old",
                reason="replacement started",
                wait_seconds=1,
            )

        self.assertTrue(task.done())
        self.assertEqual(queue.state["status"], PipelineStage.PAUSED.value)
        self.assertFalse(queue.state["running"])

    async def test_voice_review_approval_persists_status_and_boolean(self) -> None:
        update = dashboard._voice_review_approval_update(
            "2026-08-03T00:00:00+00:00",
            "cast-revision",
        )

        self.assertEqual(update["voice_review_status"], "approved")
        self.assertTrue(update["voice_review_approved"])
        self.assertEqual(
            update["voice_review_approved_revision"],
            "cast-revision",
        )
        self.assertIsNone(update["pause_reason"])

    async def test_validation_reset_targets_only_current_selection(self) -> None:
        state = {
            "scripted_chapters": [1, 2, 3, 4],
            "generation_chapter_selection": [4, 2, 4],
        }
        self.assertEqual(dashboard._validation_reset_targets(state), [2, 4])
        state["generation_chapter_selection"] = None
        self.assertEqual(
            dashboard._validation_reset_targets(state),
            [1, 2, 3, 4],
        )

    async def test_quality_report_keeps_accepted_warnings_out_of_failures(self) -> None:
        class FakeQueue:
            @staticmethod
            def get_quality_report(project_id):
                return [
                    {
                        "line_id": "pass",
                        "chapter_number": 1,
                        "attempt": 1,
                        "status": "pass",
                        "wer": 0.0,
                        "quality_score": 1.0,
                        "details": {},
                    },
                    {
                        "line_id": "warning",
                        "chapter_number": 1,
                        "attempt": 1,
                        "status": "accepted_with_warning",
                        "wer": 0.0,
                        "quality_score": 0.9,
                        "details": {},
                    },
                    {
                        "line_id": "fail",
                        "chapter_number": 1,
                        "attempt": 1,
                        "status": "fail",
                        "wer": 1.0,
                        "quality_score": 0.4,
                        "details": {},
                    },
                ]

        with patch.object(dashboard, "job_queue", FakeQueue()):
            report = await dashboard.get_quality_report("book")

        self.assertEqual(report["passed_segments"], 1)
        self.assertEqual(report["accepted_with_warning_segments"], 1)
        self.assertEqual(report["failed_segments"], 1)

    async def test_voice_download_uses_book_and_character_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            voice_dir = Path(directory)
            sample = voice_dir / "narrator-abcd.wav"
            sample.write_bytes(b"RIFF" + b"0" * 1200)
            (voice_dir / "voices.json").write_text(
                json.dumps({"voices": {"narrator": {
                    "file": str(sample), "name": "The Narrator",
                }}}),
                encoding="utf-8",
            )
            with (
                patch.object(dashboard, "_voice_project_dir", return_value=voice_dir),
                patch.object(
                    dashboard, "_require_job", return_value={"title": "A Book: One?"}
                ),
                patch.object(
                    dashboard,
                    "_load_or_build_voice_cast",
                    return_value={"voices": {"narrator": {"name": "The Narrator"}}},
                ),
            ):
                response = await dashboard.download_project_voice("book", "narrator")

            disposition = response.headers["content-disposition"]
            self.assertIn("attachment", disposition)
            self.assertIn("A%20Book_%20One%20-", disposition)
            self.assertIn("The%20Narrator", disposition)
            self.assertEqual(Path(response.path), sample)

    async def test_voice_download_rejects_registry_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            voice_dir = root / "voice"
            voice_dir.mkdir()
            outside = root / "outside.wav"
            outside.write_bytes(b"RIFF" + b"0" * 1200)
            (voice_dir / "voices.json").write_text(
                json.dumps({"voices": {"character": {"file": str(outside)}}}),
                encoding="utf-8",
            )
            with patch.object(dashboard, "_voice_project_dir", return_value=voice_dir):
                with self.assertRaisesRegex(Exception, "Invalid voice registry path"):
                    dashboard._registered_voice_path("book", "character")

    async def test_narrator_voice_regeneration_reaches_voice_client(self) -> None:
        class FakeVoiceClient:
            def __init__(self) -> None:
                self.request = None

            def bootstrap_voices(self, request):
                self.request = request
                return type(
                    "BootstrapResponse",
                    (),
                    {"voices_generated": {"narrator_male": {"status": "ok"}}},
                )()

        class FakePipeline:
            def __init__(self) -> None:
                self._voice_server_proc = None
                self.voice_client = FakeVoiceClient()

            def _start_voice_server(self) -> None:
                self._voice_server_proc = object()

            def _stop_voice_server(self) -> None:
                self._voice_server_proc = None

        registry = {
            "book_title": "Dialogue Only",
            "book_author": "Test",
            "characters": {
                "narrator": {
                    "id": "narrator",
                    "name": "Narrator",
                    "gender": "male",
                    "age_range": "40s",
                    "voice_description": "A clear and steady narrator voice",
                }
            },
        }
        cast = {
            "schema": "1",
            "voices": {
                "narrator_male": {
                    "voice_id": "narrator_male",
                    "owner_character_id": "narrator",
                    "name": "Narrator — Male",
                    "gender": "male",
                    "assigned_characters": ["narrator"],
                }
            },
        }
        fake_pipeline = FakePipeline()
        with tempfile.TemporaryDirectory() as directory:
            characters_path = Path(directory) / "characters.json"
            with (
                patch.object(dashboard, "pipeline", fake_pipeline),
                patch.object(dashboard, "_ensure_voice_editable"),
                patch.object(
                    dashboard,
                    "_load_character_registry",
                    return_value=(characters_path, registry),
                ),
                patch.object(dashboard, "_load_or_build_voice_cast", return_value=cast),
                patch.object(dashboard, "_save_voice_cast"),
                patch.object(dashboard, "_chapters_for_speakers", return_value=[]),
                patch.object(dashboard, "_mark_voice_chapters_stale"),
            ):
                result = await dashboard.regenerate_project_voice(
                    "book",
                    "narrator_male",
                    dashboard.VoiceRegenerationRequest(
                        voice_description="A warm, composed audiobook narrator voice"
                    ),
                )

        self.assertEqual(result["status"], "success")
        self.assertIn("narrator_male", fake_pipeline.voice_client.request.characters)


if __name__ == "__main__":
    unittest.main()
