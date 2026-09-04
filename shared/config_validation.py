"""Startup validation for user-editable YAML configuration."""

from __future__ import annotations

from datetime import datetime
from ipaddress import ip_network
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


def _number(
    errors: list[str],
    payload: dict[str, Any],
    key: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> None:
    if key not in payload:
        return
    try:
        value = float(payload[key])
    except (TypeError, ValueError):
        errors.append(f"{key} must be numeric")
        return
    if minimum is not None and value < minimum:
        errors.append(f"{key} must be >= {minimum}")
    if maximum is not None and value > maximum:
        errors.append(f"{key} must be <= {maximum}")


def validate_brain_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate brain config before clients or models are initialized."""
    errors: list[str] = []
    ollama = config.get("ollama", {})
    if not isinstance(ollama, dict):
        errors.append("ollama must be an object")
    else:
        for field in ("host", "model"):
            if field in ollama and not str(ollama[field]).strip():
                errors.append(f"ollama.{field} cannot be empty")
        fallbacks = ollama.get("fallback_models", [])
        if (
            not isinstance(fallbacks, list)
            or any(not isinstance(item, str) or not item.strip() for item in fallbacks)
        ):
            errors.append("ollama.fallback_models must be a list of non-empty model tags")
        _number(errors, ollama, "max_output_tokens", minimum=1, maximum=65536)
        _number(errors, ollama, "max_generation_seconds", minimum=1, maximum=7200)
        _number(errors, ollama, "repetition_window_chars", minimum=0, maximum=8192)
        _number(errors, ollama, "repetition_count", minimum=2, maximum=20)
        _number(errors, ollama, "num_parallel", minimum=1, maximum=16)
        backend = str(ollama.get("gpu_backend", "vulkan")).strip().lower()
        if backend not in {"vulkan", "rocm"}:
            errors.append("ollama.gpu_backend must be 'vulkan' or 'rocm'")
        kv_cache_type = str(ollama.get("kv_cache_type", "") or "").strip()
        if kv_cache_type and kv_cache_type not in {"f16", "q8_0", "q4_0"}:
            errors.append(
                "ollama.kv_cache_type must be empty, f16, q8_0, or q4_0"
            )
        if kv_cache_type in {"q8_0", "q4_0"} and not ollama.get(
            "flash_attention", True
        ):
            errors.append(
                "ollama.kv_cache_type quantization requires "
                "ollama.flash_attention: true"
            )
        think = ollama.get("think")
        if think is not None and not (
            isinstance(think, bool)
            or (isinstance(think, str) and think in {"low", "medium", "high", "max"})
        ):
            errors.append(
                "ollama.think must be boolean or one of low, medium, high, max"
            )
    voice = config.get("voice_server", {})
    host = str(voice.get("host", "http://127.0.0.1:8100"))
    parsed = urlsplit(host)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        errors.append("voice_server.host must be an absolute HTTP(S) URL")
    _number(errors, voice, "startup_timeout_seconds", minimum=1)
    _number(errors, voice, "timeout", minimum=1)
    _number(errors, voice, "retries", minimum=0, maximum=20)
    script = config.get("script", {})
    if not isinstance(script, dict):
        errors.append("script must be an object")
        script = {}
    elif (
        "joint_analysis" in script
        and not isinstance(script["joint_analysis"], bool)
    ):
        errors.append("script.joint_analysis must be boolean")
    if (
        "adaptive_split_enabled" in script
        and not isinstance(script["adaptive_split_enabled"], bool)
    ):
        errors.append("script.adaptive_split_enabled must be boolean")
    if (
        "dialogue_focused_schema" in script
        and not isinstance(script["dialogue_focused_schema"], bool)
    ):
        errors.append("script.dialogue_focused_schema must be boolean")
    _number(errors, script, "chunk_size_words", minimum=50)
    _number(errors, script, "max_fragments_per_chunk", minimum=1)
    _number(errors, script, "adaptive_split_max_depth", minimum=0, maximum=6)
    _number(errors, script, "adaptive_split_min_fragments", minimum=2)
    _number(errors, script, "speaker_confidence_threshold", minimum=0, maximum=1)
    metadata = config.get("metadata", {})
    if not isinstance(metadata, dict):
        errors.append("metadata must be an object")
    else:
        if (
            "auto_fetch_external" in metadata
            and not isinstance(metadata["auto_fetch_external"], bool)
        ):
            errors.append("metadata.auto_fetch_external must be boolean")
        _number(errors, metadata, "cache_hours", minimum=0, maximum=720)
    external = config.get("external_validation", {})
    if not isinstance(external, dict):
        errors.append("external_validation must be an object")
    else:
        for field in ("enabled",):
            if field in external and not isinstance(external[field], bool):
                errors.append(f"external_validation.{field} must be boolean")
        _number(errors, external, "auto_accept_confidence", minimum=0, maximum=1)
        _number(errors, external, "manual_review_confidence", minimum=0, maximum=1)
        _number(errors, external, "attribution_batch_size", minimum=1, maximum=100)
        _number(errors, external, "max_audio_regenerations", minimum=0, maximum=5)
        circuit = external.get("circuit_breaker", {})
        if not isinstance(circuit, dict):
            errors.append("external_validation.circuit_breaker must be an object")
        else:
            _number(errors, circuit, "failure_threshold", minimum=1, maximum=20)
            _number(errors, circuit, "cooldown_seconds", minimum=30, maximum=86400)
        api = external.get("api", {})
        browser = external.get("browser", {})
        if not isinstance(api, dict):
            errors.append("external_validation.api must be an object")
        else:
            if "enabled" in api and not isinstance(api["enabled"], bool):
                errors.append("external_validation.api.enabled must be boolean")
            _number(errors, api, "timeout_seconds", minimum=1, maximum=600)
            _number(errors, api, "max_attempts", minimum=1, maximum=10)
            for field in ("triage_model", "adjudication_model", "api_key_env"):
                if field in api and not str(api[field]).strip():
                    errors.append(f"external_validation.api.{field} cannot be empty")
            budgets = api.get("daily_request_budgets", {})
            if not isinstance(budgets, dict) or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in budgets.values()
            ):
                errors.append("external_validation.api.daily_request_budgets must contain non-negative integers")
        if not isinstance(browser, dict):
            errors.append("external_validation.browser must be an object")
        else:
            for field in ("enabled", "headless"):
                if field in browser and not isinstance(browser[field], bool):
                    errors.append(f"external_validation.browser.{field} must be boolean")
            _number(errors, browser, "timeout_seconds", minimum=1, maximum=900)
            _number(errors, browser, "max_turns_per_conversation", minimum=1, maximum=1000)
    schedule = config.get("schedule", {})
    if not isinstance(schedule, dict):
        errors.append("schedule must be an object")
        schedule = {}
    timezone_name = str(schedule.get("timezone", "UTC"))
    try:
        ZoneInfo(timezone_name)
    except Exception:
        errors.append(f"schedule.timezone is unknown: {timezone_name}")
    valid_days = {
        "Monday", "Tuesday", "Wednesday", "Thursday",
        "Friday", "Saturday", "Sunday",
    }
    windows = schedule.get("windows", [])
    if not isinstance(windows, list):
        errors.append("schedule.windows must be a list")
    else:
        for index, window in enumerate(windows, 1):
            if not isinstance(window, dict):
                errors.append(f"schedule window {index} must be an object")
                continue
            for field in ("start", "end"):
                try:
                    datetime.strptime(str(window.get(field, "")), "%H:%M")
                except ValueError:
                    errors.append(f"schedule window {index} {field} must use HH:MM")
            days = window.get("days", [])
            if (
                not isinstance(days, list)
                or not days
                or any(day not in valid_days for day in days)
            ):
                errors.append(f"schedule window {index} has invalid days")
            if window.get("start") == window.get("end"):
                errors.append(
                    f"schedule window {index} start and end must differ"
                )
    if schedule.get("enabled") and not windows:
        errors.append("enabled schedule requires at least one window")

    dashboard = config.get("dashboard", {})
    if not isinstance(dashboard, dict):
        errors.append("dashboard must be an object")
    else:
        # A malformed CIDR must fail at startup. Silently discarding it would
        # widen unauthenticated LAN access instead of narrowing it, which is
        # the opposite of what the operator intended by setting the key.
        if "trusted_lan_cidrs" in dashboard:
            cidrs = dashboard.get("trusted_lan_cidrs")
            if isinstance(cidrs, str):
                cidrs = [cidrs]
            if cidrs is None or not isinstance(cidrs, (list, tuple)):
                errors.append(
                    "dashboard.trusted_lan_cidrs must be a list of CIDR strings"
                )
            else:
                for entry in cidrs:
                    try:
                        ip_network(str(entry).strip(), strict=False)
                    except ValueError:
                        errors.append(
                            "dashboard.trusted_lan_cidrs contains an invalid "
                            f"network: {entry!r}"
                        )
        origins = dashboard.get("cors_origins")
        if origins is not None and not isinstance(origins, (list, tuple)):
            errors.append("dashboard.cors_origins must be a list")
        _number(errors, dashboard, "port", minimum=1, maximum=65535)
        _number(errors, dashboard, "max_upload_size_mb", minimum=1)
        _number(errors, dashboard, "max_epub_expanded_mb", minimum=1)

    if errors:
        raise ValueError("Invalid brain configuration: " + "; ".join(errors))
    return config


def validate_voice_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate voice config before any GPU component is constructed."""
    errors: list[str] = []
    tts = config.get("tts", {})
    validation = config.get("validation", {})
    _number(errors, tts, "sample_rate", minimum=8_000, maximum=192_000)
    _number(errors, tts, "max_text_length", minimum=100, maximum=10_000)
    post_processing = tts.get("post_processing", {})
    if not isinstance(post_processing, dict):
        errors.append("tts.post_processing must be an object")
    else:
        for field in ("enabled", "allow_phase_vocoder_fallback"):
            if field in post_processing and not isinstance(
                post_processing[field],
                bool,
            ):
                errors.append(f"tts.post_processing.{field} must be boolean")
    if tts.get("attn_implementation", "sdpa") not in {"sdpa", "eager"}:
        errors.append("tts.attn_implementation must be sdpa or eager")
    if validation.get("whisper_backend", "auto") not in {
        "auto", "faster_whisper", "openai_whisper"
    }:
        errors.append("validation.whisper_backend is invalid")
    _number(errors, validation, "wer_threshold", minimum=0, maximum=1)
    _number(errors, validation, "speaker_similarity_threshold", minimum=-1, maximum=1)
    _number(errors, validation, "max_retries", minimum=0, maximum=20)
    mastering = config.get("mastering", {})
    _number(errors, mastering, "crossfade_ms", minimum=0, maximum=500)
    _number(errors, mastering, "target_lufs", minimum=-40, maximum=-5)
    _number(errors, mastering, "peak_limit_dbfs", minimum=-20, maximum=0)
    _number(errors, config.get("storage", {}), "max_workspace_gb", minimum=0)
    if errors:
        raise ValueError("Invalid voice configuration: " + "; ".join(errors))
    return config
