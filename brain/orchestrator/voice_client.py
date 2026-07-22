"""Voice Server client — REST API client for the local TTS server.

Communicates with the local Voice TTS server to:
  - Bootstrap character voices
  - Generate audio segments
  - Validate audio quality
  - Master chapter audio
  - Export M4B audiobooks
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from shared.models import (
    BootstrapVoicesRequest,
    BootstrapVoicesResponse,
    ExportM4BRequest,
    ExportM4BResponse,
    GenerateChapterRequest,
    GenerateChapterResponse,
    GenerateLineRequest,
    GenerateLineResponse,
    MasterChapterRequest,
    MasterChapterResponse,
    ValidateRequest,
    QualityResult,
    VoiceHealthResponse,
)

logger = logging.getLogger(__name__)


class VoiceClient:
    """REST client for the local Voice (TTS) server."""

    def __init__(
        self,
        host: str = "http://127.0.0.1:8100",
        timeout: int = 3600,
        retries: int = 3,
        retry_delay: int = 2,
    ):
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = retry_delay
        self._client = httpx.Client(
            timeout=httpx.Timeout(float(timeout), connect=10.0),
            follow_redirects=True,
        )

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health_check(self) -> VoiceHealthResponse:
        """Check if the Voice server is running and healthy."""
        data = self._get("/health")
        return VoiceHealthResponse(**data)

    def wait_for_server(self, max_wait_seconds: int = 120) -> bool:
        """Wait for the Voice server to become available.

        Args:
            max_wait_seconds: Maximum time to wait.

        Returns:
            True if server became available, False if timed out.
        """
        start = time.time()
        while time.time() - start < max_wait_seconds:
            try:
                health = self.health_check()
                if health.status == "ok":
                    logger.info("Voice server is ready: %s", health.model_loaded)
                    return True
            except Exception:
                pass

            elapsed = int(time.time() - start)
            logger.info(
                "Waiting for Voice server at %s (%ds / %ds)...",
                self.host,
                elapsed,
                max_wait_seconds,
            )
            time.sleep(self.retry_delay)

        logger.error("Voice server did not become available within %ds", max_wait_seconds)
        return False

    # ------------------------------------------------------------------
    # Voice bootstrapping
    # ------------------------------------------------------------------

    def bootstrap_voices(self, request: BootstrapVoicesRequest) -> BootstrapVoicesResponse:
        """Generate voice reference clips for all characters."""
        logger.info(
            "Bootstrapping %d voices for project '%s'",
            len(request.characters),
            request.project_id,
        )
        data = self._post(
            "/voices/bootstrap",
            request.model_dump(),
            timeout=1200,
        )
        return BootstrapVoicesResponse(**data)

    # ------------------------------------------------------------------
    # TTS generation
    # ------------------------------------------------------------------

    def generate_line(self, request: GenerateLineRequest) -> GenerateLineResponse:
        """Generate audio for a single script line."""
        data = self._post("/generate/line", request.model_dump())
        return GenerateLineResponse(**data)

    def generate_chapter(self, request: GenerateChapterRequest) -> GenerateChapterResponse:
        """Generate audio for an entire chapter."""
        logger.info(
            "Generating chapter %d (%d lines) for project '%s'",
            request.chapter_number,
            len(request.lines),
            request.project_id,
        )
        data = self._post(
            "/generate/chapter",
            request.model_dump(),
            timeout=7200,  # 2 hours max for a chapter
        )
        return GenerateChapterResponse(**data)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_segment(self, request: ValidateRequest) -> QualityResult:
        """Validate a single audio segment."""
        data = self._post("/validate", request.model_dump())
        return QualityResult(**data)

    # ------------------------------------------------------------------
    # Mastering
    # ------------------------------------------------------------------

    def master_chapter(self, request: MasterChapterRequest) -> MasterChapterResponse:
        """Master (assemble + normalize) a chapter's audio."""
        logger.info(
            "Mastering chapter %d for project '%s'",
            request.chapter_number,
            request.project_id,
        )
        data = self._post(
            "/master/chapter",
            request.model_dump(),
            timeout=300,
        )
        return MasterChapterResponse(**data)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_m4b(self, request: ExportM4BRequest) -> ExportM4BResponse:
        """Export all chapters as a single M4B audiobook."""
        logger.info("Exporting M4B for project '%s'", request.project_id)
        data = self._post(
            "/export/m4b",
            request.model_dump(),
            timeout=600,
        )
        return ExportM4BResponse(**data)

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, timeout: int | None = None) -> dict[str, Any]:
        """Make a GET request with retry logic."""
        return self._request("GET", path, timeout=timeout)

    def _post(
        self,
        path: str,
        json_data: dict | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Make a POST request with retry logic."""
        return self._request("POST", path, json_data=json_data, timeout=timeout)

    def _request(
        self,
        method: str,
        path: str,
        json_data: dict | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Make an HTTP request with retry logic."""
        url = f"{self.host}{path}"
        effective_timeout = timeout or self.timeout
        last_error: Exception | None = None
        req_size = len(str(json_data)) if json_data else 0

        logger.info("[VoiceClient] Requesting %s %s (timeout=%ss, payload=%d bytes)", method, path, effective_timeout, req_size)

        for attempt in range(1, self.retries + 1):
            t0 = time.time()
            try:
                response = self._client.request(
                    method,
                    url,
                    json=json_data,
                    timeout=effective_timeout,
                )
                response.raise_for_status()
                elapsed = time.time() - t0
                logger.info("[VoiceClient] %s %s -> 200 OK (%.2fs)", method, path, elapsed)
                return response.json()

            except httpx.TimeoutException as e:
                last_error = e
                logger.warning(
                    "%s %s timed out (attempt %d/%d): %s",
                    method,
                    path,
                    attempt,
                    self.retries,
                    e,
                )
            except httpx.HTTPStatusError as e:
                last_error = e
                try:
                    error_details = e.response.text
                except Exception:
                    error_details = ""
                logger.warning(
                    "%s %s failed with status %d (attempt %d/%d): %s\nDetails: %s",
                    method,
                    path,
                    e.response.status_code,
                    attempt,
                    self.retries,
                    e,
                    error_details,
                )
                if e.response.status_code in (401, 403, 404, 422):
                    raise VoiceClientError(
                        f"{method} {path} failed: {e.response.status_code} {e.response.reason_phrase}\nDetails: {error_details}"
                    ) from e
            except httpx.ConnectError as e:
                last_error = e
                logger.warning(
                    "Cannot connect to Voice server at %s (attempt %d/%d): %s",
                    self.host,
                    attempt,
                    self.retries,
                    e,
                )
            except Exception as e:
                last_error = e
                logger.warning(
                    "%s %s failed (attempt %d/%d): %s",
                    method,
                    path,
                    attempt,
                    self.retries,
                    e,
                )

            if attempt < self.retries:
                time.sleep(self.retry_delay)

        raise VoiceClientError(
            f"{method} {path} failed after {self.retries} attempts: {last_error}"
        ) from last_error

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self) -> VoiceClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class VoiceClientError(Exception):
    """Raised when communication with the Voice server fails."""
