"""Confidence-aware Gemini escalation for attribution and perceptual audio QA.

Deterministic audio gates remain authoritative. Gemini may clear a soft warning,
but cannot override clipping, missing speech, transcript failure, or other hard
gates. Browser automation is optional and uses one persisted conversation per
project and validation purpose.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field

from shared.artifacts import atomic_write_json
from shared.models import QualityResult, ScriptChapter
from shared.single_instance import SingleInstanceLock

logger = logging.getLogger(__name__)

_GENERIC_ATTRIBUTION_IDS = {
    "minor_male", "minor_female", "child_male", "child_female",
    "crowd", "collective", "character_male", "character_female",
}
_EXPLICIT_IDENTITY_PATTERNS = (
    re.compile(
        r"\b(?:attributed to|spoken by|continuation of|dialogue (?:of|from))\s+"
        r"(?:the\s+)?([A-Z][\w'-]{1,})\b"
    ),
    re.compile(
        r"\b([A-Z][\w'-]{1,})(?:'s|’s)\s+(?:dialogue|speech|line|turn)\b"
    ),
    re.compile(
        r"\b([A-Z][\w'-]{1,})\s+(?:said|asked|replied|whispered|shouted|"
        r"murmured|exclaimed|noted|continued|frowned)\b"
    ),
)


def _normalized_identity(value: str) -> str:
    cleaned = re.sub(r"['’]s$", "", value.strip(), flags=re.IGNORECASE)
    return re.sub(r"[^\w]+", "_", cleaned.casefold()).strip("_")


def _attribution_identity_conflict(
    decision: AttributionDecision,
    candidates: dict[str, dict[str, Any]],
) -> str | None:
    """Reject a resolver that names one person but returns another/generic ID."""
    if decision.decision != "resolved" or not decision.speaker_id:
        return None
    text = f"{decision.reason}\n{decision.evidence}"
    claims = {
        _normalized_identity(match.group(1))
        for pattern in _EXPLICIT_IDENTITY_PATTERNS
        for match in pattern.finditer(text)
    }
    claims.discard("")
    if not claims:
        return None

    returned = str(decision.speaker_id)
    returned_context = candidates.get(returned, {})
    returned_identities = {
        _normalized_identity(returned),
        _normalized_identity(str(returned_context.get("name") or "")),
        *{
            _normalized_identity(str(alias))
            for alias in returned_context.get("aliases", [])
        },
    }
    returned_identities.discard("")
    mismatches = sorted(claims - returned_identities)
    if mismatches:
        return (
            "Resolver rationale names a different or missing character: "
            + ", ".join(mismatches)
        )
    if returned in _GENERIC_ATTRIBUTION_IDS and claims:
        # A proper named identity must never be collapsed into a generic voice,
        # even if that identity is absent from the candidate registry.
        return "Resolver mapped an explicitly named identity to a generic speaker"
    return None


def _lock_name(prefix: str, path: Path) -> str:
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}.lock"


class ExternalValidationError(RuntimeError):
    """Raised when an external validator cannot produce a trustworthy result."""


_VALIDATION_RECOVERABLE_ERRORS = (
    ExternalValidationError,
    ValueError,
    OSError,
    httpx.HTTPError,
    TimeoutError,
    json.JSONDecodeError,
    Exception,
)


class _ProviderHealth:
    """Persisted failure counter and cooldown circuit for slow external providers."""

    def __init__(self, path: Path, threshold: int = 3, cooldown_seconds: int = 900):
        self.path = path
        self.threshold = max(1, threshold)
        self.cooldown_seconds = max(30, cooldown_seconds)

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def before(self, provider: str) -> None:
        state = self._read().get(provider, {})
        open_until = float(state.get("open_until_epoch") or 0)
        if open_until > time.time():
            remaining = max(1, round(open_until - time.time()))
            raise ExternalValidationError(
                f"{provider} circuit is cooling down for {remaining}s after repeated failures"
            )

    def record(self, provider: str, *, success: bool, latency_ms: int, error: str = "") -> None:
        lock = SingleInstanceLock(_lock_name("external-health", self.path))
        if not lock.acquire():
            return
        try:
            state = self._read()
            entry = dict(state.get(provider, {}))
            now = datetime.now(UTC).isoformat()
            entry["last_latency_ms"] = latency_ms
            if success:
                entry.update({"consecutive_failures": 0, "last_success": now,
                              "last_error": "", "open_until_epoch": 0})
            else:
                failures = int(entry.get("consecutive_failures", 0)) + 1
                entry.update({"consecutive_failures": failures, "last_failure": now,
                              "last_error": error[:1000]})
                if failures >= self.threshold:
                    entry["open_until_epoch"] = time.time() + self.cooldown_seconds
            state[provider] = entry
            atomic_write_json(self.path, state)
        finally:
            lock.release()

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        state = self._read()
        for entry in state.values():
            open_until = float(entry.get("open_until_epoch") or 0)
            entry["circuit_open"] = open_until > now
            entry["cooldown_remaining_seconds"] = max(0, round(open_until - now))
        return state


class AttributionDecision(BaseModel):
    item_id: str
    decision: Literal["resolved", "abstain"]
    speaker_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(max_length=1000)
    evidence: str = Field(default="", max_length=1000)


class AttributionBatch(BaseModel):
    decisions: list[AttributionDecision]


class AudioDecision(BaseModel):
    item_id: str
    decision: Literal["accept", "reject", "abstain"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(max_length=1000)
    defects: list[str] = Field(default_factory=list)


class ExtractionDecision(BaseModel):
    item_id: str
    decision: Literal["include", "exclude", "reference", "abstain"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(max_length=1000)


class ExtractionBatch(BaseModel):
    decisions: list[ExtractionDecision]


class CharacterAugmentationDecision(BaseModel):
    character_id: str
    decision: Literal["update", "abstain"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(max_length=1000)
    evidence: list[str] = Field(default_factory=list, max_length=8)
    gender: Literal["male", "female", "other"] | None = None
    age_range: str | None = Field(default=None, max_length=100)
    voice_description: str | None = Field(default=None, max_length=1000)
    personality_traits: list[str] = Field(default_factory=list, max_length=20)
    speaking_style: str | None = Field(default=None, max_length=500)
    test_sentence: str | None = Field(default=None, max_length=500)


class CharacterAugmentationBatch(BaseModel):
    decisions: list[CharacterAugmentationDecision]


def _extract_json(text: str) -> dict[str, Any]:
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start : end + 1]
    start = candidate.find("{")
    if start < 0:
        raise ValueError("Gemini response did not contain a JSON object")
    value, _ = json.JSONDecoder().raw_decode(candidate[start:])
    if not isinstance(value, dict):
        raise ValueError("Gemini response was not a JSON object")
    return value


def _gemini_response_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline Pydantic references and remove metadata outside Gemini's subset."""
    definitions = dict(schema.get("$defs", {}))

    def convert(value: Any) -> Any:
        if isinstance(value, list):
            return [convert(item) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.rsplit("/", 1)[-1]
            target = definitions.get(name)
            if not isinstance(target, dict):
                raise ValueError(f"Unresolved response-schema reference: {reference}")
            return convert(target)
        return {
            key: convert(item)
            for key, item in value.items()
            if key not in {"$defs", "title", "default", "additionalProperties"}
        }

    converted = convert(schema)
    if not isinstance(converted, dict):
        raise ValueError("Gemini response schema must be an object")
    return converted


class _UsageBudget:
    """Small local guardrail that stays comfortably below account quotas."""

    def __init__(self, path: Path, limits: dict[str, int]):
        self.path = path
        self.limits = limits
        self.lock_path = path.with_suffix(".lock")

    def reserve(self, model: str) -> None:
        limit = int(self.limits.get(model, 0))
        if limit <= 0:
            return
        day = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
        lock = SingleInstanceLock(_lock_name("gemini-budget", self.lock_path))
        if not lock.acquire():
            raise ExternalValidationError("Gemini request budget is busy")
        try:
            state: dict[str, Any] = {}
            if self.path.is_file():
                try:
                    state = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    state = {}
            if state.get("day") != day:
                state = {"day": day, "models": {}}
            models = state.setdefault("models", {})
            used = int(models.get(model, 0))
            if used >= limit:
                raise ExternalValidationError(
                    f"Local daily safety budget exhausted for {model} ({used}/{limit})"
                )
            models[model] = used + 1
            atomic_write_json(self.path, state)
        finally:
            lock.release()


class GeminiApiClient:
    def __init__(self, config: dict[str, Any], projects_dir: Path):
        self.config = config
        self.api_key = os.getenv(str(config.get("api_key_env", "GEMINI_API_KEY")), "").strip()
        self.timeout = float(config.get("timeout_seconds", 120))
        self.request_interval = max(0.0, float(config.get("request_interval_seconds", 2.0)))
        self._last_request_time: float = 0.0
        self._request_lock = threading.Lock()
        limits = {
            str(key): int(value)
            for key, value in dict(config.get("daily_request_budgets", {})).items()
        }
        self.budget = _UsageBudget(projects_dir / ".gemini_api_usage.json", limits)

    @property
    def available(self) -> bool:
        return bool(self.config.get("enabled", True) and self.api_key)

    def generate_json(
        self,
        *,
        model: str,
        prompt: str,
        schema: dict[str, Any],
        audio_path: Path | None = None,
        reference_audio_path: Path | None = None,
    ) -> dict[str, Any]:
        if not self.available:
            raise ExternalValidationError("Gemini API is disabled or its API key is unavailable")
        parts: list[dict[str, Any]] = [{"text": prompt}]
        audio_inputs = [path for path in (audio_path, reference_audio_path) if path is not None]
        if sum(path.stat().st_size for path in audio_inputs) > 18 * 1024 * 1024:
            raise ExternalValidationError("Audio inputs are too large for inline Gemini validation")
        for index, input_path in enumerate(audio_inputs):
            parts.append({"text": "Candidate segment:" if index == 0 else "Expected speaker reference:"})
            mime = {
                ".wav": "audio/wav",
                ".mp3": "audio/mpeg",
                ".flac": "audio/flac",
                ".m4a": "audio/mp4",
                ".ogg": "audio/ogg",
            }.get(input_path.suffix.lower(), "application/octet-stream")
            parts.append(
                {
                    "inlineData": {
                        "mimeType": mime,
                        "data": base64.b64encode(input_path.read_bytes()).decode("ascii"),
                    }
                }
            )
        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": _gemini_response_schema(schema),
            },
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        response: httpx.Response | None = None
        try:
            max_attempts = max(1, int(self.config.get("max_attempts", 4)))
            for attempt in range(max_attempts):
                self.budget.reserve(model)
                if self.request_interval > 0:
                    with self._request_lock:
                        now = time.monotonic()
                        elapsed = now - self._last_request_time
                        if elapsed < self.request_interval:
                            time.sleep(self.request_interval - elapsed)
                        self._last_request_time = time.monotonic()
                response = httpx.post(
                    url,
                    headers={"x-goog-api-key": self.api_key},
                    json=payload,
                    timeout=self.timeout,
                )
                if response.status_code not in {408, 429, 500, 502, 503, 504}:
                    break
                if attempt + 1 >= max_attempts:
                    break
                retry_after = 0.0
                if response.status_code == 429:
                    raw_retry = response.headers.get("Retry-After", "")
                    try:
                        retry_after = float(raw_retry)
                    except (ValueError, TypeError):
                        retry_after = 0.0
                time.sleep(max(retry_after, min(16.0, 2.0 ** (attempt + 1))))
            assert response is not None
            response.raise_for_status()
            body = response.json()
            text = "".join(
                str(part.get("text", ""))
                for part in body["candidates"][0]["content"]["parts"]
            )
            return _extract_json(text)
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            response_detail = ""
            if response is not None and response.status_code >= 400:
                response_detail = re.sub(r"\s+", " ", response.text).strip()[:500]
            suffix = f"; response={response_detail}" if response_detail else ""
            raise ExternalValidationError(f"Gemini API request failed: {exc}{suffix}") from exc


class GeminiWebClient:
    """Optional Playwright adapter with persistent project/purpose conversations."""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    @property
    def available(self) -> bool:
        return bool(self.config.get("enabled", False))

    def generate_json(
        self,
        project_dir: Path,
        purpose: str,
        prompt: str,
        audio_path: Path | None = None,
        reference_audio_path: Path | None = None,
    ) -> dict[str, Any]:
        if not self.available:
            raise ExternalValidationError("Gemini web escalation is disabled")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ExternalValidationError("Playwright is not installed for Gemini web escalation") from exc

        state_path = project_dir / "external_validation" / "browser_state.json"
        state: dict[str, Any] = {}
        if state_path.is_file():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}
        conversations = state.setdefault("conversations", {})
        conversation = conversations.get(purpose, {})
        max_turns = max(1, int(self.config.get("max_turns_per_conversation", 100)))
        prior_turns = int(conversation.get("turns", 0) or 0)
        saved_url = (
            str(conversation.get("url", ""))
            if prior_turns < max_turns
            else ""
        )
        profile_dir = Path(str(self.config.get("profile_dir", "brain/projects/.gemini-browser-profile")))
        profile_dir.mkdir(parents=True, exist_ok=True)
        input_selector = str(
            self.config.get(
                "input_selector",
                '[contenteditable="true"][aria-label="Enter a prompt for Gemini"]',
            )
        )
        response_selector = str(self.config.get("response_selector", "message-content"))
        timeout_ms = int(float(self.config.get("timeout_seconds", 180)) * 1000)

        lock = SingleInstanceLock(_lock_name("gemini-browser", profile_dir))
        if not lock.acquire():
            raise ExternalValidationError("Gemini browser profile is already in use")
        try:
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(profile_dir),
                    channel=str(self.config.get("channel", "chrome")),
                    headless=bool(self.config.get("headless", True)),
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(saved_url or "https://gemini.google.com/app", wait_until="domcontentloaded", timeout=timeout_ms)
                    if "accounts.google." in page.url:
                        raise ExternalValidationError(
                            "Gemini browser profile is not authenticated; initialize it from the dashboard"
                        )
                    model_selector = str(
                        self.config.get(
                            "model_selector",
                            'button[aria-label*="mode picker" i]',
                        )
                    )
                    model_label = str(self.config.get("model_label", "Pro")).strip()
                    chooser = page.locator(model_selector).last
                    try:
                        chooser.wait_for(state="visible", timeout=timeout_ms)
                    except Exception as exc:
                        raise ExternalValidationError(
                            "Gemini model chooser was not found; update browser.model_selector"
                        ) from exc
                    chooser_label = " ".join(filter(None, [
                        chooser.inner_text(),
                        chooser.get_attribute("aria-label") or "",
                        chooser.get_attribute("title") or "",
                    ]))
                    if model_label.casefold() not in chooser_label.casefold():
                        chooser.click()
                        option = page.get_by_text(model_label, exact=False).last
                        option.wait_for(state="visible", timeout=timeout_ms)
                        option.click()
                    editor = page.locator(input_selector).last
                    editor.wait_for(state="visible", timeout=timeout_ms)
                    before = page.locator(response_selector).count()
                    uploads = [
                        str(path)
                        for path in (audio_path, reference_audio_path)
                        if path is not None
                    ]
                    if uploads:
                        upload = page.locator('input[type="file"]').last
                        if upload.count() == 0:
                            tools_button = page.get_by_role(
                                "button",
                                name=str(
                                    self.config.get(
                                        "upload_tools_button_name",
                                        "Upload & tools",
                                    )
                                ),
                            ).last
                            try:
                                tools_button.click(timeout=timeout_ms)
                                upload.wait_for(state="attached", timeout=timeout_ms)
                            except Exception as exc:
                                raise ExternalValidationError(
                                    "Gemini upload input was not found after opening Upload & tools"
                                ) from exc
                        upload.set_input_files(uploads)
                        page.wait_for_timeout(2000)
                        missing_uploads = [
                            Path(path).name
                            for path in uploads
                            if page.get_by_text(Path(path).name, exact=True).count() == 0
                        ]
                        if missing_uploads:
                            raise ExternalValidationError(
                                "Gemini did not attach audio files: "
                                + ", ".join(missing_uploads)
                            )
                    editor.fill(prompt)
                    editor.press("Enter")
                    page.wait_for_function(
                        "([selector, count]) => document.querySelectorAll(selector).length > count",
                        arg=[response_selector, before],
                        timeout=timeout_ms,
                    )
                    response = page.locator(response_selector).last
                    response.wait_for(state="visible", timeout=timeout_ms)
                    text = ""
                    stable_reads = 0
                    deadline = time.monotonic() + timeout_ms / 1000
                    while time.monotonic() < deadline and stable_reads < 5:
                        current = response.inner_text(timeout=timeout_ms).strip()
                        stable_reads = stable_reads + 1 if current and current == text else 0
                        text = current
                        time.sleep(1)
                    if stable_reads < 5:
                        raise ExternalValidationError("Gemini web response did not finish before timeout")
                    conversations[purpose] = {
                        "url": page.url,
                        "turns": prior_turns + 1 if saved_url else 1,
                        "updated_at": datetime.now().astimezone().isoformat(),
                    }
                    atomic_write_json(state_path, state)
                    return _extract_json(text)
                finally:
                    context.close()
        finally:
            lock.release()


class GeminiValidationService:
    """Coordinates API triage, API adjudication, and web Pro fallback."""

    def __init__(self, config: dict[str, Any], projects_dir: Path, event_sink: Any = None):
        self.config = config
        self.enabled = bool(config.get("enabled", False))
        self.api = GeminiApiClient(dict(config.get("api", {})), projects_dir)
        self.web = GeminiWebClient(dict(config.get("browser", {})))
        self.auto_accept = float(config.get("auto_accept_confidence", 0.9))
        self.manual_threshold = float(config.get("manual_review_confidence", 0.75))
        self.attribution_batch_size = max(1, int(config.get("attribution_batch_size", 20)))
        self.triage_model = str(config.get("api", {}).get("triage_model", "gemini-3.5-flash-lite"))
        self.adjudication_model = str(config.get("api", {}).get("adjudication_model", "gemini-3.5-flash"))
        character_cfg = dict(config.get("character_augmentation", {}))
        self.character_augmentation_enabled = bool(
            character_cfg.get("enabled", False)
        )
        self.character_triage_model = str(
            character_cfg.get("triage_model", self.triage_model)
        )
        self.character_adjudication_model = str(
            character_cfg.get("adjudication_model", self.adjudication_model)
        )
        circuit = dict(config.get("circuit_breaker", {}))
        self.health = _ProviderHealth(
            projects_dir / ".external_validation_health.json",
            int(circuit.get("failure_threshold", 3)),
            int(circuit.get("cooldown_seconds", 900)),
        )
        self.event_sink = event_sink

    def health_snapshot(self) -> dict[str, Any]:
        return self.health.snapshot()

    def _call_stage(self, stage: str, operation: Any) -> tuple[Any, int]:
        self.health.before(stage)
        started = time.perf_counter()
        try:
            value = operation()
        except Exception as exc:
            latency = round((time.perf_counter() - started) * 1000)
            self.health.record(stage, success=False, latency_ms=latency, error=str(exc))
            raise
        latency = round((time.perf_counter() - started) * 1000)
        self.health.record(stage, success=True, latency_ms=latency)
        return value, latency

    def _event(self, project_dir: Path, item_type: str, item_id: str, provider: str,
               model: str, decision: str, confidence: float | None, reason: str,
               latency_ms: int | None = None, details: dict[str, Any] | None = None) -> None:
        if self.event_sink is None:
            return
        event_details = dict(details or {})
        event_details.setdefault(
            "purpose_version",
            {
                "attribution": "speaker-attribution-v3",
                "segment": "audio-validation-v2",
                "extraction": "section-classification-v2",
                "character": "character-augmentation-v1",
            }.get(item_type, f"{item_type or 'unknown'}-legacy"),
        )
        try:
            self.event_sink(project_dir.name, item_type, item_id, provider, model,
                            decision, confidence, reason, latency_ms, event_details)
        except Exception:
            logger.warning("Could not append external validation event", exc_info=True)

    @staticmethod
    def _clean_review_reason(stage: str, decision: Any) -> str:
        """Format a crisp, human-readable review reason without bloating the review UI."""
        reason_text = str(getattr(decision, "reason", "") or "").strip()
        first_sentence = reason_text.split(". ")[0].rstrip(".")
        if len(first_sentence) > 200:
            first_sentence = first_sentence[:197] + "..."
        conf = getattr(decision, "confidence", None)
        conf_str = f" ({conf:.0%})" if conf is not None else ""
        return f"{stage}: {first_sentence}{conf_str}"

    @staticmethod
    def _attribution_schema() -> dict[str, Any]:
        return AttributionBatch.model_json_schema()

    @staticmethod
    def _audio_schema() -> dict[str, Any]:
        return AudioDecision.model_json_schema()

    @staticmethod
    def _extraction_schema() -> dict[str, Any]:
        return ExtractionBatch.model_json_schema()

    @staticmethod
    def _character_schema() -> dict[str, Any]:
        return CharacterAugmentationBatch.model_json_schema()

    @staticmethod
    def _character_evidence_is_grounded(
        decision: CharacterAugmentationDecision,
        dossier: dict[str, Any],
    ) -> bool:
        source = " ".join(
            str(value) for value in dossier.get("evidence_snippets", [])
        )
        normalized_source = re.sub(r"\s+", " ", source).casefold()
        for evidence in decision.evidence:
            normalized = re.sub(r"\s+", " ", evidence).strip()
            if len(normalized) >= 12 and normalized.casefold() in normalized_source:
                return True
        return False

    def augment_characters(
        self,
        *,
        project_dir: Path,
        dossier: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Return only high-confidence, source-grounded character enrichments."""
        if (
            not self.enabled
            or not self.character_augmentation_enabled
            or not dossier
        ):
            return {"accepted": {}, "review": [], "trace": []}

        remaining = set(dossier)
        accepted: dict[str, dict[str, Any]] = {}
        review: dict[str, dict[str, Any]] = {}
        trace: list[dict[str, Any]] = []
        for stage, model in (
            ("gemini_api_triage", self.character_triage_model),
            ("gemini_api_adjudication", self.character_adjudication_model),
            ("gemini_web", "Pro"),
        ):
            if not remaining:
                break
            stage_dossier = {key: dossier[key] for key in sorted(remaining)}
            prompt = (
                "Enrich an audiobook character registry using ONLY the supplied "
                "source excerpts. Abstain when identity, gender, age, personality, "
                "or vocal qualities are unsupported. Evidence entries must be "
                "verbatim substrings of the supplied evidence snippets. Never "
                "change a known male/female gender to the opposite; abstain instead. "
                "Return one decision per character. Voice descriptions may be "
                "distinctive but must not invent accents, disabilities, or biography. "
                "Return JSON matching this schema: "
                + json.dumps(self._character_schema(), ensure_ascii=False)
                + "\nDOSSIER:\n"
                + json.dumps(stage_dossier, ensure_ascii=False, indent=2)
            )
            try:
                raw, latency_ms = self._call_stage(
                    stage,
                    # Loop variables bound as defaults, not captured.
                    # `_call_stage` invokes this with no arguments and does so
                    # within this iteration, so the capture is currently
                    # harmless -- but it would silently send one stage's prompt
                    # to another the moment the call is deferred or retried out
                    # of line.
                    lambda stage=stage, model=model, prompt=prompt: (
                        self.web.generate_json(
                            project_dir,
                            "character_augmentation_v1",
                            prompt,
                        )
                        if stage == "gemini_web"
                        else self.api.generate_json(
                            model=model,
                            prompt=prompt,
                            schema=self._character_schema(),
                        )
                    ),
                )
                batch = CharacterAugmentationBatch.model_validate(raw)
            except _VALIDATION_RECOVERABLE_ERRORS as exc:
                err_summary = str(exc).split("\n")[0][:300]
                trace.append({"stage": stage, "model": model, "error": err_summary})
                continue

            for decision in batch.decisions:
                character_id = decision.character_id
                if character_id not in remaining:
                    continue
                current = dossier[character_id]
                grounded = self._character_evidence_is_grounded(decision, current)
                current_gender = str(current.get("current_gender") or "other")
                gender_conflict = bool(
                    decision.gender
                    and current_gender in {"male", "female"}
                    and decision.gender != current_gender
                )
                record = {
                    **decision.model_dump(mode="json"),
                    "provider": stage,
                    "model": model,
                    "grounded": grounded,
                    "gender_conflict": gender_conflict,
                    "latency_ms": latency_ms,
                }
                trace.append(record)
                if (
                    decision.decision == "update"
                    and decision.confidence >= self.auto_accept
                    and grounded
                    and not gender_conflict
                ):
                    accepted[character_id] = record
                    review.pop(character_id, None)
                    remaining.discard(character_id)
                else:
                    review[character_id] = record

        for character_id in sorted(remaining):
            review.setdefault(
                character_id,
                {
                    "character_id": character_id,
                    "decision": "abstain",
                    "confidence": None,
                    "reason": "No source-grounded high-confidence augmentation was available.",
                    "evidence": [],
                    "provider": "none",
                    "model": "",
                    "grounded": False,
                    "gender_conflict": False,
                },
            )
        result = {
            "accepted": accepted,
            "review": list(review.values()),
            "trace": trace,
        }
        atomic_write_json(project_dir / "character_augmentation_audit.json", result)
        return result

    def resolve_extraction_sections(
        self,
        *,
        project_dir: Path,
        sections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Classify ambiguous EPUB spine documents with bounded evidence."""
        if not self.enabled or not sections:
            return {"decisions": {}, "trace": []}
        cases = [
            {
                "item_id": str(item.get("item_id")),
                "href": str(item.get("href", "")),
                "title": str(item.get("title", "")),
                "word_count": int(item.get("word_count", 0)),
                "epub_semantics": item.get("semantics", []),
                "local_decision": item.get("decision"),
                "local_confidence": item.get("confidence"),
                "local_reason": str(item.get("reason", ""))[:300],
                "bounded_excerpt": str(item.get("classifier_excerpt", ""))[:400],
            }
            for item in sections
        ]
        base_prompt = (
            "Classify EPUB sections for audiobook extraction. 'include' means narrative text "
            "that should be spoken; 'exclude' means navigation, publishing matter, marketing, "
            "acknowledgments, or other non-narrative material; 'reference' means glossary or "
            "character/world reference material useful to analysis but not narration. Preserve "
            "prologues, epilogues, interludes, letters, poems, and narrative appendices. Abstain "
            "when evidence is insufficient. Return one JSON decision per item."
        )
        stages = [
            ("gemini_api_triage", self.triage_model),
            ("gemini_api_adjudication", self.adjudication_model),
            ("gemini_web", "Gemini web Pro"),
        ]
        remaining = {case["item_id"] for case in cases}
        accepted: dict[str, dict[str, Any]] = {}
        trace: list[dict[str, Any]] = []
        for stage, model in stages:
            if not remaining:
                break
            stage_cases = [case for case in cases if case["item_id"] in remaining]
            stage_prompt = (
                base_prompt
                + "\nCASES:\n"
                + json.dumps(stage_cases, ensure_ascii=False)
                + "\nPRIOR DECISIONS:\n"
                + json.dumps(trace, ensure_ascii=False)
            )
            try:
                raw, latency_ms = self._call_stage(
                    stage,
                    # Loop variables bound as defaults, not captured; see the
                    # character-augmentation call site for the rationale.
                    lambda stage=stage, model=model, stage_prompt=stage_prompt: (
                        self.web.generate_json(project_dir, "extraction_v1", stage_prompt)
                        if stage == "gemini_web"
                        else self.api.generate_json(
                            model=model,
                            prompt=stage_prompt,
                            schema=self._extraction_schema(),
                        )
                    ),
                )
                batch = ExtractionBatch.model_validate(raw)
            except _VALIDATION_RECOVERABLE_ERRORS as exc:
                err_summary = str(exc).split("\n")[0][:300]
                for item_id in sorted(remaining):
                    self._event(project_dir, "extraction", item_id, stage, model,
                                "unavailable", None, err_summary)
                    trace.append({
                        "item_id": item_id,
                        "provider": stage,
                        "model": model,
                        "decision": "unavailable",
                        "confidence": None,
                        "reason": err_summary,
                    })
                continue
            for decision in batch.decisions:
                if decision.item_id not in remaining:
                    continue
                record = {
                    "provider": stage,
                    "model": model,
                    "decision": decision.decision,
                    "confidence": decision.confidence,
                    "reason": decision.reason,
                }
                trace.append({"item_id": decision.item_id, **record})
                self._event(project_dir, "extraction", decision.item_id, stage, model,
                            decision.decision, decision.confidence, decision.reason,
                            latency_ms)
                if (
                    decision.decision != "abstain"
                    and decision.confidence >= self.auto_accept
                ):
                    accepted[decision.item_id] = record
                    remaining.discard(decision.item_id)
        return {"decisions": accepted, "trace": trace}

    def _run_attribution_stage(self, stage: str, model: str, prompt: str, project_dir: Path) -> AttributionBatch:
        raw = (
            self.web.generate_json(project_dir, "attribution_v2", prompt)
            if stage == "gemini_web"
            else self.api.generate_json(model=model, prompt=prompt, schema=self._attribution_schema())
        )
        return AttributionBatch.model_validate(raw)

    def resolve_attributions(
        self,
        *,
        project_dir: Path,
        chapters: list[ScriptChapter],
        character_ids: set[str],
        character_context: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        unresolved = [line for chapter in chapters for line in chapter.lines if line.attribution_review_required]
        if not self.enabled or not unresolved:
            return {"attempted": 0, "resolved": 0, "manual_review": len(unresolved)}
        by_id = {line.line_id: line for line in unresolved}
        for line in unresolved:
            if not line.attribution_confidence_history:
                line.attribution_confidence_history.append({
                    "resolver": "local",
                    "model": "script_director",
                    "decision": "resolved" if line.speaker else "abstain",
                    "speaker_id": line.speaker or None,
                    "confidence": line.speaker_confidence,
                    "reason": line.attribution_review_reason or line.speaker_evidence,
                })
        cases: list[dict[str, Any]] = []
        unresolved_ids = set(by_id)
        for chapter in chapters:
            for index, line in enumerate(chapter.lines):
                if line.line_id not in unresolved_ids:
                    continue
                context = [
                    {
                        "item_id": neighbor.line_id,
                        "text": neighbor.text,
                        "current_speaker": neighbor.speaker,
                        "confidence": neighbor.speaker_confidence,
                    }
                    for neighbor in chapter.lines[max(0, index - 10) : index + 11]
                ]
                surrounding_scene = " ".join(
                    neighbor.text for neighbor in chapter.lines[max(0, index - 8) : index + 9]
                )
                cases.append({
                    "item_id": line.line_id,
                    "chapter": chapter.chapter_number,
                    "chapter_title": chapter.chapter_title,
                    "text": line.text,
                    "attached_source_tag_or_evidence": line.speaker_evidence,
                    "current_speaker": line.speaker,
                    "local_confidence": line.speaker_confidence,
                    "local_reason": line.attribution_review_reason or line.speaker_evidence,
                    "surrounding_scene_text": surrounding_scene,
                    "neighboring_turns": context,
                })
        candidates = dict(character_context or {
            character_id: {"id": character_id} for character_id in sorted(character_ids)
        })
        generic_defaults = {
            "minor_male": {"id": "minor_male", "name": "Unnamed Man", "gender": "male", "description": "Any unnamed male speaker, man, senator, guard, soldier, trapper, technician."},
            "minor_female": {"id": "minor_female", "name": "Unnamed Woman", "gender": "female", "description": "Any unnamed female speaker, woman, passerby, technician."},
            "child_male": {"id": "child_male", "name": "Boy", "gender": "male", "description": "Any unnamed young boy or male child."},
            "child_female": {"id": "child_female", "name": "Girl", "gender": "female", "description": "Any unnamed young girl or female child."},
            "narrator": {"id": "narrator", "name": "Narrator", "gender": "neutral", "description": "The narrator. Use only for written text, signs, thoughts, or non-spoken quotations."},
        }
        for gid, gdef in generic_defaults.items():
            if gid not in candidates:
                candidates[gid] = gdef
        # Build chapter-scoped candidates map
        chapter_candidates: dict[int, dict[str, Any]] = {}
        for chapter in chapters:
            chapter_evidence = " ".join(
                part
                for line in chapter.lines
                for part in (
                    line.text,
                    line.speaker_evidence,
                    line.attribution_review_reason,
                )
                if part
            ).casefold()
            # A character who already owns another turn in this chapter must
            # remain selectable even if the unresolved quote does not repeat
            # their name. This is especially important for pronoun-only and
            # alternating dialogue scenes.
            active_ids = {
                speaker_id
                for line in chapter.lines
                for speaker_id in (line.speaker, line.voice_id)
                if speaker_id in candidates
            }
            generics = {"narrator", "minor_male", "minor_female", "child_male", "child_female", "crowd", "collective", "character_male", "character_female"}
            for cid, c in candidates.items():
                if cid in generics:
                    active_ids.add(cid)
                    continue
                c_name = str(c.get("name", "")).strip().casefold()
                if len(c_name) >= 2 and c_name in chapter_evidence:
                    active_ids.add(cid)
                    continue
                c_aliases = [
                    str(alias).strip().casefold()
                    for alias in c.get("aliases", [])
                ]
                if any(
                    len(alias) >= 3 and alias in chapter_evidence
                    for alias in c_aliases
                ):
                    active_ids.add(cid)
                    continue
                # ID parts (e.g. 'dusk' from 'sixth_of_dusk')
                id_parts = [p.casefold() for p in cid.split("_") if len(p) >= 4]
                if any(part in chapter_evidence for part in id_parts):
                    active_ids.add(cid)
                    continue
            chapter_candidates[chapter.chapter_number] = {
                cid: c for cid, c in candidates.items() if cid in active_ids
            } if len(active_ids - generics) > 0 else candidates

        remaining = set(by_id)
        trace: list[dict[str, Any]] = []
        stages = [
            ("gemini_api_triage", self.triage_model),
            ("gemini_api_adjudication", self.adjudication_model),
            ("gemini_web", "Gemini web Pro"),
        ]
        for stage, model in stages:
            if not remaining:
                break
            stage_cases = []
            for case in cases:
                if case["item_id"] not in remaining:
                    continue
                staged = dict(case)
                staged["prior_decisions"] = by_id[case["item_id"]].attribution_confidence_history
                stage_cases.append(staged)
            cases_by_chapter: dict[int, list[dict[str, Any]]] = {}
            for case in stage_cases:
                cases_by_chapter.setdefault(
                    int(case.get("chapter") or 0), []
                ).append(case)
            stage_batches = [
                chapter_cases[start : start + self.attribution_batch_size]
                for chapter_cases in cases_by_chapter.values()
                for start in range(0, len(chapter_cases), self.attribution_batch_size)
            ]
            for batch in stage_batches:
                batch_chapter = int(batch[0].get("chapter") or 0)
                batch_candidates = dict(
                    chapter_candidates.get(batch_chapter, candidates)
                )
                if not batch_candidates:
                    batch_candidates = candidates

                stage_prompt = (
                    "Resolve audiobook dialogue attribution with deep conversational grounding.\n"
                    "RULES:\n"
                    "1. Use ONLY a candidate ID listed below. For spoken dialogue in quotation marks, assign the in-story character speaking (never narrator).\n"
                    "2. TWO-PARTY CONVERSATION ALTERNATION: In scenes between two active characters without intervening speakers, untagged dialogue turns strictly alternate between Speaker A and Speaker B. Do NOT assume a continuous monologue across separate quotes unless an explicit narrative tag indicates continuation.\n"
                    "3. VOCATIVE DIRECT ADDRESS: When a quote addresses someone by name or title (e.g. '..., Dusk' or 'Remember us, worldspinner'), the speaker is the OTHER character talking TO that person, never the person addressed.\n"
                    "4. LEADING ACTION BEATS: When a quote is preceded in the same paragraph by a singular pronoun action beat (e.g. 'He nodded slowly. \"...\"'), the subject pronoun gender/identity binds to the speaker of that quote.\n"
                    "5. EXPLICIT SPEECH TAGS & PRONOUNS: When a quote has an attached speech tag in the context or evidence (e.g. 'he replied', 'she asked', '[Name] whispered'), the speaker's canonical gender and identity MUST strictly match the pronoun/name in that tag.\n"
                    "6. Return one decision for every case as JSON with shape:\n"
                    "{\"decisions\":[{\"item_id\":str,\"decision\":\"resolved\"|\"abstain\",\"speaker_id\":str|null,\"confidence\":0..1,\"reason\":str,\"evidence\":str}]}.\n\n"
                    "CANDIDATES:\n"
                    + json.dumps(batch_candidates, ensure_ascii=False, indent=2)
                    + "\n\nCASES:\n"
                    + json.dumps(batch, ensure_ascii=False, indent=2)
                )
                try:
                    result, latency_ms = self._call_stage(
                        stage,
                        # Loop variables bound as defaults, not captured; see
                        # the character-augmentation call site for why.
                        lambda stage=stage, model=model, stage_prompt=stage_prompt: (
                            self._run_attribution_stage(
                                stage, model, stage_prompt, project_dir
                            )
                        ),
                    )
                except _VALIDATION_RECOVERABLE_ERRORS as exc:
                    err_summary = str(exc).split("\n")[0][:300]
                    logger.warning("Attribution escalation %s unavailable: %s", stage, err_summary)
                    trace.append({
                        "stage": stage,
                        "model": model,
                        "item_ids": [case["item_id"] for case in batch],
                        "error": err_summary,
                    })
                    for case in batch:
                        self._event(project_dir, "attribution", case["item_id"], stage,
                                    model, "unavailable", None, err_summary)
                        by_id[case["item_id"]].attribution_confidence_history.append({
                            "resolver": stage,
                            "model": model,
                            "decision": "unavailable",
                            "confidence": None,
                            "reason": err_summary,
                        })
                    continue
                for decision in result.decisions:
                    line = by_id.get(decision.item_id)
                    if line is None or decision.item_id not in remaining:
                        continue
                    record = {
                        "resolver": stage,
                        "model": model,
                        "decision": decision.decision,
                        "speaker_id": decision.speaker_id,
                        "confidence": decision.confidence,
                        "reason": decision.reason,
                        "evidence": decision.evidence,
                    }
                    line.attribution_confidence_history.append(record)
                    trace.append({"item_id": line.line_id, **record})
                    self._event(project_dir, "attribution", line.line_id, stage, model,
                                decision.decision, decision.confidence, decision.reason,
                                latency_ms, {"speaker_id": decision.speaker_id,
                                             "evidence": decision.evidence})
                    is_quoted_dialogue = line.text.strip().startswith(('"', '“', '‘', "'"))
                    case = next(
                        (
                            item
                            for item in batch
                            if item["item_id"] == decision.item_id
                        ),
                        {},
                    )
                    allowed_ids = set(
                        chapter_candidates.get(
                            int(case.get("chapter") or 0),
                            candidates,
                        )
                    )
                    identity_conflict = _attribution_identity_conflict(
                        decision,
                        batch_candidates,
                    )
                    if is_quoted_dialogue and decision.speaker_id == "narrator":
                        valid = False
                    else:
                        valid = (
                            decision.decision == "resolved"
                            and decision.speaker_id in character_ids
                            and decision.speaker_id in allowed_ids
                            and identity_conflict is None
                        )
                    if identity_conflict:
                        line.attribution_confidence_history[-1][
                            "validation_error"
                        ] = identity_conflict
                        trace[-1]["validation_error"] = identity_conflict

                    if valid and decision.confidence >= self.auto_accept:
                        line.speaker = str(decision.speaker_id)
                        line.speaker_confidence = decision.confidence
                        line.speaker_evidence = str(decision.evidence or decision.reason or "")[:2000]
                        line.attribution_resolver = stage
                        line.attribution_review_required = False
                        line.attribution_review_reason = ""
                        remaining.discard(line.line_id)
                    else:
                        line.attribution_resolver = stage
                        if identity_conflict:
                            line.speaker_confidence = min(
                                float(line.speaker_confidence or 0.0),
                                max(0.0, self.manual_threshold - 0.01),
                            )
                            line.attribution_review_reason = str(identity_conflict).split(". ")[0].rstrip(".")
                        else:
                            line.speaker_confidence = decision.confidence
                            line.attribution_review_reason = self._clean_review_reason(stage, decision)
        for line_id in remaining:
            line = by_id[line_id]
            line.attribution_review_required = True
            if not line.attribution_review_reason:
                line.attribution_review_reason = "Unresolved speaker: confirmation required before voice synthesis"
        atomic_write_json(project_dir / "external_validation" / "attribution.json", trace)
        return {"attempted": len(unresolved), "resolved": len(unresolved) - len(remaining), "manual_review": len(remaining)}

    def validate_audio(
        self,
        *,
        project_dir: Path,
        audio_path: Path,
        line_text: str,
        result: QualityResult,
        expected_speaker: str = "",
        expected_emotion: str = "",
        reference_audio_path: Path | None = None,
    ) -> QualityResult:
        if not result.passed_hard_gates:
            local_decision = "reject"
            local_confidence = 1.0
        elif result.status.value == "pass" and not result.warnings:
            local_decision = "accept"
            local_confidence = min(1.0, 0.9 + 0.1 * float(result.quality_score))
        else:
            local_decision = "abstain"
            local_confidence = min(0.8, 0.4 + 0.4 * float(result.quality_score))
        result.validation_confidence = local_confidence
        result.external_validation_history.append({
            "provider": "local",
            "model": "deterministic_audio_validator",
            "decision": local_decision,
            "confidence": local_confidence,
            "reason": result.acceptance_reason or "; ".join(result.warnings),
        })
        if not self.enabled or (result.status.value == "pass" and not result.warnings):
            return result
        if not result.passed_hard_gates:
            result.manual_review_required = True
            result.manual_review_reason = "Deterministic audio hard gate failed; external models cannot override it"
            return result
        prompt = (
            "Evaluate this audiobook segment for audible defects, wrong/missing words, unnatural prosody, "
            "speaker inconsistency, emotion mismatch, glitches, and distracting noise. Be conservative and "
            "abstain if uncertain. Return exactly {\"item_id\":str,\"decision\":\"accept\"|\"reject\"|"
            "\"abstain\",\"confidence\":0..1,\"reason\":str,\"defects\":[str]}.\n"
            + json.dumps(
                {
                    "item_id": result.line_id,
                    "expected_text": line_text,
                    "expected_speaker": expected_speaker,
                    "expected_emotion": expected_emotion,
                    "local_metrics": result.model_dump(mode="json", exclude={"metrics"}),
                },
                ensure_ascii=False,
            )
        )
        stages = [
            ("gemini_api_triage", self.triage_model),
            ("gemini_api_adjudication", self.adjudication_model),
            ("gemini_web", "Gemini web Pro"),
        ]
        errors: list[str] = []
        for stage, model in stages:
            try:
                raw, latency_ms = self._call_stage(
                    stage,
                    # Loop variables bound as defaults, not captured; see the
                    # character-augmentation call site for why.
                    lambda stage=stage, model=model, prompt=prompt: (
                        self.web.generate_json(
                            project_dir, "audio_qa_v1", prompt, audio_path=audio_path,
                            reference_audio_path=reference_audio_path,
                        ) if stage == "gemini_web" else self.api.generate_json(
                            model=model, prompt=prompt, schema=self._audio_schema(),
                            audio_path=audio_path, reference_audio_path=reference_audio_path,
                        )
                    ),
                )
                decision = AudioDecision.model_validate(raw)
            except _VALIDATION_RECOVERABLE_ERRORS as exc:
                err_summary = str(exc).split("\n")[0][:300]
                errors.append(f"{stage}: {err_summary}")
                result.external_validation_history.append({
                    "provider": stage,
                    "model": model,
                    "decision": "unavailable",
                    "confidence": None,
                    "reason": err_summary,
                })
                self._event(project_dir, "segment", result.line_id, stage, model,
                            "unavailable", None, err_summary)
                continue
            if decision.item_id != result.line_id:
                errors.append(
                    f"{stage}: response item_id {decision.item_id!r} did not match {result.line_id!r}"
                )
                result.external_validation_history.append({
                    "provider": stage,
                    "model": model,
                    "decision": "invalid",
                    "confidence": None,
                    "reason": errors[-1],
                })
                continue
            result.external_validation_provider = stage
            result.external_validation_model = model
            result.external_validation_decision = decision.decision
            result.external_validation_confidence = decision.confidence
            result.external_validation_reason = decision.reason
            result.external_validation_history.append({
                "provider": stage,
                "model": model,
                "decision": decision.decision,
                "confidence": decision.confidence,
                "reason": decision.reason,
                "defects": decision.defects,
            })
            self._event(project_dir, "segment", result.line_id, stage, model,
                        decision.decision, decision.confidence, decision.reason,
                        latency_ms, {"defects": decision.defects})
            result.validation_confidence = decision.confidence
            if decision.confidence >= self.auto_accept and decision.decision == "accept":
                result.manual_review_required = False
                result.manual_review_reason = ""
                return result
            if decision.confidence >= self.auto_accept and decision.decision == "reject":
                result.manual_review_required = True
                result.manual_review_reason = f"External audio QA rejected this segment: {decision.reason}"
                return result
            prompt += "\nA previous validator was inconclusive: " + decision.model_dump_json()
        if (
            result.status.value in {"pass", "accepted_with_warning"}
            and result.passed_hard_gates
            and len(errors) == len(stages)
        ):
            result.manual_review_required = False
            result.manual_review_reason = ""
            logger.info(
                "[ExternalAudioQA] External triage unavailable (%s); accepting locally verified segment %s",
                "; ".join(errors),
                result.line_id,
            )
            return result
        result.manual_review_required = True
        confidence_label = (
            "low confidence"
            if (result.validation_confidence or 0.0) < self.manual_threshold
            else "below the automatic acceptance threshold"
        )
        result.manual_review_reason = (
            f"All audio fallbacks remained {confidence_label}"
            + (": " + "; ".join(errors) if errors else "")
        )
        return result
