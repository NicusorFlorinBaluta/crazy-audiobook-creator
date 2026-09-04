"""Tests for Mobile API routes (/api/mobile/v1/*) and streaming."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_remote_mobile_routes_require_token_but_discovery_is_public(self):
        remote = TestClient(app, client=("192.0.2.10", 50000))
        with patch.dict(
            "os.environ",
            {"CRAZY_AUDIOBOOK_DASHBOARD_TOKEN": "mobile-secret"},
            clear=False,
        ):
            self.assertEqual(
                remote.get("/api/mobile/v1/server-info").status_code,
                200,
            )
            self.assertEqual(remote.get("/api/mobile/v1/catalog").status_code, 401)
            self.assertEqual(
                remote.get(
                    "/api/mobile/v1/catalog",
                    headers={"X-API-Token": "mobile-secret"},
                ).status_code,
                200,
            )

    def test_cross_site_progress_mutation_is_rejected(self):
        response = self.client.post(
            "/api/mobile/v1/books/example/progress",
            headers={"Sec-Fetch-Site": "cross-site"},
            json={"chapter_number": 1, "position_ms": 0},
        )
        self.assertEqual(response.status_code, 403)

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
                json.dumps(
                    {
                        "metadata": {
                            "title": "A Great Adventure",
                            "author": "John Doe",
                            "genre": "Fantasy",
                            "year": "2026",
                            "description": "An epic journey",
                            "isbn": "1234567890",
                        },
                        "chapters": [{"title": "Ch 1"}, {"title": "Ch 2"}],
                    }
                ),
                encoding="utf-8",
            )
            self.job_queue.create_job(
                project_id,
                {
                    "title": "A Great Adventure",
                    "author": "John Doe",
                    "status": "complete",
                    "total_chapters": 2,
                    "mastered_chapters": [1, 2],
                },
            )

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
                json.dumps(
                    {
                        "metadata": {
                            "title": "Detailed Story",
                            "author": "Jane Smith",
                        },
                        "chapters": [
                            {"title": "Prologue"},
                            {"title": "The Awakening"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self.job_queue.create_job(
                project_id,
                {
                    "title": "Detailed Story",
                    "author": "Jane Smith",
                    "status": "generating",
                    "total_chapters": 2,
                    "mastered_chapters": [1],
                    "generated_chapters": [1, 2],
                },
            )

            response = self.client.get(f"/api/mobile/v1/books/{project_id}")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["project_id"], project_id)
            self.assertEqual(data["chapters"][0]["title"], "Prologue")
            self.assertEqual(data["chapters"][0]["source_heading"], "Prologue")
            self.assertEqual(data["chapters"][0]["raw_title"], "Prologue")
            self.assertEqual(data["chapters"][0]["status"], "mastered")
            self.assertEqual(data["chapters"][1]["title"], "The Awakening")
            self.assertEqual(data["chapters"][1]["raw_title"], "The Awakening")
            self.assertEqual(data["chapters"][1]["status"], "generating")
        finally:
            import shutil

            if project_dir.exists():
                shutil.rmtree(project_dir)

    def test_export_manifest_does_not_mark_unexported_chapters_mastered(self):
        project_id = "test_partial_manifest"
        project_dir = Path("brain/projects") / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        try:
            (project_dir / "book.json").write_text(
                json.dumps(
                    {
                        "metadata": {"title": "Subset", "author": "Author"},
                        "chapters": [
                            {"title": "One"},
                            {"title": "Two"},
                            {"title": "Three"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (project_dir / f"{project_id}.m4b").write_bytes(b"audio")
            (project_dir / "export_quality.json").write_text(
                json.dumps(
                    {
                        "partial": True,
                        "chapters": [2],
                        "output_file": str(project_dir / f"{project_id}.m4b"),
                    }
                ),
                encoding="utf-8",
            )
            self.job_queue.create_job(
                project_id,
                {
                    "title": "Subset",
                    "status": "complete",
                    "total_chapters": 3,
                    "mastered_chapters": [],
                    "generated_chapters": [],
                },
            )

            response = self.client.get(f"/api/mobile/v1/books/{project_id}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                [chapter["status"] for chapter in response.json()["chapters"]],
                ["pending", "mastered", "pending"],
            )
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

    def test_catalog_excludes_orphaned_directories_not_in_job_queue(self):
        active_id = "test_active_catalog_book"
        orphan_id = "test_orphaned_catalog_book"
        active_dir = Path("brain/projects") / active_id
        orphan_dir = Path("brain/projects") / orphan_id
        active_dir.mkdir(parents=True, exist_ok=True)
        orphan_dir.mkdir(parents=True, exist_ok=True)
        try:
            (active_dir / "book.json").write_text(json.dumps({"title": "Active Book"}), encoding="utf-8")
            (orphan_dir / "book.json").write_text(json.dumps({"title": "Orphaned Book"}), encoding="utf-8")
            (active_dir / f"{active_id}.m4b").write_bytes(b"dummy audio content")
            (orphan_dir / f"{orphan_id}.m4b").write_bytes(b"dummy orphan content")
            self.job_queue.create_job(active_id, {"title": "Active Book", "status": "completed"})

            response = self.client.get("/api/mobile/v1/catalog")
            self.assertEqual(response.status_code, 200)
            catalog_books = response.json()["books"]
            catalog_ids = {b["project_id"] for b in catalog_books}

            self.assertIn(active_id, catalog_ids)
            self.assertNotIn(orphan_id, catalog_ids)
        finally:
            import shutil

            if active_dir.exists():
                shutil.rmtree(active_dir)
            if orphan_dir.exists():
                shutil.rmtree(orphan_dir)

    def test_delivery_batches_include_relative_chapter_details(self):
        project_id = "test_delivery_detail_book"
        project_dir = Path("brain/projects") / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        deliv_dir = project_dir / "deliveries"
        deliv_dir.mkdir(parents=True, exist_ok=True)
        manifests_dir = project_dir / "manifests"
        manifests_dir.mkdir(parents=True, exist_ok=True)

        try:
            (project_dir / "book.json").write_text(
                json.dumps(
                    {
                        "metadata": {"title": "Delivery Book", "author": "Author"},
                        "chapters": [
                            {"title": "Prologue"},
                            {"title": "Chapter One"},
                            {"title": "Chapter Two"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            # Write master manifests for durations
            for ch_num, dur in [(1, 100.0), (2, 200.0), (3, 300.0)]:
                (manifests_dir / f"chapter_{ch_num:03d}.master.json").write_text(
                    json.dumps(
                        {
                            "duration_seconds": dur,
                        }
                    ),
                    encoding="utf-8",
                )

            # Write delivery index
            (deliv_dir / "index.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "project_id": project_id,
                        "batch_size": 2,
                        "chapter_numbers": [1, 2],
                        "deliveries": [
                            {
                                "delivery_id": "part-001",
                                "ordinal": 1,
                                "chapter_numbers": [1, 2],
                                "status": "published",
                                "artifact": "Part-01.m4b",
                                "duration_seconds": 300.0,
                                "published_at": "2026-08-27T12:00:00Z",
                                "sha256": "abc123def456",
                                "bytes": 1024,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.job_queue.create_job(
                project_id,
                {
                    "title": "Delivery Book",
                    "status": "generating",
                    "total_chapters": 3,
                    "mastered_chapters": [1, 2],
                },
            )

            response = self.client.get(f"/api/mobile/v1/books/{project_id}")
            self.assertEqual(response.status_code, 200)
            data = response.json()

            deliveries = data.get("deliveries", [])
            self.assertEqual(len(deliveries), 1)
            part1 = deliveries[0]
            self.assertEqual(part1["delivery_id"], "part-001")
            self.assertEqual(part1["filename"], "Part-01.m4b")
            self.assertEqual(part1["duration_seconds"], 300.0)

            ch_details = part1.get("chapter_details", [])
            self.assertEqual(len(ch_details), 2)
            self.assertEqual(ch_details[0]["number"], 1)
            self.assertEqual(ch_details[0]["title"], "Prologue")
            self.assertEqual(ch_details[0]["start_ms"], 0)
            self.assertEqual(ch_details[0]["end_ms"], 100000)

            self.assertEqual(ch_details[1]["number"], 2)
            self.assertEqual(ch_details[1]["title"], "Chapter One")
            self.assertEqual(ch_details[1]["start_ms"], 100000)
            self.assertEqual(ch_details[1]["end_ms"], 300000)

            # Verify data["chapters"] also has relative offsets for delivery parts
            all_chaps = data.get("chapters", [])
            self.assertEqual(all_chaps[0]["start_ms"], 0)
            self.assertEqual(all_chaps[0]["end_ms"], 100000)
            self.assertEqual(all_chaps[1]["start_ms"], 100000)
            self.assertEqual(all_chaps[1]["end_ms"], 300000)
        finally:
            import shutil

            if project_dir.exists():
                shutil.rmtree(project_dir)

    def test_chapter_lyrics_endpoint(self):
        project_id = "test_lyrics_book"
        project_dir = Path("brain/projects") / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        try:
            # Create script
            script_dir = project_dir / "script"
            script_dir.mkdir(parents=True, exist_ok=True)
            script_data = {
                "chapter_number": 1,
                "chapter_title": "Prologue: The Awakening",
                "lines": [
                    {
                        "line_id": "ch01_0000",
                        "speaker": "Narrator",
                        "text": "The wind howled across the crags.",
                        "emotion": "ominous",
                        "source_start": 0,
                        "source_end": 33,
                    },
                    {
                        "line_id": "ch01_0001",
                        "speaker": "Kaladin",
                        "text": "We need to keep moving!",
                        "emotion": "urgent",
                        "source_start": 35,
                        "source_end": 58,
                    },
                ],
            }
            (script_dir / "chapter_001.json").write_text(json.dumps(script_data), encoding="utf-8")

            # Create timeline
            manifest_dir = project_dir / "manifests"
            manifest_dir.mkdir(parents=True, exist_ok=True)
            timeline_data = [
                {"line_id": "ch01_0000", "start_ms": 1000, "end_ms": 4500},
                {"line_id": "ch01_0001", "start_ms": 5000, "end_ms": 7800},
            ]
            (manifest_dir / "chapter_001.timeline.json").write_text(json.dumps(timeline_data), encoding="utf-8")

            response = self.client.get(f"/api/mobile/v1/books/{project_id}/chapters/1/lyrics")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["project_id"], project_id)
            self.assertEqual(data["chapter_number"], 1)
            self.assertEqual(data["chapter_title"], "Prologue: The Awakening")
            lines = data["lines"]
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0]["line_id"], "ch01_0000")
            self.assertEqual(lines[0]["speaker"], "Narrator")
            self.assertEqual(lines[0]["emotion"], "ominous")
            self.assertEqual(lines[0]["start_ms"], 1000)
            self.assertEqual(lines[0]["end_ms"], 4500)

            self.assertEqual(lines[1]["line_id"], "ch01_0001")
            self.assertEqual(lines[1]["speaker"], "Kaladin")
            self.assertEqual(lines[1]["start_ms"], 5000)
            self.assertEqual(lines[1]["end_ms"], 7800)
        finally:
            import shutil

            if project_dir.exists():
                shutil.rmtree(project_dir)

    def test_chapter_reader_endpoint(self):
        project_id = "test_reader_book"
        project_dir = Path("brain/projects") / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        try:
            # Create book.json with chapter text
            ch_text = "The wind howled across the crags.\n\nWe need to keep moving! The storm is coming."
            book_data = {
                "chapters": [
                    {
                        "number": 1,
                        "title": "Prologue",
                        "source_heading": "Prologue: Dawn",
                        "text": ch_text,
                    }
                ]
            }
            (project_dir / "book.json").write_text(json.dumps(book_data), encoding="utf-8")

            # Create script
            script_dir = project_dir / "script"
            script_dir.mkdir(parents=True, exist_ok=True)
            script_data = {
                "chapter_number": 1,
                "lines": [
                    {
                        "line_id": "ch01_0000",
                        "speaker": "Narrator",
                        "text": "The wind howled across the crags.",
                        "source_start": 0,
                        "source_end": 33,
                    },
                    {
                        "line_id": "ch01_0001",
                        "speaker": "Kaladin",
                        "text": "We need to keep moving! The storm is coming.",
                        "source_start": 35,
                        "source_end": 79,
                    },
                ],
            }
            (script_dir / "chapter_001.json").write_text(json.dumps(script_data), encoding="utf-8")

            # Create timeline
            manifest_dir = project_dir / "manifests"
            manifest_dir.mkdir(parents=True, exist_ok=True)
            timeline_data = [
                {"line_id": "ch01_0000", "start_ms": 1000, "end_ms": 4500},
                {"line_id": "ch01_0001", "start_ms": 5000, "end_ms": 9000},
            ]
            (manifest_dir / "chapter_001.timeline.json").write_text(json.dumps(timeline_data), encoding="utf-8")

            response = self.client.get(f"/api/mobile/v1/books/{project_id}/chapters/1/reader")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["project_id"], project_id)
            self.assertEqual(data["chapter_number"], 1)
            self.assertEqual(data["title"], "Prologue")
            self.assertEqual(data["source_heading"], "Prologue: Dawn")
            paragraphs = data["paragraphs"]
            self.assertEqual(len(paragraphs), 2)
            self.assertEqual(paragraphs[0]["index"], 0)
            self.assertEqual(paragraphs[0]["text"], "The wind howled across the crags.")
            self.assertEqual(paragraphs[0]["start_ms"], 1000)
            self.assertEqual(paragraphs[0]["end_ms"], 4500)

            self.assertEqual(paragraphs[1]["index"], 1)
            self.assertEqual(paragraphs[1]["text"], "We need to keep moving! The storm is coming.")
            self.assertEqual(paragraphs[1]["start_ms"], 5000)
            self.assertEqual(paragraphs[1]["end_ms"], 9000)
        finally:
            import shutil

            if project_dir.exists():
                shutil.rmtree(project_dir)

    def test_book_epub_download(self):
        project_id = "test_epub_book"
        project_dir = Path("brain/projects") / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        try:
            epub_file = project_dir / "source.epub"
            epub_file.write_bytes(b"PK\x03\x04fake_epub_content")

            response = self.client.get(f"/api/mobile/v1/books/{project_id}/epub")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"PK\x03\x04fake_epub_content")
            self.assertIn("application/epub+zip", response.headers["content-type"])
        finally:
            import shutil

            if project_dir.exists():
                shutil.rmtree(project_dir)
