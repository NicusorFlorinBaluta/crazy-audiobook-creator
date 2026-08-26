"""Integration tests for NAS API endpoints in brain/dashboard/api/main.py."""

import unittest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from brain.dashboard.api.main import app
from brain.dashboard.api.security import configured_dashboard_token


class TestNASApi(unittest.TestCase):
    """Test suite for NAS REST endpoints."""

    def setUp(self):
        self.auth_patcher = patch("brain.dashboard.api.main.dashboard_request_authorized", return_value=True)
        self.auth_patcher.start()
        self.client = TestClient(app)
        self.headers = {}

    def tearDown(self):
        self.auth_patcher.stop()

    @patch("brain.dashboard.api.main.NASSyncer")
    def test_get_nas_status(self, mock_syncer_cls):
        mock_instance = MagicMock()
        mock_instance.is_configured = True
        mock_instance.host = "192.168.50.26"
        mock_instance.shared_folder = "crazybooks"
        mock_instance.auto_sync = True
        mock_instance.prune_parts_on_full = True
        mock_syncer_cls.return_value = mock_instance

        resp = self.client.get("/api/nas/status", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("configured"))
        self.assertEqual(data.get("host"), "192.168.50.26")
        self.assertEqual(data.get("shared_folder"), "crazybooks")

    @patch("brain.dashboard.api.main.NASSyncer")
    def test_test_nas_connection(self, mock_syncer_cls):
        mock_instance = MagicMock()
        mock_instance.is_configured = True
        mock_instance.test_connection.return_value = {
            "success": True,
            "host": "192.168.50.26",
            "nas_root": "/volume1/crazybooks",
            "message": "Connected successfully",
        }
        mock_syncer_cls.return_value = mock_instance

        resp = self.client.post("/api/nas/test-connection", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("nas_root"), "/volume1/crazybooks")

    @patch("brain.dashboard.api.main.NASSyncer")
    def test_sync_all_to_nas(self, mock_syncer_cls):
        mock_instance = MagicMock()
        mock_instance.is_configured = True
        mock_instance.sync_all_projects.return_value = {
            "status": "success",
            "synced_count": 2,
            "catalog_total": 2,
        }
        mock_syncer_cls.return_value = mock_instance

        resp = self.client.post("/api/nas/sync-all", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("status"), "success")
        self.assertEqual(data.get("synced_count"), 2)


if __name__ == "__main__":
    unittest.main()
