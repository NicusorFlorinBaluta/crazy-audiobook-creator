"""Unit and integration tests for Home Assistant notification engine."""

import json
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient

from brain.orchestrator.notifier import (
    HANotifier,
    NotificationPayload,
    NotificationEventType,
)
from brain.dashboard.api.main import app


@pytest.fixture
def mock_notifier():
    return HANotifier(
        base_url="http://192.168.50.194:8123",
        api_token="test_token_xyz",
        notify_service="crazywiz_notification_group",
        dashboard_url="https://crazyha.mywire.org/audiobook/",
    )


def test_notifier_configuration(mock_notifier):
    assert mock_notifier.is_configured is True
    assert mock_notifier.base_url == "http://192.168.50.194:8123"
    assert mock_notifier.notify_service == "crazywiz_notification_group"
    assert mock_notifier.dashboard_url == "https://crazyha.mywire.org/audiobook/"


def test_notifier_not_configured():
    unconfigured = HANotifier(base_url="", api_token="", notify_service="")
    assert unconfigured.is_configured is False
    res = unconfigured.send_notification_sync(
        NotificationPayload(
            event_type=NotificationEventType.TEST,
            project_id="p1",
            project_title="Book",
            title="Title",
            message="Msg",
        )
    )
    assert res["status"] == "skipped"


@patch("urllib.request.urlopen")
def test_send_notification_success(mock_urlopen, mock_notifier):
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b'[]'
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    payload = NotificationPayload(
        event_type=NotificationEventType.TEST,
        project_id="p1",
        project_title="Test Book",
        title="Test Title",
        message="Test Message",
    )

    result = mock_notifier.send_notification_sync(payload)
    assert result["status"] == "success"
    assert result["http_status"] == 200

    # Verify request payload
    call_args = mock_urlopen.call_args[0]
    req = call_args[0]
    assert req.full_url == "http://192.168.50.194:8123/api/services/notify/crazywiz_notification_group"
    assert req.headers["Authorization"] == "Bearer test_token_xyz"
    sent_json = json.loads(req.data.decode("utf-8"))
    assert sent_json["title"] == "Test Title"
    assert sent_json["message"] == "Test Message"
    assert sent_json["data"]["url"] == "https://crazyha.mywire.org/audiobook/"


@patch("urllib.request.urlopen")
def test_notification_deduplication(mock_urlopen, mock_notifier):
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b'[]'
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    payload = NotificationPayload(
        event_type=NotificationEventType.VOICE_REVIEW_REQUIRED,
        project_id="book_dedup",
        project_title="Dedup Book",
        title="Voice Review",
        message="Review needed",
    )

    # First send succeeds
    res1 = mock_notifier.send_notification_sync(payload)
    assert res1["status"] == "success"

    # Second immediate send is suppressed
    res2 = mock_notifier.send_notification_sync(payload)
    assert res2["status"] == "suppressed"


@patch("urllib.request.urlopen")
def test_helpers(mock_urlopen, mock_notifier):
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b'[]'
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    with patch.object(mock_notifier, "notify_async") as mock_async:
        mock_notifier.notify_voice_review_required("p1", "Novel", 5)
        mock_async.assert_called_once()
        payload = mock_async.call_args[0][0]
        assert payload.event_type == NotificationEventType.VOICE_REVIEW_REQUIRED
        assert "5 characters" in payload.message

    with patch.object(mock_notifier, "notify_async") as mock_async:
        mock_notifier.notify_generation_error("p1", "Novel", "Out of memory", chapter=4)
        mock_async.assert_called_once()
        payload = mock_async.call_args[0][0]
        assert payload.event_type == NotificationEventType.GENERATION_ERROR
        assert "Chapter 4" in payload.message

    with patch.object(mock_notifier, "notify_async") as mock_async:
        mock_notifier.notify_delivery_published("p1", "Novel", "Part 01", [1, 2, 3])
        mock_async.assert_called_once()
        payload = mock_async.call_args[0][0]
        assert payload.event_type == NotificationEventType.DELIVERY_PUBLISHED
        assert "Chapters 1-3" in payload.message

    with patch.object(mock_notifier, "notify_async") as mock_async:
        mock_notifier.notify_full_book_ready("p1", "Novel", 10, 7200.0)
        mock_async.assert_called_once()
        payload = mock_async.call_args[0][0]
        assert payload.event_type == NotificationEventType.FULL_BOOK_READY
        assert "2h 0m" in payload.message


def test_api_notification_settings():
    with patch("brain.dashboard.api.main.dashboard_request_authorized", return_value=True):
        client = TestClient(app)
        response = client.get("/api/notifications/settings")
        assert response.status_code == 200
        data = response.json()
        assert "configured" in data
        assert "notify_service" in data


@patch("brain.orchestrator.notifier.HANotifier.is_configured", True)
@patch("brain.orchestrator.notifier.HANotifier.send_notification_sync")
def test_api_notification_test(mock_send):
    mock_send.return_value = {"status": "success", "http_status": 200}
    with patch("brain.dashboard.api.main.dashboard_request_authorized", return_value=True):
        client = TestClient(app)
        response = client.post("/api/notifications/test")
        assert response.status_code == 200
        assert response.json()["status"] == "success"
