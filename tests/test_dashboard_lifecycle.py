from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
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

    async def test_restart_triggers_independent_fixed_task(self) -> None:
        with patch.object(
            dashboard,
            "_launch_dashboard_restart_helper",
            return_value="Crazy Audiobook Dashboard Restart",
        ):
            response = await dashboard.restart_dashboard_server()

        self.assertEqual(response["status"], "restarting")
        self.assertEqual(
            response["restart_task"],
            "Crazy Audiobook Dashboard Restart",
        )
        self.assertIsNone(dashboard._dashboard_shutdown_task)

    async def test_restart_does_not_shutdown_when_helper_fails(self) -> None:
        with patch.object(
            dashboard,
            "_launch_dashboard_restart_helper",
            side_effect=RuntimeError("task unavailable"),
        ):
            with self.assertRaises(dashboard.HTTPException) as raised:
                await dashboard.restart_dashboard_server()

        self.assertEqual(raised.exception.status_code, 503)
        self.assertIsNone(dashboard._dashboard_shutdown_task)

    async def test_partial_project_delete_retains_job_for_safe_retry(self) -> None:
        class FakeQueue:
            deleted = False

            @staticmethod
            def get_job(project_id):
                return {"project_id": project_id, "running": False}

            def delete_job(self, project_id):
                self.deleted = True

        queue = FakeQueue()
        with tempfile.TemporaryDirectory() as directory:
            roots = [Path(directory) / name for name in ("brain", "workspace", "voice")]
            for root in roots:
                root.mkdir()
            with (
                patch.object(dashboard, "job_queue", queue),
                patch.object(dashboard, "running_tasks", {}),
                patch.object(dashboard, "_project_dir", return_value=roots[0]),
                patch.object(dashboard, "_workspace_project_dir", return_value=roots[1]),
                patch.object(dashboard, "_voice_project_dir", return_value=roots[2]),
                patch.object(
                    dashboard,
                    "_safe_delete_tree",
                    side_effect=[True, False, True],
                ),
                patch.object(dashboard, "_purge_project_cache") as purge,
            ):
                with self.assertRaises(dashboard.HTTPException) as raised:
                    await dashboard.delete_project("book")

        self.assertEqual(raised.exception.status_code, 409)
        self.assertFalse(queue.deleted)
        purge.assert_not_called()

    async def test_metadata_preview_is_cached_and_explicit_apply_sets_reviewed_identity(self) -> None:
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
            book_path.write_text(
                json.dumps(
                    {
                        "metadata": {
                            "title": "Original Title",
                            "author": "Original Author",
                            "language": "en",
                            "description": "Embedded EPUB description",
                            "cover_image_path": str(embedded_cover),
                        },
                        "chapters": [],
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(dashboard, "_project_dir", return_value=project_dir),
                patch.object(MetadataFetcher, "fetch", return_value=fetched) as fetch,
            ):
                preview = dashboard._fetch_metadata_sync("sample")
                automatic = dashboard._fetch_metadata_sync("sample", apply=True, only_missing=True)
                automatically_saved = json.loads(book_path.read_text(encoding="utf-8"))["metadata"]
                applied = dashboard._fetch_metadata_sync("sample", apply=True)

            self.assertFalse(preview["cached"])
            self.assertTrue(automatic["applied"])
            self.assertTrue(applied["applied"])
            fetch.assert_called_once()
            saved = json.loads(book_path.read_text(encoding="utf-8"))["metadata"]
            self.assertEqual(saved["title"], "Provider Title")
            self.assertEqual(saved["author"], "Provider Author")
            self.assertEqual(saved["source_title"], "Original Title")
            self.assertEqual(saved["source_author"], "Original Author")
            self.assertEqual(saved["cover_image_path"], str(embedded_cover))
            self.assertEqual(automatically_saved["description"], "Embedded EPUB description")
            self.assertEqual(saved["description"], "Provider description")
            self.assertEqual(saved["genre"], "Mystery")
            self.assertEqual(saved["metadata_provider_id"], "volume-1")

    async def test_manual_metadata_selection_persists_the_exact_volume(self) -> None:
        selected = FetchedMetadata(
            status="matched",
            query_title="Manual title",
            query_author="Manual author",
            title="Chosen edition",
            authors=["Chosen author"],
            description="Selected by the user.",
            provider_id="chosen-volume",
            confidence=0.61,
        )
        with tempfile.TemporaryDirectory() as directory:
            project_dir = Path(directory)
            book_path = project_dir / "book.json"
            book_path.write_text(
                json.dumps(
                    {
                        "metadata": {
                            "title": "EPUB title",
                            "author": "EPUB author",
                            "language": "en",
                        },
                        "chapters": [],
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(dashboard, "_project_dir", return_value=project_dir),
                patch.object(
                    MetadataFetcher,
                    "fetch_volume",
                    return_value=selected,
                ) as fetch_volume,
            ):
                preview = dashboard._fetch_metadata_sync(
                    "sample",
                    provider_id="chosen-volume",
                    query_title="Manual title",
                    query_author="Manual author",
                )
                applied = dashboard._fetch_metadata_sync("sample", apply=True)

            fetch_volume.assert_called_once_with("chosen-volume", "Manual title", "Manual author")
            self.assertEqual(preview["provider_id"], "chosen-volume")
            self.assertTrue(applied["applied"])
            metadata = json.loads(book_path.read_text(encoding="utf-8"))["metadata"]
            self.assertEqual(metadata["title"], "Chosen edition")
            self.assertEqual(metadata["author"], "Chosen author")
            self.assertEqual(metadata["source_title"], "EPUB title")
            self.assertEqual(metadata["source_author"], "EPUB author")
            self.assertEqual(metadata["description"], "Selected by the user.")
            self.assertEqual(metadata["metadata_provider_id"], "chosen-volume")

    async def test_metadata_refresh_remuxes_existing_export_without_reencoding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_dir = root / "project"
            workspace_dir = root / "workspace"
            project_dir.mkdir()
            (workspace_dir / "output").mkdir(parents=True)
            export = project_dir / "book.m4b"
            export.write_bytes(b"old export")

            def fake_run(command, **kwargs):
                Path(command[-1]).write_bytes(b"refreshed export")
                return type("Result", (), {"returncode": 0, "stderr": ""})()

            with (
                patch.object(dashboard, "_project_dir", return_value=project_dir),
                patch.object(dashboard, "_workspace_project_dir", return_value=workspace_dir),
                patch.object(dashboard.shutil, "which", return_value="ffmpeg"),
                patch.object(dashboard.subprocess, "run", side_effect=fake_run) as run,
            ):
                refreshed = dashboard._refresh_exported_audiobook_metadata(
                    "book",
                    {
                        "title": "Reviewed title",
                        "author": "Reviewed author",
                        "genre": "Fantasy",
                        "year": "2026",
                        "description": "Description",
                        "isbn": "9780000000001",
                    },
                )

            self.assertEqual(refreshed, [str(export.resolve())])
            self.assertEqual(export.read_bytes(), b"refreshed export")
            command = run.call_args.args[0]
            self.assertIn("copy", command)
            self.assertIn("title=Reviewed title", command)
            self.assertIn("artist=Reviewed author", command)
            self.assertIn("isbn=9780000000001", command)
            self.assertIn("grouping=ISBN 9780000000001", command)
            self.assertIn("+faststart", command)
            self.assertIn("-map_chapters", command)

    async def test_audiobook_download_uses_current_book_title(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_dir = Path(directory)
            export = project_dir / "book.m4b"
            export.write_bytes(b"audiobook")
            (project_dir / "book.json").write_text(
                json.dumps({"metadata": {"title": "Reviewed: Book?"}}),
                encoding="utf-8",
            )
            with (
                patch.object(dashboard, "_project_dir", return_value=project_dir),
                patch.object(dashboard, "_workspace_project_dir", return_value=project_dir),
            ):
                response = await dashboard.download_audiobook("book")

            self.assertIn(
                "Reviewed_%20Book.m4b",
                response.headers["content-disposition"],
            )

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
            "windows": [
                {
                    "days": ["Sunday", "Monday"],
                    "start": "08:00",
                    "end": "18:00",
                }
            ],
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

    async def test_empty_chapter_selection_is_saved_but_cannot_be_started(self) -> None:
        class FakeQueue:
            def __init__(self):
                self.state = {
                    "total_chapters": 4,
                    "generation_chapter_selection": None,
                    "incremental_delivery": {"enabled": True},
                }

            def get_job(self, project_id):
                return dict(self.state)

            def update_job(self, project_id, updates):
                self.state.update(updates)

        queue = FakeQueue()
        request = dashboard.ChapterSelectionRequest(chapters=[])
        with patch.object(dashboard, "job_queue", queue):
            response = await dashboard.set_chapter_selection("book", request)

        self.assertEqual(response["selection"], [])
        self.assertEqual(queue.state["generation_chapter_selection"], [])

        with (
            patch.object(dashboard, "pipeline", object()),
            patch.object(dashboard, "job_queue", queue),
            patch.object(dashboard, "running_tasks", {}),
        ):
            with self.assertRaises(dashboard.HTTPException) as raised:
                await dashboard.start_pipeline("book")

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("Select at least one chapter", raised.exception.detail)

    async def test_manual_start_overrides_a_closed_schedule_for_one_run(self) -> None:
        class FakeQueue:
            def __init__(self):
                self.state = {
                    "status": PipelineStage.PAUSED.value,
                    "active_stage": PipelineStage.SCRIPTING.value,
                    "generation_chapter_selection": None,
                    "voice_review_policy": "grandfathered",
                    "bootstrapping_completed": False,
                }
                self.updates = []

            def get_job(self, project_id):
                return dict(self.state)

            def update_job(self, project_id, updates):
                self.updates.append(dict(updates))
                self.state.update(updates)

        class FakePipeline:
            @staticmethod
            def schedule_is_open():
                return False

            @staticmethod
            def run(project_id):
                return None

        queue = FakeQueue()
        tasks = {}
        with (
            patch.object(dashboard, "pipeline", FakePipeline()),
            patch.object(dashboard, "job_queue", queue),
            patch.object(dashboard, "running_tasks", tasks),
            patch.object(
                dashboard,
                "collect_review_gate",
                return_value=SimpleNamespace(blocking_items=[]),
            ),
        ):
            response = await dashboard.start_pipeline(
                "book",
                override_schedule=True,
            )
            self.assertTrue(response["schedule_overridden"])
            self.assertTrue(queue.updates[0]["schedule_override_active"])
            await tasks["book"]

        self.assertFalse(queue.state["schedule_override_active"])
        self.assertFalse(queue.state["running"])

    async def test_manual_start_wakes_an_existing_scheduled_pause_task(self) -> None:
        class FakeQueue:
            def __init__(self):
                self.state = {
                    "status": PipelineStage.PAUSED_SCHEDULED.value,
                    "active_stage": PipelineStage.SCRIPTING.value,
                    "running": True,
                    "pause_reason": "outside configured working hours",
                }

            def get_job(self, project_id):
                return dict(self.state)

            def update_job(self, project_id, updates):
                self.state.update(updates)

        queue = FakeQueue()
        parked_task = asyncio.create_task(asyncio.sleep(60))
        try:
            with (
                patch.object(dashboard, "pipeline", object()),
                patch.object(dashboard, "job_queue", queue),
                patch.object(dashboard, "running_tasks", {"book": parked_task}),
            ):
                response = await dashboard.start_pipeline(
                    "book",
                    override_schedule=True,
                )

            self.assertEqual(response["status"], "resumed")
            self.assertTrue(queue.state["schedule_override_active"])
            self.assertIsNone(queue.state["pause_reason"])
        finally:
            parked_task.cancel()
            await asyncio.gather(parked_task, return_exceptions=True)

    async def test_status_reconciles_post_bootstrap_voice_cast_revision(self) -> None:
        class FakeQueue:
            def __init__(self) -> None:
                self.state = {
                    "project_id": "voice-cast-reconcile-test",
                    "status": PipelineStage.VOICE_REVIEW.value,
                    "active_stage": PipelineStage.VOICE_REVIEW.value,
                    "total_chapters": 0,
                    "voice_cast_revision": "current-revision",
                    "voice_cast": {
                        "fingerprint": "stale-revision",
                        "voices": {"stale": {}},
                    },
                }

            def get_job(self, project_id):
                return dict(self.state)

            def update_job(self, project_id, updates):
                self.state.update(updates)

        queue = FakeQueue()
        current_cast = {
            "fingerprint": "current-revision",
            "voices": {"current": {"ready": True}},
        }
        with tempfile.TemporaryDirectory() as directory:
            project_dir = Path(directory)
            (project_dir / "voice_cast.json").write_text(
                json.dumps(current_cast),
                encoding="utf-8",
            )
            with (
                patch.object(dashboard, "job_queue", queue),
                patch.object(dashboard, "running_tasks", {}),
                patch.object(dashboard, "_project_dir", return_value=project_dir),
            ):
                response = await dashboard.get_pipeline_status("voice-cast-reconcile-test")

        self.assertNotIn("voice_cast", response)
        self.assertEqual(
            response["voice_cast_summary"]["fingerprint"],
            "current-revision",
        )
        self.assertIsNone(queue.state["voice_cast"])

    async def test_status_does_not_treat_partial_segments_as_generated(self) -> None:
        class FakeQueue:
            def __init__(self) -> None:
                self.state = {
                    "project_id": "partial-generation-test",
                    "status": PipelineStage.GENERATING.value,
                    "active_stage": PipelineStage.GENERATING.value,
                    "total_chapters": 1,
                    "current_gen_chapter": 1,
                    "generated_chapters": [],
                    "scripted_chapters": [1],
                }

            def get_job(self, project_id):
                return dict(self.state)

            def update_job(self, project_id, updates):
                self.state.update(updates)

        queue = FakeQueue()
        active_task = asyncio.create_task(asyncio.sleep(60))
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project_dir = root / "project"
                workspace_dir = root / "workspace"
                (project_dir / "script").mkdir(parents=True)
                (project_dir / "manifests").mkdir(parents=True)
                (workspace_dir / "segments").mkdir(parents=True)
                (project_dir / "script" / "chapter_001.json").write_text(
                    json.dumps(
                        {
                            "chapter_number": 1,
                            "chapter_title": "One",
                            "lines": [
                                {"line_id": "ch01_0000"},
                                {"line_id": "ch01_0001"},
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                (workspace_dir / "segments" / "ch01_0000.wav").write_bytes(b"partial")
                manifest_path = project_dir / "manifests" / "chapter_001.segments.json"
                manifest_path.write_text(
                    json.dumps(
                        {
                            "segments": [
                                {"line_id": "ch01_0000"},
                                {"line_id": "ch01_0001"},
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                with (
                    patch.object(dashboard, "job_queue", queue),
                    patch.object(
                        dashboard,
                        "running_tasks",
                        {"partial-generation-test": active_task},
                    ),
                    patch.object(dashboard, "_project_dir", return_value=project_dir),
                    patch.object(
                        dashboard,
                        "_workspace_project_dir",
                        return_value=workspace_dir,
                    ),
                ):
                    partial = await dashboard.get_pipeline_status("partial-generation-test")
                    self.assertEqual(partial["generated_chapters"], [])
                    self.assertEqual(
                        partial["chapter_details"][0]["progress_percent"],
                        50,
                    )

                    manifest_path.write_text(
                        json.dumps(
                            {
                                "segments": [
                                    {
                                        "line_id": "ch01_0000",
                                        "output_hash": "a",
                                    },
                                    {
                                        "line_id": "ch01_0001",
                                        "output_hash": "b",
                                    },
                                ]
                            }
                        ),
                        encoding="utf-8",
                    )
                    complete = await dashboard.get_pipeline_status("partial-generation-test")
                    self.assertEqual(complete["generated_chapters"], [])
                    self.assertEqual(
                        complete["chapter_details"][0]["progress_percent"],
                        50,
                    )

                    # Only the pipeline's full dependency/hash reconciler may
                    # promote the durable state to complete.
                    queue.state["generated_chapters"] = [1]
                    reconciled = await dashboard.get_pipeline_status("partial-generation-test")
                    self.assertEqual(reconciled["generated_chapters"], [1])
                    self.assertEqual(
                        reconciled["chapter_details"][0]["progress_percent"],
                        100,
                    )
        finally:
            active_task.cancel()
            await asyncio.gather(active_task, return_exceptions=True)

    async def test_quality_endpoint_omits_routine_attempt_payloads(self) -> None:
        class FakeQueue:
            @staticmethod
            def get_quality_report(project_id):
                base = {
                    "chapter_number": 1,
                    "wer": 0.0,
                    "quality_score": 1.0,
                    "details": {"selected": True},
                }
                return [
                    {**base, "line_id": "routine", "attempt": 1, "status": "pass"},
                    {
                        **base,
                        "line_id": "retried",
                        "attempt": 1,
                        "status": "fail",
                        "details": {"selected": False},
                    },
                    {**base, "line_id": "retried", "attempt": 2, "status": "pass"},
                    {
                        **base,
                        "line_id": "warning",
                        "attempt": 1,
                        "status": "accepted_with_warning",
                    },
                ]

        with patch.object(dashboard, "job_queue", FakeQueue()):
            response = await dashboard.get_quality_report("book")

        self.assertEqual(response["total_segments"], 3)
        self.assertEqual(
            {item["line_id"] for item in response["final_attempts"]},
            {"retried", "warning"},
        )
        self.assertEqual(len(response["attempts"]), 2)
        self.assertNotIn("routine", {item["line_id"] for item in response["attempts"]})

    async def test_quality_endpoint_archives_results_outside_current_generation(self) -> None:
        class FakeQueue:
            @staticmethod
            def get_job(project_id):
                return {"generated_chapters": [], "mastered_chapters": []}

            @staticmethod
            def get_review_items(project_id):
                return []

            @staticmethod
            def get_quality_report(project_id):
                return [
                    {
                        "line_id": "old-line",
                        "chapter_number": 1,
                        "attempt": 1,
                        "status": "pass",
                        "wer": 0.0,
                        "quality_score": 1.0,
                        "details": {"selected": True},
                    }
                ]

        with patch.object(dashboard, "job_queue", FakeQueue()):
            response = await dashboard.get_quality_report("book")

        self.assertEqual(response["total_segments"], 0)
        self.assertEqual(response["stale_records"], 1)
        self.assertTrue(response["stale"])
        self.assertEqual(response["final_attempts"], [])

    async def test_quality_endpoint_keeps_the_active_audio_review_blocker(self) -> None:
        class FakeQueue:
            @staticmethod
            def get_job(project_id):
                return {
                    "status": "waiting_for_review",
                    "active_stage": "waiting_for_review",
                    "generated_chapters": [],
                    "mastered_chapters": [],
                    "review_blocking_item_ids": ["current-line"],
                }

            @staticmethod
            def get_review_items(project_id):
                return [
                    {
                        "item_type": "segment",
                        "item_id": "current-line",
                        "disposition": "unreviewed",
                    }
                ]

            @staticmethod
            def get_quality_report(project_id):
                return [
                    {
                        "line_id": "current-line",
                        "chapter_number": 2,
                        "attempt": 1,
                        "status": "flagged",
                        "wer": 0.1,
                        "quality_score": 0.7,
                        "details": {"selected": True, "manual_review_required": True},
                    }
                ]

        with patch.object(dashboard, "job_queue", FakeQueue()):
            response = await dashboard.get_quality_report("book")

        self.assertEqual(response["total_segments"], 1)
        self.assertEqual(response["stale_records"], 0)
        self.assertEqual(response["final_attempts"][0]["line_id"], "current-line")

    async def test_stop_outside_hours_can_park_for_scheduled_resume(self) -> None:
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
            stopped = False

            @staticmethod
            def schedule_is_open():
                return False

            def stop(self, project_id):
                self.stopped = True

        queue = FakeQueue()
        fake_pipeline = FakePipeline()
        with (
            patch.object(dashboard, "pipeline", fake_pipeline),
            patch.object(dashboard, "job_queue", queue),
        ):
            response = await dashboard.stop_pipeline(
                "book",
                resume_on_schedule=True,
            )

        self.assertTrue(response["will_resume_on_schedule"])
        self.assertTrue(fake_pipeline.stopped)
        self.assertEqual(
            queue.state["status"],
            PipelineStage.PAUSED_SCHEDULED.value,
        )
        self.assertEqual(
            queue.state["pause_reason"],
            "waiting for configured working hours",
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
                json.dumps(
                    {
                        "voices": {
                            "narrator": {
                                "file": str(sample),
                                "name": "The Narrator",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(dashboard, "_voice_project_dir", return_value=voice_dir),
                patch.object(dashboard, "_require_job", return_value={"title": "A Book: One?"}),
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
            # Windows runners may expose the same temp directory through its
            # long name in one path and its 8.3 alias (RUNNER~1) in another.
            self.assertTrue(os.path.samefile(response.path, sample))

    async def test_character_profile_correction_is_persistent_and_scoped(self) -> None:
        class FakeQueue:
            def __init__(self) -> None:
                self.updates = []

            @staticmethod
            def get_job(project_id):
                return {"project_id": project_id, "running": False}

            def update_job(self, project_id, updates):
                self.updates.append(updates)

        registry = {
            "book_title": "Book",
            "book_author": "Author",
            "characters": {
                "speaker": {
                    "id": "speaker",
                    "name": "Speaker",
                    "gender": "other",
                    "age_range": "adult",
                    "voice_description": "A neutral speaking voice",
                    "speaking_style": "measured",
                }
            },
        }
        cast = {
            "voices": {
                "speaker": {
                    "voice_id": "speaker",
                    "owner_character_id": "speaker",
                    "assigned_characters": ["speaker"],
                    "warnings": [],
                }
            }
        }
        queue = FakeQueue()
        with tempfile.TemporaryDirectory() as directory:
            project_dir = Path(directory)
            chars_path = project_dir / "characters.json"
            chars_path.write_text(json.dumps(registry), encoding="utf-8")
            with (
                patch.object(dashboard, "job_queue", queue),
                patch.object(dashboard, "_project_dir", return_value=project_dir),
                patch.object(
                    dashboard,
                    "_load_character_registry",
                    return_value=(chars_path, registry),
                ),
                patch.object(dashboard, "_load_or_build_voice_cast", return_value=cast),
                patch.object(dashboard, "_save_voice_cast") as save_cast,
                patch.object(dashboard, "_chapters_for_speakers", return_value=[2, 7]),
                patch.object(dashboard, "_mark_voice_chapters_stale") as mark_stale,
            ):
                result = await dashboard.update_character_profile(
                    "book",
                    "speaker",
                    dashboard.CharacterProfileUpdate(
                        gender="female",
                        age_range="30s",
                        voice_description="A warm, precise speaking voice",
                        speaking_style="calm and deliberate",
                    ),
                )

            overrides = json.loads((project_dir / "character_overrides.json").read_text(encoding="utf-8"))
            self.assertEqual(overrides["characters"]["speaker"]["gender"], "female")
            self.assertEqual(result["affected_chapters"], [2, 7])
            self.assertTrue(result["requires_voice_regeneration"])
            self.assertEqual(cast["voices"]["speaker"]["design_fingerprint"], "")
            mark_stale.assert_called_once_with("book", [2, 7])
            save_cast.assert_called_once()
            self.assertTrue(any(update.get("voice_review_status") == "pending" for update in queue.updates))

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

    async def test_all_voice_download_contains_named_samples_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            voice_dir = Path(directory)
            base = voice_dir / "child.wav"
            candidate = voice_dir / "child-candidate.wav"
            base.write_bytes(b"RIFF" + b"1" * 1200)
            candidate.write_bytes(b"RIFF" + b"2" * 1200)
            (voice_dir / "voices.json").write_text(
                json.dumps(
                    {
                        "voices": {
                            "child": {"file": str(base), "source_type": "generated"},
                            "child_cand2": {
                                "file": str(candidate),
                                "source_type": "generated",
                                "ref_text": "A reusable reference.",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            cast = {
                "voices": {
                    "child": {
                        "name": "Child Male",
                        "owner_character_id": "child",
                        "assigned_characters": ["child"],
                    },
                    "child_cand2": {
                        "name": "Candidate 2",
                        "owner_character_id": "child",
                        "assigned_characters": [],
                    },
                }
            }
            with (
                patch.object(dashboard, "_voice_project_dir", return_value=voice_dir),
                patch.object(dashboard, "_require_job", return_value={"title": "A Book"}),
                patch.object(dashboard, "_load_or_build_voice_cast", return_value=cast),
            ):
                response = await dashboard.download_all_project_voices("book")
                response_all = await dashboard.download_all_project_voices("book", all_variants=True)

            self.assertEqual(response.media_type, "application/zip")
            self.assertEqual(response.headers["x-voice-sample-count"], "1")
            with zipfile.ZipFile(io.BytesIO(response.body)) as bundle:
                names = set(bundle.namelist())
                self.assertIn("A Book - Child Male - voice-reference.wav", names)
                self.assertNotIn(
                    "A Book - Child Male - Candidate 2 - voice-reference.wav",
                    names,
                )
                manifest = json.loads(bundle.read("voice-samples.json"))
            self.assertEqual(len(manifest["samples"]), 1)

            self.assertEqual(response_all.headers["x-voice-sample-count"], "2")
            with zipfile.ZipFile(io.BytesIO(response_all.body)) as bundle_all:
                names_all = set(bundle_all.namelist())
                self.assertIn("A Book - Child Male - voice-reference.wav", names_all)
                self.assertIn(
                    "A Book - Child Male - Candidate 2 - voice-reference.wav",
                    names_all,
                )
                manifest_all = json.loads(bundle_all.read("voice-samples.json"))
            self.assertEqual(len(manifest_all["samples"]), 2)
            self.assertEqual(
                manifest_all["samples"][1]["reference_text"],
                "A reusable reference.",
            )

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
                    dashboard.VoiceRegenerationRequest(voice_description="A warm, composed audiobook narrator voice"),
                )

        self.assertEqual(result["status"], "success")
        self.assertIn("narrator_male", fake_pipeline.voice_client.request.characters)


if __name__ == "__main__":
    unittest.main()
