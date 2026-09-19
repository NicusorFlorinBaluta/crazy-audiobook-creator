"""Unit tests for NASSyncer module."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from brain.orchestrator.nas_syncer import NASSyncer

# `paramiko` is an optional dependency: `nas_syncer` imports it defensively so
# the pipeline still runs without it, with the 24/7 NAS streaming feature
# simply unavailable. These tests patch `paramiko.SSHClient`, which needs the
# real module to be importable, so they skip rather than error when it is
# absent -- matching how the code itself treats the dependency.
#
# Deliberately not `pytest.importorskip`: the suite must also run under plain
# `python -m unittest discover`, which is how CI invokes it.
try:
    import paramiko  # noqa: F401

    HAS_PARAMIKO = True
except ImportError:  # pragma: no cover - exercised only without the optional dep
    HAS_PARAMIKO = False


@unittest.skipUnless(
    HAS_PARAMIKO,
    "optional NAS sync dependency 'paramiko' is not installed",
)
class TestNASSyncer(unittest.TestCase):
    """Test suite for NASSyncer operations."""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.project_dir = Path(self.temp_dir.name) / "test_project"
        self.project_dir.mkdir(parents=True, exist_ok=True)

        # Create dummy book.json
        (self.project_dir / "book.json").write_text(
            json.dumps(
                {
                    "metadata": {
                        "title": "Test Novel",
                        "author": "Test Author",
                        "total_chapters": 5,
                    },
                    "chapters": [
                        {"chapter_number": 1, "title": "Chapter One"},
                        {"chapter_number": 2, "title": "Chapter Two"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        # Create dummy part M4B
        self.part_m4b = self.project_dir / "Part 01 - Chapters 1-2-r1.m4b"
        self.part_m4b.write_bytes(b"dummy m4b audio content")

        self.syncer = NASSyncer(
            host="192.168.50.26",
            username="testuser",
            password="testpassword",
            shared_folder="crazybooks",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_is_configured(self):
        self.assertTrue(self.syncer.is_configured)
        unconfigured = NASSyncer(host="", username="", password="")
        self.assertFalse(unconfigured.is_configured)

    @patch("paramiko.SSHClient")
    def test_test_connection_success(self, mock_ssh_cls):
        mock_ssh = MagicMock()
        mock_sftp = MagicMock()
        mock_ssh.open_sftp.return_value = mock_sftp
        mock_ssh_cls.return_value = mock_ssh

        # Mock stat for resolve_nas_root
        mock_sftp.stat.return_value = MagicMock(st_size=100)

        result = self.syncer.test_connection()
        self.assertTrue(result["success"])
        self.assertEqual(result["host"], "192.168.50.26")
        self.assertIn("crazybooks", result["nas_root"])

    @patch("paramiko.SSHClient")
    def test_sync_delivery_part(self, mock_ssh_cls):
        mock_ssh = MagicMock()
        mock_sftp = MagicMock()
        mock_ssh.open_sftp.return_value = mock_sftp
        mock_ssh_cls.return_value = mock_ssh

        # Mock stat for file check
        mock_sftp.stat.side_effect = lambda path: MagicMock(
            st_size=len(b"dummy m4b audio content") if "tmp" in path or "m4b" in path else 0
        )
        mock_sftp.listdir.return_value = []

        result = self.syncer.sync_delivery_part(
            project_id="test_project",
            project_dir=self.project_dir,
            part_artifact_path=self.part_m4b,
        )

        self.assertEqual(result["status"], "synced")
        self.assertEqual(result["part_artifact"], self.part_m4b.name)
        mock_sftp.put.assert_called()

    @patch("paramiko.SSHClient")
    def test_sync_full_export_with_pruning(self, mock_ssh_cls):
        mock_ssh = MagicMock()
        mock_sftp = MagicMock()
        mock_ssh.open_sftp.return_value = mock_sftp
        mock_ssh_cls.return_value = mock_ssh

        full_m4b = self.project_dir / "Test Novel.m4b"
        full_m4b.write_bytes(b"full audiobook content")

        mock_sftp.stat.side_effect = lambda path: MagicMock(st_size=len(b"full audiobook content"))
        mock_sftp.listdir.side_effect = lambda path: (
            ["Part 01 - Chapters 1-2-r1.m4b", "index.json"] if "parts" in path else []
        )

        result = self.syncer.sync_full_export(
            project_id="test_project",
            project_dir=self.project_dir,
            full_m4b_path=full_m4b,
            prune_parts=True,
        )

        self.assertEqual(result["status"], "synced")
        self.assertTrue(result["parts_pruned"])
        # Verify removal of parts was called
        mock_sftp.remove.assert_called()

    @patch("paramiko.SSHClient")
    def test_delete_project_preserved_when_delete_from_nas_is_false(self, mock_ssh_cls):
        result = self.syncer.delete_project("test_project", delete_from_nas=False)
        self.assertEqual(result["status"], "preserved")
        mock_ssh_cls.assert_not_called()

    @patch("paramiko.SSHClient")
    def test_delete_project_when_delete_from_nas_is_true(self, mock_ssh_cls):
        mock_ssh = MagicMock()
        mock_sftp = MagicMock()
        mock_ssh.open_sftp.return_value = mock_sftp
        mock_ssh_cls.return_value = mock_ssh

        mock_sftp.listdir.side_effect = lambda path: ["book.json", "cover.jpg"] if "test_project" in path else []

        result = self.syncer.delete_project("test_project", delete_from_nas=True)
        self.assertEqual(result["status"], "deleted")
        mock_sftp.rmdir.assert_called()

    def test_build_project_manifest_full_m4b_cumulative_offsets(self):
        mock_sftp = MagicMock()
        # Mock full/ directory containing an M4B
        mock_sftp.listdir.side_effect = lambda path: (
            ["Test Novel.m4b"] if "full" in path else (["Part 01 - Chapters 1-3-r1.m4b"] if "parts" in path else [])
        )
        mock_sftp.stat.return_value = MagicMock(st_size=2048)

        # Update book.json to have 3 chapters
        (self.project_dir / "book.json").write_text(
            json.dumps(
                {
                    "title": "Test Book",
                    "author": "Test Author",
                    "chapters": [
                        {"chapter_number": 1, "title": "Chapter One"},
                        {"chapter_number": 2, "title": "Chapter Two"},
                        {"chapter_number": 3, "title": "Chapter Three"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        # Create dummy chapter wav files for chapters 1 and 3, leaving chapter 2 missing
        ch_dir = self.project_dir / "chapters"
        ch_dir.mkdir(parents=True, exist_ok=True)
        import wave

        for ch_num in (1, 3):
            with wave.open(str(ch_dir / f"chapter_{ch_num:03d}.wav"), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(24000)
                w.writeframes(b"\x00\x00" * 24000 * 10)  # 10s audio each

        manifest = self.syncer._generate_book_manifest(
            project_id="test_project",
            project_dir=self.project_dir,
            sftp=mock_sftp,
            proj_remote_dir="/crazybooks/test_project",
        )

        chapters = manifest["chapters"]
        self.assertEqual(len(chapters), 3)
        # Verify cumulative offsets across full M4B stream
        self.assertEqual(chapters[0]["start_ms"], 0)
        self.assertEqual(chapters[0]["end_ms"], 10000)
        # Chapter 2 has no WAV, so it falls back to 60s and cumulative_offset advances by 60s
        self.assertEqual(chapters[1]["start_ms"], 10000)
        self.assertEqual(chapters[1]["end_ms"], 70000)
        # Chapter 3 must start at 70000 ms, strictly after chapter 2
        self.assertEqual(chapters[2]["start_ms"], 70000)
        self.assertEqual(chapters[2]["end_ms"], 80000)
        self.assertTrue(chapters[0]["start_ms"] < chapters[1]["start_ms"] < chapters[2]["start_ms"])


if __name__ == "__main__":
    unittest.main()
