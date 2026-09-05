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


if __name__ == "__main__":
    unittest.main()
