from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import brain.dashboard.api.main as dashboard
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


if __name__ == "__main__":
    unittest.main()
