"""Tests for Mobile API routes (/api/mobile/v1/*) and streaming."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from brain.dashboard.api.main import app
from brain.orchestrator.job_queue import JobQueue


class MobileApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_pipeline.db"
        self.job_queue = JobQueue(db_path=str(self.db_path))

        # Attach test job queue to app state
        app.state.job_queue = self.job_queue
        app.state.running_tasks = {}

        self.client = TestClient(app, client=("127.0.0.1", 50000))

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_server_info_endpoint(self):
        response = self.client.get("/api/mobile/v1/server-info")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["server_name"], "Crazy Audiobook Creator")
        self.assertEqual(data["version"], "2.0.0")
        self.assertTrue(data["capabilities"]["streaming"])
        self.assertTrue(data["capabilities"]["byte_ranges"])
        self.assertTrue(data["capabilities"]["wav_chapter_streaming"])
        self.assertTrue(data["capabilities"]["progress_sync"])
        self.assertFalse(data["is_busy"])

    def test_progress_save_and_get(self):
        # Create a test project directory
        project_id = "test_progress_book"
        project_dir = Path("brain/projects") / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.job_queue.create_job(project_id, {"title": "Test Book", "status": "generating"})

            # Initially empty progress
            get_resp = self.client.get(f"/api/mobile/v1/books/{project_id}/progress")
            self.assertEqual(get_resp.status_code, 200)
            self.assertFalse(get_resp.json()["has_progress"])

            # Save progress
            payload = {
                "client_id": "voice_android_test",
                "chapter_number": 3,
                "position_ms": 45000,
                "playback_speed": 1.2,
                "is_completed": False,
            }
            save_resp = self.client.post(
                f"/api/mobile/v1/books/{project_id}/progress",
                json=payload,
            )
            self.assertEqual(save_resp.status_code, 200)
            self.assertEqual(save_resp.json()["status"], "synced")

            # Get progress again
            get_resp2 = self.client.get(f"/api/mobile/v1/books/{project_id}/progress")
            self.assertEqual(get_resp2.status_code, 200)
            data = get_resp2.json()
            self.assertTrue(data["has_progress"])
            self.assertEqual(data["chapter_number"], 3)
            self.assertEqual(data["position_ms"], 45000)
            self.assertEqual(data["playback_speed"], 1.2)
            self.assertFalse(data["is_completed"])
        finally:
            import shutil
            if project_dir.exists():
                shutil.rmtree(project_dir)

    def test_catalog_endpoint(self):
        project_id = "test_catalog_book"
        project_dir = Path("brain/projects") / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        try:
            book_json = project_dir / "book.json"
            book_json.write_text(
                json.dumps({
                    "metadata": {
                        "title": "A Great Adventure",
                        "author": "John Doe",
                        "genre": "Fantasy",
                        "year": "2026",
                        "description": "An epic journey",
                        "isbn": "1234567890",
                    },
                    "chapters": [{"title": "Ch 1"}, {"title": "Ch 2"}],
                }),
                encoding="utf-8",
            )
            self.job_queue.create_job(project_id, {
                "title": "A Great Adventure",
                "author": "John Doe",
                "status": "complete",
                "total_chapters": 2,
                "mastered_chapters": [1, 2],
            })

            # Create dummy m4b file
            m4b_file = project_dir / f"{project_id}.m4b"
            m4b_file.write_bytes(b"\x00" * 1024)

            response = self.client.get("/api/mobile/v1/catalog")
            self.assertEqual(response.status_code, 200)
            books = response.json()["books"]
            found = next((b for b in books if b["project_id"] == project_id), None)
            self.assertIsNotNone(found)
            self.assertEqual(found["title"], "A Great Adventure")
            self.assertEqual(found["author"], "John Doe")
            self.assertEqual(found["genre"], "Fantasy")
            self.assertEqual(found["status"], "ready_full")
            self.assertEqual(found["stream_url"], f"api/projects/{project_id}/stream")
            self.assertEqual(found["download_url"], f"api/projects/{project_id}/download")
            self.assertEqual(found["file_size_bytes"], 1024)
        finally:
            import shutil
            if project_dir.exists():
                shutil.rmtree(project_dir)

    def test_book_detail_endpoint(self):
        project_id = "test_detail_book"
        project_dir = Path("brain/projects") / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        try:
            book_json = project_dir / "book.json"
            book_json.write_text(
                json.dumps({
                    "metadata": {
                        "title": "Detailed Story",
                        "author": "Jane Smith",
                    },
                    "chapters": [
                        {"title": "Prologue"},
                        {"title": "The Awakening"},
                    ],
                }),
                encoding="utf-8",
            )
            self.job_queue.create_job(project_id, {
                "title": "Detailed Story",
                "author": "Jane Smith",
                "status": "generating",
                "total_chapters": 2,
                "mastered_chapters": [1],
                "generated_chapters": [1, 2],
            })

            response = self.client.get(f"/api/mobile/v1/books/{project_id}")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["project_id"], project_id)
            self.assertEqual(data["title"], "Detailed Story")
            self.assertEqual(len(data["chapters"]), 2)
            self.assertEqual(data["chapters"][0]["title"], "Chapter 1: Prologue")
            self.assertEqual(data["chapters"][0]["raw_title"], "Prologue")
            self.assertEqual(data["chapters"][0]["status"], "mastered")
            self.assertEqual(data["chapters"][1]["title"], "Chapter 2: The Awakening")
            self.assertEqual(data["chapters"][1]["raw_title"], "The Awakening")
            self.assertEqual(data["chapters"][1]["status"], "generating")
        finally:
            import shutil
            if project_dir.exists():
                shutil.rmtree(project_dir)

    def test_stream_range_requests(self):
        project_id = "test_stream_book"
        project_dir = Path("brain/projects") / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        try:
            # Create a 2048-byte dummy audio file
            m4b_file = project_dir / f"{project_id}.m4b"
            dummy_bytes = bytes([i % 256 for i in range(2048)])
            m4b_file.write_bytes(dummy_bytes)

            # Normal full stream request
            response = self.client.get(f"/api/projects/{project_id}/stream")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("accept-ranges"), "bytes")
            self.assertEqual(len(response.content), 2048)

            # Range request for bytes 0-511
            range_resp = self.client.get(
                f"/api/projects/{project_id}/stream",
                headers={"Range": "bytes=0-511"},
            )
            self.assertEqual(range_resp.status_code, 206)
            self.assertEqual(len(range_resp.content), 512)
            self.assertEqual(range_resp.content, dummy_bytes[0:512])
            self.assertIn("bytes 0-511/2048", range_resp.headers.get("content-range", ""))

            # Range request for second half: 1024-2047
            range_resp2 = self.client.get(
                f"/api/projects/{project_id}/stream",
                headers={"Range": "bytes=1024-2047"},
            )
            self.assertEqual(range_resp2.status_code, 206)
            self.assertEqual(len(range_resp2.content), 1024)
            self.assertEqual(range_resp2.content, dummy_bytes[1024:2048])
        finally:
            import shutil
            if project_dir.exists():
                shutil.rmtree(project_dir)
