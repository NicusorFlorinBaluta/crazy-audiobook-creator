"""Home Assistant Notification Engine for Crazy Audiobook Creator.

Provides asynchronous, non-blocking notifications to Home Assistant
for pipeline events (voice review, errors, delivery published, full book ready).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
import urllib.request
import urllib.error
import json

logger = logging.getLogger(__name__)


class NotificationEventType(str, Enum):
    VOICE_REVIEW_REQUIRED = "voice_review_required"
    REVIEW_REQUIRED = "review_required"
    PRONUNCIATION_ATTENTION = "pronunciation_attention"
    GENERATION_ERROR = "generation_error"
    DELIVERY_PUBLISHED = "delivery_published"
    FULL_BOOK_READY = "full_book_ready"
    PAUSED_AFTER_DELIVERY = "paused_after_delivery"
    TEST = "test"


@dataclass
class NotificationPayload:
    event_type: NotificationEventType
    project_id: str
    project_title: str
    title: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    dashboard_url: str | None = None
    importance: str = "high"  # "high", "default", "low"


class HANotifier:
    """Manages Home Assistant push notifications with rate limiting and deduplication."""

    def __init__(
        self,
        base_url: str | None = None,
        api_token: str | None = None,
        notify_service: str | None = None,
        dashboard_url: str | None = None,
    ):
        self.base_url = (base_url if base_url is not None else os.getenv("HA_BASE_URL", "http://192.168.50.194:8123")).rstrip("/")
        self.api_token = api_token if api_token is not None else os.getenv("HA_API_TOKEN", "")
        self.notify_service = (notify_service if notify_service is not None else os.getenv("HA_NOTIFY_SERVICE", "crazywiz_notification_group")).replace("notify.", "")
        self.dashboard_url = dashboard_url if dashboard_url is not None else os.getenv("DASHBOARD_PUBLIC_URL", "https://crazyha.mywire.org/audiobook/")
        
        # Deduplication cache: key -> timestamp
        self._sent_cache: dict[str, float] = {}
        self._dedup_window_seconds = 300.0  # 5 minutes

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_token and self.notify_service)

    def _should_suppress_duplicate(self, dedup_key: str) -> bool:
        now = time.time()
        last_sent = self._sent_cache.get(dedup_key, 0.0)
        if now - last_sent < self._dedup_window_seconds:
            return True
        self._sent_cache[dedup_key] = now
        # Clean expired keys
        self._sent_cache = {k: v for k, v in self._sent_cache.items() if now - v < self._dedup_window_seconds}
        return False

    def send_notification_sync(self, payload: NotificationPayload) -> dict[str, Any]:
        """Send notification synchronously via Home Assistant REST API."""
        if not self.is_configured:
            logger.debug("Home Assistant notification skipped: not configured.")
            return {"status": "skipped", "reason": "not_configured"}

        dedup_key = f"{payload.event_type.value}:{payload.project_id}:{payload.title}"
        if payload.event_type != NotificationEventType.TEST and self._should_suppress_duplicate(dedup_key):
            logger.info("Notification suppressed (duplicate within %ds): %s", self._dedup_window_seconds, dedup_key)
            return {"status": "suppressed", "reason": "duplicate"}

        endpoint = f"{self.base_url}/api/services/notify/{self.notify_service}"
        
        click_url = payload.dashboard_url or self.dashboard_url
        if click_url and not click_url.endswith("/"):
            click_url += "/"

        ha_body = {
            "title": payload.title,
            "message": payload.message,
            "data": {
                "url": click_url,
                "clickAction": click_url,
                "tag": f"crazy-{payload.event_type.value}-{payload.project_id}-{int(time.time())}",
                "group": "crazy-audiobook-creator",
                "importance": "high" if payload.importance == "high" else "default",
                "priority": "high",
                "ttl": 0,
                "channel": "Audiobook Notifications",
                "notification_icon": "mdi:book-open-page-variant",
                "visibility": "public",
            }
        }

        req = urllib.request.Request(
            endpoint,
            data=json.dumps(ha_body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status_code = resp.status
                body = resp.read().decode("utf-8")
                logger.info("Home Assistant notification sent: %s (%s)", payload.title, payload.event_type.value)
                return {"status": "success", "http_status": status_code, "response": body}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            logger.error("Failed to send Home Assistant notification: HTTP %d - %s", e.code, err_body)
            return {"status": "error", "http_status": e.code, "error": err_body}
        except Exception as e:
            logger.error("Failed to send Home Assistant notification: %s", e)
            return {"status": "error", "error": str(e)}

    def notify_async(self, payload: NotificationPayload) -> None:
        """Non-blocking fire-and-forget notification delivery."""
        import threading
        thread = threading.Thread(target=self.send_notification_sync, args=(payload,), daemon=True)
        thread.start()

    # --- Convenience Helper Methods ---

    def notify_review_required(
        self,
        project_id: str,
        project_title: str,
        reason: str | None = None,
        item_count: int = 0,
        item_ids: list[str] | None = None,
    ) -> None:
        reason_msg = reason or (f"{item_count} item(s) require review before proceeding." if item_count > 0 else "Action required.")
        title = f"🔍 Review Required: {project_title}" if item_count > 0 else f"🎭 Action Required: {project_title}"
        payload = NotificationPayload(
            event_type=NotificationEventType.REVIEW_REQUIRED,
            project_id=project_id,
            project_title=project_title,
            title=title,
            message=f"{reason_msg} Tap to open the creator dashboard.",
            details={"item_count": item_count, "item_ids": item_ids or [], "reason": reason_msg},
            importance="high",
        )
        self.notify_async(payload)

    def notify_voice_review_required(self, project_id: str, project_title: str, character_count: int = 0) -> None:
        payload = NotificationPayload(
            event_type=NotificationEventType.VOICE_REVIEW_REQUIRED,
            project_id=project_id,
            project_title=project_title,
            title=f"🎭 Voice Approval Needed: {project_title}",
            message=f"Speaking cast review is ready for '{project_title}' ({character_count} characters). Approve the voice assignments to begin audio generation.",
            details={"character_count": character_count},
            importance="high",
        )
        self.notify_async(payload)

    def notify_generation_error(self, project_id: str, project_title: str, error_message: str, chapter: int | None = None) -> None:
        ch_str = f" in Chapter {chapter}" if chapter else ""
        payload = NotificationPayload(
            event_type=NotificationEventType.GENERATION_ERROR,
            project_id=project_id,
            project_title=project_title,
            title=f"⚠️ Pipeline Error: {project_title}",
            message=f"Audio generation stopped{ch_str}: {error_message[:200]}",
            details={"error": error_message, "chapter": chapter},
            importance="high",
        )
        self.notify_async(payload)

    def notify_delivery_published(self, project_id: str, project_title: str, part_title: str, chapters: list[int]) -> None:
        ch_list = f"Chapters {chapters[0]}-{chapters[-1]}" if len(chapters) > 1 else f"Chapter {chapters[0]}" if chapters else ""
        payload = NotificationPayload(
            event_type=NotificationEventType.DELIVERY_PUBLISHED,
            project_id=project_id,
            project_title=project_title,
            title=f"🎧 New Part Ready: {project_title}",
            message=f"{part_title} ({ch_list}) is mastered and synced to your NAS! You can stream it now on your phone.",
            details={"part_title": part_title, "chapters": chapters},
            importance="default",
        )
        self.notify_async(payload)

    def notify_full_book_ready(self, project_id: str, project_title: str, total_chapters: int, total_duration_seconds: float) -> None:
        hours = int(total_duration_seconds // 3600)
        minutes = int((total_duration_seconds % 3600) // 60)
        dur_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
        payload = NotificationPayload(
            event_type=NotificationEventType.FULL_BOOK_READY,
            project_id=project_id,
            project_title=project_title,
            title=f"🎉 Audiobook Complete: {project_title}",
            message=f"The full audiobook '{project_title}' ({total_chapters} chapters, {dur_str}) is finished, mastered, and ready on your NAS!",
            details={"total_chapters": total_chapters, "duration_seconds": total_duration_seconds},
            importance="default",
        )
        self.notify_async(payload)

    def notify_paused_after_delivery(self, project_id: str, project_title: str, completed_part: str) -> None:
        payload = NotificationPayload(
            event_type=NotificationEventType.PAUSED_AFTER_DELIVERY,
            project_id=project_id,
            project_title=project_title,
            title=f"⏸️ Batch Paused: {project_title}",
            message=f"Completed {completed_part}. Pipeline paused as scheduled. Resume in the dashboard whenever you are ready.",
            details={"completed_part": completed_part},
            importance="default",
        )
        self.notify_async(payload)


# Global singleton instance
notifier = HANotifier()
