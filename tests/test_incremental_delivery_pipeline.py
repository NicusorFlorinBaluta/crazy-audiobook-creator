from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from fastapi import HTTPException

from brain.dashboard.api.main import (
    DeliverySettingsRequest,
    cancel_pause_after_delivery,
    download_delivery,
    get_deliveries,
    request_pause_after_delivery,
    update_delivery_settings,
)
import brain.dashboard.api.main as dashboard_main
from brain.orchestrator.delivery_manager import DeliveryManager
from brain.orchestrator.job_queue import JobQueue
from brain.orchestrator.pipeline import Pipeline, _GracefulDeliveryPause
from shared.constants import PipelineStage
from shared.artifacts import fingerprint, hash_file
from shared.models import ScriptChapter


class IncrementalDeliveryPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.temp_dir.name)
        self.scripts_dir = self.project_dir / "script"
        self.scripts_dir.mkdir(parents=True, exist_ok=True)

        # Create mock chapters 1 through 6
        for ch_num in range(1, 7):
            ch_data = ScriptChapter(
                chapter_number=ch_num,
                chapter_title=f"Chapter {ch_num}",
                lines=[],
            )
            (self.scripts_dir / f"chapter_{ch_num:03d}.json").write_text(
                ch_data.model_dump_json(), encoding="utf-8"
            )

        # Create book.json
        (self.project_dir / "book.json").write_text(
            json.dumps({"metadata": {"title": "Test Novel", "author": "Tester"}}),
            encoding="utf-8",
        )

        self.db_path = self.project_dir / "test_pipeline.db"
        self.job_queue = JobQueue(db_path=str(self.db_path))
        self.project_id = "test-project"
        self.job_queue.create_job(self.project_id, {"status": "created"})

        # Initialize Pipeline
        self.pipeline = Pipeline()
        self.pipeline.job_queue = self.job_queue

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_master_manifests(self, chapter_numbers: set[int]) -> None:
        manifests = self.project_dir / "manifests"
        manifests.mkdir(exist_ok=True)
        for number in chapter_numbers:
            (manifests / f"chapter_{number:03d}.master.json").write_text(
                json.dumps({"mastering_quality": {"lufs": -18.0}}),
                encoding="utf-8",
            )

    def test_run_incremental_delivery_publishes_all_batches_and_final_export(self) -> None:
        generated_batches: list[set[int]] = []
        mastered_batches: list[set[int]] = []
        exported_partials: list[dict] = []
        full_export_called = False

        def mock_gen(proj_id, pdir, ch_nums=None):
            generated_batches.append(ch_nums)

        def mock_master(proj_id, pdir, ch_nums=None):
            mastered_batches.append(ch_nums)
            self._write_master_manifests(ch_nums)

        def mock_export(proj_id, pdir, partial=False, chapter_selection=None, temp_output=None, **kwargs):
            nonlocal full_export_called
            if partial and temp_output:
                exported_partials.append({
                    "chapters": chapter_selection,
                    "temp_output": temp_output,
                })
                # Simulate export output
                temp_output.write_bytes(b"dummy partial audio content")
                return {"duration_seconds": 120.0}
            else:
                full_export_called = True
                return {"duration_seconds": 240.0}

        self.pipeline._run_generation = mock_gen
        self.pipeline._run_mastering = mock_master
        self.pipeline._run_export = mock_export

        # Set batch size = 4 (chapters 1..4 -> part-001, chapters 5..6 -> part-002)
        self.job_queue.update_job(
            self.project_id,
            {"incremental_delivery": {"enabled": True, "batch_size": 4}},
        )

        self.pipeline._run_incremental_delivery(
            self.project_id, self.project_dir, PipelineStage.GENERATING
        )

        # 2 batches planned
        self.assertEqual(generated_batches, [{1, 2, 3, 4}, {5, 6}])
        self.assertEqual(mastered_batches, [{1, 2, 3, 4}, {5, 6}])
        self.assertEqual(len(exported_partials), 2)
        self.assertTrue(full_export_called)

        # Verify DeliveryManager index
        dm = DeliveryManager(self.project_dir)
        index = dm.load_index()
        self.assertEqual(len(index.deliveries), 2)
        self.assertEqual(index.deliveries[0].delivery_id, "part-001")
        self.assertEqual(index.deliveries[0].chapter_numbers, [1, 2, 3, 4])
        self.assertEqual(index.deliveries[1].delivery_id, "part-002")
        self.assertEqual(index.deliveries[1].chapter_numbers, [5, 6])

        # Verify job queue state
        state = self.job_queue.get_job(self.project_id)
        self.assertEqual(state.get("published_delivery_count"), 2)
        self.assertEqual(state.get("latest_published_delivery_id"), "part-002")
        self.assertIsNone(state.get("active_delivery_id"))

    def test_run_incremental_delivery_skips_already_published_batches(self) -> None:
        dm = DeliveryManager(self.project_dir)
        self._write_master_manifests({1, 2, 3, 4})
        script_files = sorted(self.scripts_dir.glob("chapter_*.json"))
        script_dependency = fingerprint(
            {
                str(number): hash_file(path)
                for number, path in enumerate(script_files, 1)
            }
        )
        index, batches = dm.ensure_plan(
            [1, 2, 3, 4, 5, 6],
            4,
            script_dependency_fingerprint=script_dependency,
        )
        temp_art = self.project_dir / "temp1.m4b"
        temp_art.write_bytes(b"batch 1 audio")
        master_hashes = {
            str(number): hash_file(
                self.project_dir / "manifests" / f"chapter_{number:03d}.master.json"
            )
            for number in [1, 2, 3, 4]
        }
        dm.publish_delivery(
            batch=batches[0],
            temp_artifact_path=temp_art,
            duration_seconds=100.0,
            master_manifest_hashes=master_hashes,
            metadata_fingerprint=fingerprint(
                {
                    "metadata": {"title": "Test Novel", "author": "Tester"},
                    "cover_hash": "",
                }
            ),
            book_title="Test Novel",
            plan_fingerprint=index.plan_fingerprint,
        )

        generated_batches: list[set[int]] = []

        def mock_gen(proj_id, pdir, ch_nums=None):
            generated_batches.append(ch_nums)

        def mock_master(proj_id, pdir, ch_nums=None):
            self._write_master_manifests(ch_nums)

        def mock_export(proj_id, pdir, partial=False, chapter_selection=None, temp_output=None, **kwargs):
            if temp_output:
                temp_output.write_bytes(b"batch 2 audio")
                return {"duration_seconds": 120.0}
            return {"duration_seconds": 240.0}

        self.pipeline._run_generation = mock_gen
        self.pipeline._run_mastering = mock_master
        self.pipeline._run_export = mock_export

        self.job_queue.update_job(
            self.project_id,
            {"incremental_delivery": {"enabled": True, "batch_size": 4}},
        )

        self.pipeline._run_incremental_delivery(
            self.project_id, self.project_dir, PipelineStage.GENERATING
        )

        # Batch 1 was already valid, so only Batch 2 ({5, 6}) was generated
        self.assertEqual(generated_batches, [{5, 6}])

        index = dm.load_index()
        self.assertEqual(len(index.deliveries), 2)

    def test_run_incremental_delivery_graceful_pause(self) -> None:
        def mock_gen(proj_id, pdir, ch_nums=None):
            # Request pause during batch 1 execution
            self.job_queue.update_job(proj_id, {"pause_after_delivery_requested": True})

        def mock_master(proj_id, pdir, ch_nums=None):
            self._write_master_manifests(ch_nums)

        def mock_export(proj_id, pdir, partial=False, chapter_selection=None, temp_output=None, **kwargs):
            if temp_output:
                temp_output.write_bytes(b"batch 1 audio")
                return {"duration_seconds": 120.0}
            return {"duration_seconds": 240.0}

        self.pipeline._run_generation = mock_gen
        self.pipeline._run_mastering = mock_master
        self.pipeline._run_export = mock_export

        self.job_queue.update_job(
            self.project_id,
            {"incremental_delivery": {"enabled": True, "batch_size": 4}},
        )

        with self.assertRaises(_GracefulDeliveryPause):
            self.pipeline._run_incremental_delivery(
                self.project_id, self.project_dir, PipelineStage.GENERATING
            )

        state = self.job_queue.get_job(self.project_id)
        self.assertEqual(state.get("status"), PipelineStage.PAUSED.value)
        self.assertFalse(state.get("pause_after_delivery_requested"))
        self.assertIn("Graceful pause", state.get("pause_reason", ""))
        self.assertEqual(state.get("published_delivery_count"), 1)

    def test_explicit_empty_chapter_selection_does_not_crash(self) -> None:
        self.pipeline._assert_attribution_audit = MagicMock(return_value={"passed": True})
        self.pipeline._run_generation(self.project_id, self.project_dir, set())
        self.pipeline._run_mastering(self.project_id, self.project_dir, set())

    def test_generation_scopes_attribution_gate_to_selected_chapters(self) -> None:
        self.pipeline._assert_attribution_audit = MagicMock(return_value={"passed": True})

        # The selected chapter need not exist for this gate-contract test;
        # keeping it outside the fixture avoids contacting the voice service.
        self.pipeline._run_generation(self.project_id, self.project_dir, {99})

        self.pipeline._assert_attribution_audit.assert_called_once_with(
            self.project_dir,
            enforce=False,
            chapter_numbers={99},
        )

    def test_partial_export_scopes_attribution_gate_to_selected_chapters(self) -> None:
        self.pipeline._assert_attribution_audit = MagicMock(
            side_effect=RuntimeError("stop after audit")
        )

        with self.assertRaisesRegex(RuntimeError, "stop after audit"):
            self.pipeline._run_export(
                self.project_id,
                self.project_dir,
                partial=True,
                chapter_selection={3},
                acquire_packaging_lock=False,
            )

        self.pipeline._assert_attribution_audit.assert_called_once_with(
            self.project_dir,
            chapter_numbers={3},
        )

    def test_full_export_keeps_book_wide_attribution_gate(self) -> None:
        self.pipeline._assert_attribution_audit = MagicMock(
            side_effect=RuntimeError("stop after audit")
        )

        with self.assertRaisesRegex(RuntimeError, "stop after audit"):
            self.pipeline._run_export(
                self.project_id,
                self.project_dir,
                acquire_packaging_lock=False,
            )

        self.pipeline._assert_attribution_audit.assert_called_once_with(
            self.project_dir,
            chapter_numbers=None,
        )


class IncrementalDeliveryApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.projects_root = Path(self.temp_dir.name)
        self.project_dir = self.projects_root / "test-book"
        self.project_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = self.project_dir / "test.db"
        self.job_queue = JobQueue(db_path=str(self.db_path))
        self.project_id = "test-book"
        self.job_queue.create_job(
            self.project_id,
            {
                "status": "idle",
                "incremental_delivery": {"enabled": False, "batch_size": 5},
            },
        )

        self.prev_job_queue = dashboard_main.job_queue
        self.prev_pipeline = dashboard_main.pipeline
        dashboard_main.job_queue = self.job_queue
        dashboard_main.pipeline = MagicMock()
        dashboard_main.pipeline.job_queue = self.job_queue

        # Patch _project_dir in main.py to point to our test directory
        self.project_dir_patcher = patch(
            "brain.dashboard.api.main._project_dir",
            return_value=self.project_dir,
        )
        self.project_dir_patcher.start()

    async def asyncTearDown(self) -> None:
        dashboard_main.job_queue = self.prev_job_queue
        dashboard_main.pipeline = self.prev_pipeline
        self.project_dir_patcher.stop()
        self.temp_dir.cleanup()

    async def test_update_delivery_settings_endpoint(self) -> None:
        req = DeliverySettingsRequest(enabled=True, batch_size=10)
        res = await update_delivery_settings(self.project_id, req)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["settings"]["enabled"], True)
        self.assertEqual(res["settings"]["batch_size"], 10)

        # Verify job state in database
        state = self.job_queue.get_job(self.project_id)
        self.assertEqual(state["incremental_delivery"]["enabled"], True)
        self.assertEqual(state["incremental_delivery"]["batch_size"], 10)

    async def test_get_deliveries_endpoint(self) -> None:
        dm = DeliveryManager(self.project_dir)
        temp_art = self.project_dir / "temp.m4b"
        temp_art.write_bytes(b"dummy part audio")

        batch = dm.plan_deliveries([1, 2, 3], batch_size=3)[0]
        dm.publish_delivery(
            batch=batch,
            temp_artifact_path=temp_art,
            duration_seconds=120.0,
            master_manifest_hashes={},
            metadata_fingerprint="",
            book_title="Test Book",
        )

        res = await get_deliveries(self.project_id)
        self.assertEqual(res["published_count"], 1)
        self.assertEqual(len(res["deliveries"]), 1)
        self.assertEqual(res["deliveries"][0]["delivery_id"], "part-001")
        self.assertEqual(res["deliveries"][0]["chapter_numbers"], [1, 2, 3])

    async def test_download_delivery_endpoint(self) -> None:
        dm = DeliveryManager(self.project_dir)
        temp_art = self.project_dir / "temp.m4b"
        temp_art.write_bytes(b"sample delivery audio")

        batch = dm.plan_deliveries([1, 2], batch_size=2)[0]
        part = dm.publish_delivery(
            batch=batch,
            temp_artifact_path=temp_art,
            duration_seconds=60.0,
            master_manifest_hashes={},
            metadata_fingerprint="",
            book_title="Test Book",
        )

        response = await download_delivery(self.project_id, "part-001")
        self.assertEqual(response.filename, part.artifact)
        self.assertEqual(response.media_type, "audio/mp4")
        self.assertTrue(Path(response.path).exists())

        Path(response.path).write_bytes(b"tampered")
        with self.assertRaises(HTTPException) as raised:
            await download_delivery(self.project_id, "part-001")
        self.assertEqual(raised.exception.status_code, 404)

    async def test_delivery_settings_reject_manual_selection_conflict(self) -> None:
        self.job_queue.update_job(
            self.project_id,
            {"generation_chapter_selection": [1]},
        )
        with self.assertRaises(HTTPException) as raised:
            await update_delivery_settings(
                self.project_id,
                DeliverySettingsRequest(enabled=True, batch_size=5),
            )
        self.assertEqual(raised.exception.status_code, 409)

    async def test_pause_after_delivery_toggle_endpoints(self) -> None:
        self.job_queue.update_job(
            self.project_id,
            {
                "running": True,
                "incremental_delivery": {"enabled": True, "batch_size": 5},
            },
        )
        res1 = await request_pause_after_delivery(self.project_id)
        self.assertEqual(res1["status"], "success")
        state1 = self.job_queue.get_job(self.project_id)
        self.assertTrue(state1["pause_after_delivery_requested"])

        res2 = await cancel_pause_after_delivery(self.project_id)
        self.assertEqual(res2["status"], "success")
        state2 = self.job_queue.get_job(self.project_id)
        self.assertFalse(state2["pause_after_delivery_requested"])


if __name__ == "__main__":
    unittest.main()
