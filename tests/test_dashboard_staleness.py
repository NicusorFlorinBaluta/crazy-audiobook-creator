"""Tests for staleness exposure in the Dashboard API."""

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from brain.dashboard.api.main import app

LOOPBACK = ("127.0.0.1", 50000)


@pytest.fixture
def client():
    return TestClient(app, client=LOOPBACK)


def test_dashboard_status_carries_staleness(client: TestClient, tmp_path: Path):
    project_id = "test-staleness-proj"

    mock_job = {
        "project_id": project_id,
        "title": "Test Book",
        "status": "idle",
        "running": False,
    }

    with patch("brain.dashboard.api.main.job_queue") as mock_jq, \
         patch("brain.dashboard.api.main._project_dir") as mock_pdir, \
         patch("brain.dashboard.api.main._workspace_project_dir") as mock_wdir:

        proj_dir = tmp_path / "proj"
        proj_dir.mkdir(parents=True)
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir(parents=True)

        mock_jq.get_job.return_value = mock_job
        mock_pdir.return_value = proj_dir
        mock_wdir.return_value = ws_dir

        resp = client.get(f"/api/projects/{project_id}/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "staleness" in data
        staleness = data["staleness"]
        assert "deliveries_stale" in staleness
        assert "stale_deliveries" in staleness
        assert "evidence_current" in staleness
        assert "evidence_freshness" in staleness


def test_dashboard_deliveries_carries_staleness(client: TestClient, tmp_path: Path):
    project_id = "test-staleness-deliveries"
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir(parents=True)
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir(parents=True)

    with patch("brain.dashboard.api.main._require_job") as mock_req, \
         patch("brain.dashboard.api.main._project_dir", return_value=proj_dir), \
         patch("brain.dashboard.api.main._workspace_project_dir", return_value=ws_dir), \
         patch("brain.dashboard.api.main.job_queue") as mock_jq:

        mock_req.return_value = {"project_id": project_id}
        mock_jq.get_job.return_value = {"incremental_delivery": {"enabled": True, "batch_size": 5}}

        resp = client.get(f"/api/projects/{project_id}/deliveries")
        assert resp.status_code == 200
        data = resp.json()
        assert "stale_deliveries" in data
        assert "any_stale" in data
