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
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field, ValidationError

from shared.artifacts import atomic_write_json
from shared.models import QualityResult, ScriptChapter
from shared.single_instance import SingleInstanceLock

logger = logging.getLogger(__name__)


def _lock_name(prefix: str, path: Path) -> str:
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}.lock"


class ExternalValidationError(RuntimeError):
    """Raised when an external validator cannot produce a trustworthy result."""


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


def _extract_json(text: str) -> dict[str, Any]:
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start : end + 1]
    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise ValueError("Gemini response was not a JSON object")
    return value


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
                "responseSchema": schema,
            },
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        response: httpx.Response | None = None
        try:
            max_attempts = max(1, int(self.config.get("max_attempts", 4)))
            for attempt in range(max_attempts):
                self.budget.reserve(model)
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
                time.sleep(min(8.0, 2.0 ** attempt))
            assert response is not None
            response.raise_for_status()
            body = response.json()
            text = "".join(
                str(part.get("text", ""))
                for part in body["candidates"][0]["content"]["parts"]
            )
            return _extract_json(text)
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ExternalValidationError(f"Gemini API request failed: {exc}") from exc


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

    def __init__(self, config: dict[str, Any], projects_dir: Path):
        self.config = config
        self.enabled = bool(config.get("enabled", False))
        self.api = GeminiApiClient(dict(config.get("api", {})), projects_dir)
        self.web = GeminiWebClient(dict(config.get("browser", {})))
        self.auto_accept = float(config.get("auto_accept_confidence", 0.9))
        self.manual_threshold = float(config.get("manual_review_confidence", 0.75))
        self.attribution_batch_size = max(1, int(config.get("attribution_batch_size", 20)))
        self.triage_model = str(config.get("api", {}).get("triage_model", "gemini-3.5-flash-lite"))
        self.adjudication_model = str(config.get("api", {}).get("adjudication_model", "gemini-3.6-flash"))

    @staticmethod
    def _attribution_schema() -> dict[str, Any]:
        return AttributionBatch.model_json_schema()

    @staticmethod
    def _audio_schema() -> dict[str, Any]:
        return AudioDecision.model_json_schema()

    def _run_attribution_stage(self, stage: str, model: str, prompt: str, project_dir: Path) -> AttributionBatch:
        raw = (
            self.web.generate_json(project_dir, "attribution", prompt)
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
                    for neighbor in chapter.lines[max(0, index - 3) : index + 4]
                ]
                cases.append({
                    "item_id": line.line_id,
                    "chapter": chapter.chapter_number,
                    "chapter_title": chapter.chapter_title,
                    "text": line.text,
                    "current_speaker": line.speaker,
                    "local_confidence": line.speaker_confidence,
                    "local_reason": line.attribution_review_reason or line.speaker_evidence,
                    "neighboring_turns": context,
                })
        candidates = character_context or {
            character_id: {"id": character_id} for character_id in sorted(character_ids)
        }
        prompt_prefix = (
            "Resolve audiobook dialogue attribution. Use only a candidate id listed below. Abstain when the "
            "source excerpt does not justify a speaker. Return one decision for every case as JSON "
            "with this exact shape: {\"decisions\":[{\"item_id\":str,\"decision\":\"resolved\"|"
            "\"abstain\",\"speaker_id\":str|null,\"confidence\":0..1,\"reason\":str,"
            "\"evidence\":str}]}. Do not infer from stereotypes.\nCANDIDATES:\n"
            + json.dumps(candidates, ensure_ascii=False)
            + "\nCASES:\n"
        )
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
            for start in range(0, len(stage_cases), self.attribution_batch_size):
                batch = stage_cases[start : start + self.attribution_batch_size]
                stage_prompt = prompt_prefix + json.dumps(batch, ensure_ascii=False)
                try:
                    result = self._run_attribution_stage(stage, model, stage_prompt, project_dir)
                except (ExternalValidationError, ValidationError) as exc:
                    logger.warning("Attribution escalation %s unavailable: %s", stage, exc)
                    trace.append({
                        "stage": stage,
                        "model": model,
                        "item_ids": [case["item_id"] for case in batch],
                        "error": str(exc),
                    })
                    for case in batch:
                        by_id[case["item_id"]].attribution_confidence_history.append({
                            "resolver": stage,
                            "model": model,
                            "decision": "unavailable",
                            "confidence": None,
                            "reason": str(exc),
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
                    valid = decision.decision == "resolved" and decision.speaker_id in character_ids
                    if valid and decision.confidence >= self.auto_accept:
                        line.speaker = str(decision.speaker_id)
                        line.speaker_confidence = decision.confidence
                        line.speaker_evidence = decision.evidence or decision.reason
                        line.attribution_resolver = stage
                        line.attribution_review_required = False
                        line.attribution_review_reason = ""
                        remaining.discard(line.line_id)
                    else:
                        line.speaker_confidence = decision.confidence
                        line.attribution_resolver = stage
                        line.attribution_review_reason = (
                            f"{stage} {decision.decision} at {decision.confidence:.0%}: {decision.reason}"
                        )
        for line_id in remaining:
            line = by_id[line_id]
            line.attribution_review_required = True
            if not line.attribution_review_reason:
                line.attribution_review_reason = "External validators were unavailable or abstained"
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
                raw = (
                    self.web.generate_json(
                        project_dir,
                        "audio_qa",
                        prompt,
                        audio_path=audio_path,
                        reference_audio_path=reference_audio_path,
                    )
                    if stage == "gemini_web"
                    else self.api.generate_json(
                        model=model,
                        prompt=prompt,
                        schema=self._audio_schema(),
                        audio_path=audio_path,
                        reference_audio_path=reference_audio_path,
                    )
                )
                decision = AudioDecision.model_validate(raw)
            except (ExternalValidationError, ValidationError) as exc:
                errors.append(f"{stage}: {exc}")
                result.external_validation_history.append({
                    "provider": stage,
                    "model": model,
                    "decision": "unavailable",
                    "confidence": None,
                    "reason": str(exc),
                })
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
