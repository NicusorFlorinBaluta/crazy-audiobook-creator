"""Tests for Mobile API routes (/api/mobile/v1/*) and streaming."""

from __future__ import annotations

import json
import shutil
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

            # Verify data["chapters"] (individual chapter streams) always start at 0ms
            all_chaps = data.get("chapters", [])
            self.assertEqual(all_chaps[0]["start_ms"], 0)
            self.assertEqual(all_chaps[0]["end_ms"], 100000)
            self.assertEqual(all_chaps[1]["start_ms"], 0)
            self.assertEqual(all_chaps[1]["end_ms"], 200000)
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

    def test_playback_flags_crud_and_enrichment(self) -> None:
        """Verify flagging a playback issue auto-enriches context and persists to DB and disk."""
        import shutil

        project_id = "test_flags_book"
        project_dir = Path("brain/projects") / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        manifests_dir = project_dir / "manifests"
        manifests_dir.mkdir(parents=True, exist_ok=True)
        scripts_dir = project_dir / "script"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        try:
            self.job_queue.create_job(project_id, {"title": "Flag Test Book", "status": "complete"})

            # Write script
            script_data = {
                "chapter_number": 1,
                "lines": [
                    {
                        "line_id": "line_001",
                        "speaker": "narrator",
                        "text": "Wax stood in the mist.",
                        "source_start": 0,
                        "source_end": 23,
                    },
                    {
                        "line_id": "line_002",
                        "speaker": "wax",
                        "spoken_text": "Did you check the perimeter?",
                        "source_start": 24,
                        "source_end": 52,
                    },
                    {
                        "line_id": "line_003",
                        "speaker": "wayne",
                        "spoken_text": "I sure did, mate.",
                        "source_start": 53,
                        "source_end": 70,
                    },
                ],
            }
            (scripts_dir / "chapter_001.json").write_text(json.dumps(script_data), encoding="utf-8")

            # Write timeline
            timeline_data = [
                {"line_id": "line_001", "start_ms": 1000, "end_ms": 3000},
                {"line_id": "line_002", "start_ms": 3500, "end_ms": 6500},
                {"line_id": "line_003", "start_ms": 7000, "end_ms": 9500},
            ]
            (manifests_dir / "chapter_001.timeline.json").write_text(json.dumps(timeline_data), encoding="utf-8")

            # Write book.json
            book_data = {
                "chapters": [
                    {
                        "number": 1,
                        "title": "Chapter One",
                        "text": "Wax stood in the mist. Did you check the perimeter? I sure did, mate.",
                    }
                ]
            }
            (project_dir / "book.json").write_text(json.dumps(book_data), encoding="utf-8")

            # Flag an issue at 5000ms (inside line_002)
            post_resp = self.client.post(
                f"/api/mobile/v1/books/{project_id}/flags",
                json={
                    "chapter_number": 1,
                    "position_ms": 5000,
                    "issue_type": "wrong_speaker",
                    "user_note": "Sounds like Wayne instead of Wax",
                    "source": "android_auto",
                },
            )
            self.assertEqual(post_resp.status_code, 201)
            flag_res = post_resp.json()["flag"]
            self.assertEqual(flag_res["chapter_number"], 1)
            self.assertEqual(flag_res["position_ms"], 5000)
            self.assertEqual(flag_res["source"], "android_auto")
            self.assertEqual(flag_res["line_id"], "line_002")
            self.assertEqual(flag_res["status"], "open")

            # Verify auto-enrichment and reaction delay window
            enriched = flag_res["enriched_data"]
            self.assertEqual(enriched["matched_line_id"], "line_002")
            self.assertEqual(enriched["active_line"]["speaker"], "wax")
            self.assertIn("Did you check", enriched["active_line"]["text"])
            self.assertEqual(len(enriched["surrounding_lines"]), 3)
            self.assertIn("candidate_lines", enriched)
            self.assertTrue(any(c["line_id"] == "line_002" and c["is_at_tap"] for c in enriched["candidate_lines"]))
            self.assertIn("reaction_window", enriched)

            # Check GET flags list with open filter
            get_resp = self.client.get(f"/api/mobile/v1/books/{project_id}/flags?status=open")
            self.assertEqual(get_resp.status_code, 200)
            flags_list = get_resp.json()["flags"]
            self.assertEqual(len(flags_list), 1)

            # Check PATCH retargeting line_id
            flag_id = flag_res["flag_id"]
            retarget_resp = self.client.patch(
                f"/api/mobile/v1/books/{project_id}/flags/{flag_id}",
                json={"line_id": "line_003"},
            )
            self.assertEqual(retarget_resp.status_code, 200)
            retargeted = retarget_resp.json()["flag"]
            self.assertEqual(retargeted["line_id"], "line_003")
            self.assertEqual(retargeted["active_line"]["speaker"], "wayne")

            # Check PATCH update (agent veto)
            patch_resp = self.client.patch(
                f"/api/mobile/v1/books/{project_id}/flags/{flag_id}",
                json={
                    "status": "vetoed",
                    "agent_verdict": "AGENT_VETO",
                    "agent_explanation": "Manuscript text confirms Wax spoke this line.",
                    "resolution": "Vetoed: speech tag confirms speaker.",
                    "resolved_by": "agent:test",
                },
            )
            self.assertEqual(patch_resp.status_code, 200)
            updated = patch_resp.json()["flag"]
            self.assertEqual(updated["status"], "vetoed")
            self.assertEqual(updated["agent_verdict"], "AGENT_VETO")

            # Check playback_flags.json was created on disk
            flags_json_file = project_dir / "playback_flags.json"
            self.assertTrue(flags_json_file.is_file())
            saved_flags = json.loads(flags_json_file.read_text(encoding="utf-8"))
            self.assertEqual(saved_flags["total_flags"], 1)
            self.assertEqual(saved_flags["flags"][0]["status"], "vetoed")
        finally:
            if project_dir.exists():
                shutil.rmtree(project_dir)

    def test_playback_flags_duplicate_dedupe(self):
        project_id = "test_dup_book"
        project_dir = Path("brain/projects") / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.job_queue.create_job(project_id, {"title": "Dup Test", "status": "complete"})

            # Post first flag
            resp1 = self.client.post(
                f"/api/mobile/v1/books/{project_id}/flags",
                json={
                    "chapter_number": 1,
                    "position_ms": 10000,
                    "source": "android_auto",
                    "line_id": "line_010",
                },
            )
            self.assertEqual(resp1.status_code, 201)
            flag1 = resp1.json()["flag"]

            # Exact duplicate POST
            resp2 = self.client.post(
                f"/api/mobile/v1/books/{project_id}/flags",
                json={
                    "chapter_number": 1,
                    "position_ms": 10000,
                    "source": "android_auto",
                    "line_id": "line_010",
                },
            )
            self.assertEqual(resp2.status_code, 201)
            self.assertEqual(resp2.json()["result"], "existing")
            self.assertEqual(resp2.json()["status"], "flagged")
            self.assertEqual(resp2.json()["flag"]["flag_id"], flag1["flag_id"])

            # Near duplicate within 1500ms
            resp3 = self.client.post(
                f"/api/mobile/v1/books/{project_id}/flags",
                json={
                    "chapter_number": 1,
                    "position_ms": 11500,
                    "source": "android_auto",
                },
            )
            self.assertEqual(resp3.status_code, 201)
            self.assertEqual(resp3.json()["flag"]["flag_id"], flag1["flag_id"])

            # Farther flag at 2500ms apart -> separate flag
            resp4 = self.client.post(
                f"/api/mobile/v1/books/{project_id}/flags",
                json={
                    "chapter_number": 1,
                    "position_ms": 12600,
                    "source": "android_auto",
                },
            )
            self.assertEqual(resp4.status_code, 201)
            self.assertNotEqual(resp4.json()["flag"]["flag_id"], flag1["flag_id"])

            # Total flags should be 2
            flags = self.job_queue.get_playback_flags(project_id)
            self.assertEqual(len(flags), 2)

            # Mark flag1 as fixed
            self.job_queue.update_playback_flag(project_id, flag1["flag_id"], status="fixed")

            # Re-flagging the repaired line should NOT be swallowed into the fixed flag
            resp5 = self.client.post(
                f"/api/mobile/v1/books/{project_id}/flags",
                json={
                    "chapter_number": 1,
                    "position_ms": 10000,
                    "source": "android_auto",
                    "line_id": "line_010",
                },
            )
            self.assertEqual(resp5.status_code, 201)
            self.assertEqual(resp5.json()["result"], "created")
            self.assertNotEqual(resp5.json()["flag"]["flag_id"], flag1["flag_id"])

            # Total flags should now be 3
            flags_after = self.job_queue.get_playback_flags(project_id)
            self.assertEqual(len(flags_after), 3)
        finally:
            if project_dir.exists():
                shutil.rmtree(project_dir)

    def test_playback_flags_client_flag_id(self):
        project_id = "test_client_id_book"
        project_dir = Path("brain/projects") / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.job_queue.create_job(project_id, {"title": "Client ID Test", "status": "complete"})

            # Valid client_flag_id
            resp = self.client.post(
                f"/api/mobile/v1/books/{project_id}/flags",
                json={
                    "chapter_number": 2,
                    "position_ms": 5000,
                    "client_flag_id": "custom-car-tap-987",
                },
            )
            self.assertEqual(resp.status_code, 201)
            self.assertEqual(resp.json()["flag"]["flag_id"], "custom-car-tap-987")

            # Invalid client_flag_id with path traversal should be ignored and minted safely
            resp_invalid = self.client.post(
                f"/api/mobile/v1/books/{project_id}/flags",
                json={
                    "chapter_number": 2,
                    "position_ms": 20000,
                    "client_flag_id": "../../malicious/flag",
                },
            )
            self.assertEqual(resp_invalid.status_code, 201)
            self.assertNotEqual(resp_invalid.json()["flag"]["flag_id"], "../../malicious/flag")
            self.assertTrue(resp_invalid.json()["flag"]["flag_id"].startswith("flag_"))
        finally:
            if project_dir.exists():
                shutil.rmtree(project_dir)

    def test_playback_flags_import_dedupe_coercion_and_timestamp(self):
        from unittest.mock import MagicMock, patch

        project_id = "test_import_book"
        project_dir = Path("brain/projects") / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.job_queue.create_job(project_id, {"title": "Import Test", "status": "complete"})

            # Seed an existing flag
            self.job_queue.create_playback_flag(
                project_id=project_id,
                flag_id="existing_flag_001",
                chapter_number=1,
                position_ms=5000,
                line_id="ch01_0005",
            )

            # Simulated remote flags from streamer:
            # 1. Duplicate of existing within 1000ms -> should be skipped
            # 2. Remote ghost with status "flagged" and ISO timestamp -> should coerce to "open" and keep timestamp
            # 3. Remote flag with streamer schema -> should re-enrich with dashboard schema
            # 4. Remote flag with invalid timestamp -> should fall back gracefully without error
            fake_remote_flags = [
                {
                    "flag_id": "remote_dup_001",
                    "chapter_number": 1,
                    "position_ms": 5500,
                    "line_id": "ch01_0005",
                    "status": "flagged",
                },
                {
                    "flag_id": "remote_ghost_002",
                    "chapter_number": 1,
                    "position_ms": 25000,
                    "status": "flagged",
                    "created_at": "2026-09-17T05:25:20+00:00",
                    "matched_speaker": "savahn",
                    "nearby_lines": [{"speaker": "savahn", "text": "hello"}],
                },
                {
                    "flag_id": "remote_bad_ts_003",
                    "chapter_number": 1,
                    "position_ms": 50000,
                    "status": "investigating",
                    "created_at": "not-a-valid-date",
                },
            ]

            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.read.return_value = json.dumps({"flags": fake_remote_flags}).encode("utf-8")
            mock_response.__enter__.return_value = mock_response

            with patch("urllib.request.urlopen", return_value=mock_response):
                resp = self.client.get(f"/api/mobile/v1/books/{project_id}/flags")
                self.assertEqual(resp.status_code, 200)

            flags_by_id = {f["flag_id"]: f for f in self.job_queue.get_playback_flags(project_id)}

            # 1. Duplicate should not exist
            self.assertNotIn("remote_dup_001", flags_by_id)

            # 2. Ghost should be imported with status "open" and preserved created_at
            ghost = flags_by_id["remote_ghost_002"]
            self.assertEqual(ghost["status"], "open")
            self.assertEqual(ghost["created_at"], "2026-09-17T05:25:20+00:00")
            # Enriched data contains dashboard schema
            self.assertIn("active_line", ghost)
            self.assertIn("candidate_lines", ghost)

            # 3. Bad timestamp flag imported with status "investigating" and non-empty created_at
            bad_ts_flag = flags_by_id["remote_bad_ts_003"]
            self.assertEqual(bad_ts_flag["status"], "investigating")
            self.assertTrue(bad_ts_flag["created_at"])
            self.assertNotEqual(bad_ts_flag["created_at"], "not-a-valid-date")
        finally:
            if project_dir.exists():
                shutil.rmtree(project_dir)


class NarratorLookupTests(unittest.TestCase):
    """The Android book-detail call must find the narrator in a real registry.

    `characters.json` stores `characters` as a mapping of id -> entry, so
    iterating it yields the ids. The lookup did `for c in cdata["characters"]`
    and then `c.get("id")`, which raises AttributeError on a string -- on every
    book that has ever existed. The handler caught Exception, so the app simply
    received no narrator name and nothing said why.

    Found on 2026-09-05 when narrowing that handler turned the silent failure
    into an HTTP 500 on `api/mobile/v1/books/{projectId}`.
    """

    def _detail(self, characters, tmp):
        import asyncio
        from types import SimpleNamespace

        import brain.dashboard.api.mobile as mobile

        project_dir = Path(tmp) / "proj"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "characters.json").write_text(json.dumps({"characters": characters}), encoding="utf-8")
        (project_dir / "book.json").write_text(
            json.dumps({"title": "T", "chapters": [{"number": 1, "title": "One"}]}), encoding="utf-8"
        )

        class FakeQueue:
            def get_job(self, project_id):
                return {"project_id": project_id, "status": "complete", "title": "T"}

            def list_jobs(self):
                return [self.get_job("proj")]

        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(job_queue=FakeQueue())),
            client=SimpleNamespace(host="127.0.0.1"),
            headers={},
        )
        with patch.object(mobile.shared_paths, "PROJECTS_DIR", Path(tmp)):
            return asyncio.run(mobile.get_book_detail("proj", request))

    def test_narrator_is_found_when_characters_is_a_mapping(self) -> None:
        """The shape every real characters.json actually uses."""
        with tempfile.TemporaryDirectory() as tmp:
            detail = self._detail(
                {
                    "narrator": {"id": "narrator", "name": "Narrator", "speaker_name": "House Reader"},
                    "starling": {"id": "starling", "name": "Starling"},
                },
                tmp,
            )
        self.assertEqual(detail.get("narrator"), "House Reader")

    def test_narrator_is_found_when_characters_is_a_list(self) -> None:
        """The shape the original code assumed; must keep working."""
        with tempfile.TemporaryDirectory() as tmp:
            detail = self._detail(
                [{"id": "narrator", "name": "Narrator", "voice_name": "Reader"}],
                tmp,
            )
        self.assertEqual(detail.get("narrator"), "Reader")

    def test_a_registry_of_junk_does_not_fail_the_request(self) -> None:
        """Book detail must degrade, not 500, on an unexpected registry."""
        with tempfile.TemporaryDirectory() as tmp:
            detail = self._detail(["not", "a", "mapping"], tmp)
        self.assertIn("chapters", detail)


class PlaybackFlagPositionSanityTests(unittest.TestCase):
    """Tests for F7 position sanity: match distance, confidence, and book/chapter origin resolution."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.projects_dir = self.tmp_path / "projects"
        self.workspace_dir = self.tmp_path / "workspace"
        self.projects_dir.mkdir(parents=True)
        self.workspace_dir.mkdir(parents=True)

        self.project_id = "test_pos_sanity"
        self.proj_dir = self.projects_dir / self.project_id
        self.ws_dir = self.workspace_dir / self.project_id
        self.proj_dir.mkdir(parents=True)
        self.ws_dir.mkdir(parents=True)

        (self.proj_dir / "script").mkdir(parents=True)
        (self.proj_dir / "manifests").mkdir(parents=True)

        self.db_path = self.proj_dir / "pipeline_state.db"
        self.job_queue = JobQueue(db_path=str(self.db_path))
        app.state.job_queue = self.job_queue
        app.state.running_tasks = {}
        self.client = TestClient(app, client=("127.0.0.1", 50000))

        # Setup 14 chapters with real cumulative timing for the benchmark cases
        # Ch 1-12 cumulative duration: 18,195,930 ms
        chapters_meta = []
        for c in range(1, 13):
            dur_s = 18195.93 / 12.0
            chapters_meta.append({"number": c, "title": f"Chapter {c}"})
            (self.proj_dir / "manifests" / f"chapter_{c:03d}.master.json").write_text(
                json.dumps({"duration_seconds": dur_s}), encoding="utf-8"
            )

        # Ch 13 duration: 2,257.84 s (2,257,840 ms), cum: [18,195,930, 20,453,770]
        chapters_meta.append({"number": 13, "title": "Chapter 13"})
        (self.proj_dir / "manifests" / "chapter_013.master.json").write_text(
            json.dumps({"duration_seconds": 2257.84}), encoding="utf-8"
        )
        ch13_timeline = [
            {"line_id": "ch13_0001", "start_ms": 0, "end_ms": 10000},
            {"line_id": "ch13_0350", "start_ms": 1880000, "end_ms": 1885000},
            {"line_id": "ch13_0424", "start_ms": 2250000, "end_ms": 2257840},
        ]
        (self.proj_dir / "manifests" / "chapter_013.timeline.json").write_text(
            json.dumps(ch13_timeline), encoding="utf-8"
        )
        (self.proj_dir / "script" / "chapter_013.json").write_text(
            json.dumps(
                {
                    "chapter_number": 13,
                    "lines": [
                        {"line_id": "ch13_0001", "speaker": "narrator", "text": "Start of thirteen."},
                        {"line_id": "ch13_0350", "speaker": "breezy", "text": "Midpoint of thirteen."},
                        {"line_id": "ch13_0424", "speaker": "narrator", "text": "End of thirteen."},
                    ],
                }
            ),
            encoding="utf-8",
        )

        # Ch 14 duration: 1,996.20 s (1,996,200 ms), cum: [20,453,770, 22,449,970]
        chapters_meta.append({"number": 14, "title": "Chapter 14"})
        (self.proj_dir / "manifests" / "chapter_014.master.json").write_text(
            json.dumps({"duration_seconds": 1996.20}), encoding="utf-8"
        )
        ch14_timeline = [
            {"line_id": "ch14_0001", "start_ms": 0, "end_ms": 10000},
            {"line_id": "ch14_0064", "start_ms": 319830, "end_ms": 323350},
            {"line_id": "ch14_0400", "start_ms": 1990000, "end_ms": 1996200},
        ]
        (self.proj_dir / "manifests" / "chapter_014.timeline.json").write_text(
            json.dumps(ch14_timeline), encoding="utf-8"
        )
        (self.proj_dir / "script" / "chapter_014.json").write_text(
            json.dumps(
                {
                    "chapter_number": 14,
                    "lines": [
                        {"line_id": "ch14_0001", "speaker": "narrator", "text": "Start of fourteen."},
                        {"line_id": "ch14_0064", "speaker": "jarlaxle", "text": "Target of fourteen."},
                        {"line_id": "ch14_0400", "speaker": "narrator", "text": "End of fourteen."},
                    ],
                }
            ),
            encoding="utf-8",
        )

        (self.proj_dir / "book.json").write_text(
            json.dumps({"title": "Test Book", "chapters": chapters_meta}), encoding="utf-8"
        )

        self.projects_dir_patch = patch("brain.dashboard.api.mobile.shared_paths.PROJECTS_DIR", self.projects_dir)
        self.workspace_dir_patch = patch("brain.dashboard.api.mobile.shared_paths.WORKSPACE_DIR", self.workspace_dir)
        self.projects_dir_patch.start()
        self.workspace_dir_patch.start()

    def tearDown(self):
        self.projects_dir_patch.stop()
        self.workspace_dir_patch.stop()
        self.tmp_dir.cleanup()

    def test_chapter_relative_inside_line_is_exact(self):
        """Chapter-relative position inside a line -> exact, correct line_id."""
        resp = self.client.post(
            f"/api/mobile/v1/books/{self.project_id}/flags",
            json={"chapter_number": 13, "position_ms": 5000},
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()["flag"]
        enriched = data["enriched_data"]
        self.assertEqual(enriched["match_confidence"], "exact")
        self.assertEqual(enriched["match_distance_ms"], 0)
        self.assertEqual(data["line_id"], "ch13_0001")
        self.assertEqual(data["chapter_number"], 13)
        self.assertEqual(enriched["position_origin_resolved"], "chapter")

    def test_position_exceeding_chapter_resolves_to_chapter_13_inside_line(self):
        """Position 5h beyond the chapter matching a book-absolute offset inside chapter 13.
        Real numbers: chapter 13, position_ms 20,078,546, expected chapter 13.
        """
        resp = self.client.post(
            f"/api/mobile/v1/books/{self.project_id}/flags",
            json={"chapter_number": 13, "position_ms": 20078546},
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()["flag"]
        enriched = data["enriched_data"]
        self.assertEqual(enriched["position_origin_resolved"], "book")
        self.assertEqual(data["chapter_number"], 13)
        # Position 20,078,546 - 18,195,930 = 1,882,616 ms, which lands on ch13_0350 (1880000-1885000)
        self.assertEqual(data["line_id"], "ch13_0350")
        self.assertEqual(enriched["match_confidence"], "exact")
        # Must NOT have fallen back to last line of chapter 13 (ch13_0424)
        self.assertNotEqual(data["line_id"], "ch13_0424")

    def test_position_exceeding_chapter_resolves_to_chapter_14(self):
        """Position 20,774,923 with chapter_number: 13 -> resolves into chapter 14."""
        resp = self.client.post(
            f"/api/mobile/v1/books/{self.project_id}/flags",
            json={"chapter_number": 13, "position_ms": 20774923},
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()["flag"]
        enriched = data["enriched_data"]
        self.assertEqual(enriched["position_origin_resolved"], "book")
        self.assertEqual(data["chapter_number"], 14)
        # Position 20,774,923 - 20,453,770 = 321,153 ms, which lands on ch14_0064 (319830-323350)
        self.assertEqual(data["line_id"], "ch14_0064")
        self.assertEqual(enriched["match_confidence"], "exact")

    def test_position_beyond_every_interpretation_is_out_of_range_and_created(self):
        """Position beyond every interpretation -> out_of_range, and the flag is still created."""
        resp = self.client.post(
            f"/api/mobile/v1/books/{self.project_id}/flags",
            json={"chapter_number": 13, "position_ms": 999999999},
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()["flag"]
        enriched = data["enriched_data"]
        self.assertEqual(enriched["match_confidence"], "out_of_range")
        self.assertGreater(enriched["match_distance_ms"], 30000)
        self.assertEqual(data["chapter_number"], 13)

    def test_explicit_book_position_origin_skips_inference(self):
        """position_origin: 'book' supplied explicitly -> inference is skipped."""
        resp = self.client.post(
            f"/api/mobile/v1/books/{self.project_id}/flags",
            json={
                "chapter_number": 13,
                "position_ms": 20774923,
                "position_origin": "book",
            },
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()["flag"]
        enriched = data["enriched_data"]
        self.assertEqual(enriched["position_origin_resolved"], "book")
        self.assertEqual(data["chapter_number"], 14)
        self.assertEqual(data["line_id"], "ch14_0064")

    def test_flag_creation_diagnoses_narration_line_as_inconclusive(self):
        """POST a flag on a narration line -> verdict is INCONCLUSIVE and no speaker is proposed."""
        # Setup chapter 15 with purely narration lines
        ch15_timeline = [
            {"line_id": "ch15_0001", "start_ms": 0, "end_ms": 10000},
            {"line_id": "ch15_0002", "start_ms": 10000, "end_ms": 20000},
        ]
        (self.proj_dir / "manifests" / "chapter_015.timeline.json").write_text(
            json.dumps(ch15_timeline), encoding="utf-8"
        )
        (self.proj_dir / "manifests" / "chapter_015.master.json").write_text(
            json.dumps({"duration_seconds": 20.0}), encoding="utf-8"
        )
        (self.proj_dir / "script" / "chapter_015.json").write_text(
            json.dumps(
                {
                    "chapter_number": 15,
                    "lines": [
                        {
                            "line_id": "ch15_0001",
                            "speaker": "narrator",
                            "text": "The wind howled across the crags without a pause.",
                        },
                        {
                            "line_id": "ch15_0002",
                            "speaker": "narrator",
                            "text": "A cold rain began to fall in the early evening.",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        # Flag on chapter 15 line 1 (narration)
        resp = self.client.post(
            f"/api/mobile/v1/books/{self.project_id}/flags",
            json={"chapter_number": 15, "position_ms": 5000},
        )
        self.assertEqual(resp.status_code, 201)
        flag = resp.json()["flag"]
        self.assertEqual(flag["line_id"], "ch15_0001")
        self.assertEqual(flag["agent_verdict"], "INCONCLUSIVE")
        self.assertIsNotNone(flag["agent_explanation"])
        self.assertNotIn("Suggested speaker:", flag["agent_explanation"])

        # Flag on chapter 14 line 2 (ch14_0064)
        resp_diag = self.client.post(
            f"/api/mobile/v1/books/{self.project_id}/flags",
            json={"chapter_number": 14, "position_ms": 320000},
        )
        self.assertEqual(resp_diag.status_code, 201)
        flag_diag = resp_diag.json()["flag"]
        self.assertEqual(flag_diag["line_id"], "ch14_0064")
        self.assertIsNotNone(flag_diag["agent_verdict"])
        self.assertIsNotNone(flag_diag["agent_explanation"])
