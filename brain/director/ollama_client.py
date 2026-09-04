"""Ollama API client — Wrapper for communicating with the local Ollama LLM server.

Handles:
  - HTTP communication with the Ollama REST API
  - Structured JSON output extraction and validation
  - Retry logic with exponential backoff
  - Timeout management for long-running LLM calls
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any

import httpx

from shared.constants import (
    DEFAULT_OLLAMA_HOST,
    DEFAULT_OLLAMA_MODEL,
    GenerationCancelled,
)

logger = logging.getLogger(__name__)

# How often to emit a liveness log line, in received stream chunks.
LOG_INTERVAL_CHUNKS = 200
# How often to test the response tail for a repetition loop. Deliberately
# independent of LOG_INTERVAL_CHUNKS: this is a quality gate, not diagnostics,
# and changing the log cadence must not change how quickly a loop is caught.
REPETITION_CHECK_INTERVAL_CHUNKS = 100


class OllamaClient:
    """Client for the Ollama LLM API."""

    def __init__(
        self,
        host: str = DEFAULT_OLLAMA_HOST,
        model: str = DEFAULT_OLLAMA_MODEL,
        fallback_models: list[str] | tuple[str, ...] | None = None,
        timeout: int = 120,
        max_retries: int = 3,
        max_retry_seconds: int = 900,
        context_window: int = 8192,
        max_output_tokens: int = 8192,
        max_generation_seconds: int = 900,
        repetition_window_chars: int = 512,
        repetition_count: int = 4,
        think: bool | str | None = None,
    ):
        self.host = host.rstrip("/")
        self.model = model
        self.fallback_models = tuple(fallback_models or ())
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.max_retry_seconds = max(0, max_retry_seconds)
        self.context_window = context_window
        self.max_output_tokens = max(1, max_output_tokens)
        self.max_generation_seconds = max(1, max_generation_seconds)
        self.repetition_window_chars = max(0, repetition_window_chars)
        self.repetition_count = max(2, repetition_count)
        self.think = think
        self._client_lock = threading.Lock()
        self._client = self._new_client()
        self._cancel_event = threading.Event()
        self.last_generation_metrics: dict[str, Any] = {}

    def _new_client(self) -> httpx.Client:
        return httpx.Client(timeout=httpx.Timeout(self.timeout, connect=10.0, read=self.timeout))

    def _ensure_client(self) -> httpx.Client:
        with self._client_lock:
            if getattr(self._client, "is_closed", False):
                self._client = self._new_client()
            return self._client

    def begin_run(self) -> None:
        """Clear a previous cooperative cancellation before a new pipeline run."""
        self._ensure_client()
        self._cancel_event.clear()

    def cancel_current(self, *, force: bool = False) -> None:
        """Interrupt an active stream, optionally closing its socket now."""
        self._cancel_event.set()
        if force:
            with self._client_lock:
                try:
                    self._client.close()
                except Exception:
                    pass
        logger.info("[Ollama] Cancellation requested")

    def _raise_if_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise GenerationCancelled("Ollama generation cancelled")

    def _wait_for_retry(self, seconds: int) -> None:
        """Wait between retries while remaining immediately cancellable."""
        if self._cancel_event.wait(seconds):
            self._raise_if_cancelled()

    def generate(
        self,
        prompt: str,
        *,
        temperature: float = 0.4,
        top_p: float = 0.9,
        system: str | None = None,
        format: str | None = None,
    ) -> str:
        """Generate a text completion from the LLM.

        Args:
            prompt: The user prompt to send.
            temperature: Sampling temperature (lower = more deterministic).
            top_p: Nucleus sampling parameter.
            system: Optional system prompt.
            format: Optional response format (e.g., 'json').

        Returns:
            The generated text response.

        Raises:
            OllamaError: If the request fails after all retries.
        """
        # Never let a failed request inherit diagnostics from the preceding
        # successful request.
        self.last_generation_metrics = {}
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": temperature,
                "top_p": top_p,
                # Never allow a malformed or looping response to generate
                # indefinitely. This is also enforced on the client side in
                # case a server version ignores ``num_predict``.
                "num_predict": self.max_output_tokens,
                "num_ctx": self.context_window,
                "num_gpu": 99,
            },
        }
        if format:
            payload["format"] = format
        if self.think is not None:
            payload["think"] = self.think

        last_error: Exception | None = None
        retry_started = time.monotonic()

        for attempt in range(1, self.max_retries + 1):
            self._raise_if_cancelled()
            retry_elapsed = time.monotonic() - retry_started
            if attempt > 1 and self.max_retry_seconds and retry_elapsed >= self.max_retry_seconds:
                logger.error(
                    "[Ollama] Retry budget exhausted after %.1fs (%d attempt%s)",
                    retry_elapsed,
                    attempt - 1,
                    "" if attempt == 2 else "s",
                )
                break
            try:
                prompt_kb = sum(len(m["content"]) for m in messages) / 1024
                logger.info(
                    "[Ollama] → Sending request (attempt %d/%d) | model=%s | prompt=%.1f KB | temp=%.2f",
                    attempt,
                    self.max_retries,
                    self.model,
                    prompt_kb,
                    temperature,
                )

                t0 = time.time()
                started_monotonic = time.monotonic()
                full_text = []
                token_count = 0
                last_log_tokens = 0
                last_repetition_check = 0
                final_chunk: dict[str, Any] = {}

                client = self._ensure_client()
                with client.stream(
                    "POST",
                    f"{self.host}/api/chat",
                    json=payload,
                    # ``timeout`` is an inactivity watchdog, not a total
                    # generation limit. Each received chunk resets it.
                    timeout=httpx.Timeout(
                        self.timeout,
                        connect=60.0,
                        read=self.timeout,
                    ),
                ) as response:
                    if response.status_code == 404:
                        resolved = self._resolve_configured_fallback()
                        if resolved and resolved != self.model:
                            logger.warning(
                                "[Ollama] Model '%s' returned 404. Switching to explicitly configured fallback '%s'",
                                payload["model"],
                                resolved,
                            )
                            self.model = resolved
                            payload["model"] = resolved
                            continue
                        raise OllamaError(
                            f"Configured Ollama model '{self.model}' is unavailable "
                            "and no explicitly configured fallback is installed"
                        )
                    response.raise_for_status()
                    logger.info("[Ollama] ← HTTP 200 received, streaming tokens...")

                    for line in response.iter_lines():
                        self._raise_if_cancelled()
                        if line:
                            chunk = json.loads(line)
                            if chunk.get("done"):
                                final_chunk = chunk
                            if "message" in chunk and "content" in chunk["message"]:
                                full_text.append(chunk["message"]["content"])
                                token_count += 1
                                generation_elapsed = time.monotonic() - started_monotonic
                                if generation_elapsed >= self.max_generation_seconds:
                                    self._record_generation_abort(
                                        attempt=attempt,
                                        messages=messages,
                                        response_parts=full_text,
                                        token_count=token_count,
                                        elapsed=generation_elapsed,
                                        reason="wall_clock_limit",
                                    )
                                    raise OllamaGenerationLimitError(
                                        "Ollama generation exceeded the configured "
                                        f"{self.max_generation_seconds}s wall-clock limit"
                                    )
                                if token_count >= self.max_output_tokens and not chunk.get("done"):
                                    self._record_generation_abort(
                                        attempt=attempt,
                                        messages=messages,
                                        response_parts=full_text,
                                        token_count=token_count,
                                        elapsed=generation_elapsed,
                                        reason="output_token_limit",
                                    )
                                    raise OllamaGenerationLimitError(
                                        "Ollama generation reached the configured "
                                        f"{self.max_output_tokens}-token output limit"
                                    )
                                # Liveness logging.
                                if token_count - last_log_tokens >= LOG_INTERVAL_CHUNKS:
                                    elapsed = time.time() - t0
                                    logger.info(
                                        "[Ollama] ↻ Streaming... %d chunks | %.1f chunk/s | %.0fs elapsed",
                                        token_count,
                                        token_count / elapsed if elapsed > 0 else 0,
                                        elapsed,
                                    )
                                    last_log_tokens = token_count

                                # Repetition detection is a quality gate and
                                # must not inherit its sensitivity from the log
                                # interval, which is what happened while this
                                # check lived inside the logging branch above.
                                if token_count - last_repetition_check >= REPETITION_CHECK_INTERVAL_CHUNKS:
                                    last_repetition_check = token_count
                                    if self._has_repeated_tail(full_text):
                                        self._record_generation_abort(
                                            attempt=attempt,
                                            messages=messages,
                                            response_parts=full_text,
                                            token_count=token_count,
                                            elapsed=generation_elapsed,
                                            reason="repetition_loop",
                                        )
                                        raise OllamaGenerationLimitError(
                                            "Ollama generation entered a repeated-output loop"
                                        )

                text = "".join(full_text)
                elapsed = time.time() - t0

                if final_chunk.get("done_reason") == "length":
                    self._record_generation_abort(
                        attempt=attempt,
                        messages=messages,
                        response_parts=full_text,
                        token_count=token_count,
                        elapsed=elapsed,
                        reason="server_output_limit",
                    )
                    raise OllamaGenerationLimitError(
                        "Ollama stopped at the configured output limit before completing the response"
                    )

                if not text.strip():
                    # No debug artifact is written here: by definition of this
                    # branch the response is empty, so the file only ever
                    # contained "". The log line below carries the useful
                    # signal (chunk count and elapsed time).
                    logger.error(
                        "[Ollama] ✗ Empty response after streaming! %d token chunks received in %.1fs",
                        token_count,
                        elapsed,
                    )
                    raise OllamaError("Empty response from Ollama")

                logger.info(
                    "[Ollama] ✓ Complete: %d tokens | %d chars | %.1fs | ~%.0f tok/s | preview: %r",
                    token_count,
                    len(text),
                    elapsed,
                    token_count / elapsed if elapsed > 0 else 0,
                    text[:120].replace("\n", " "),
                )
                self.last_generation_metrics = {
                    "attempt": attempt,
                    "prompt_characters": sum(len(m["content"]) for m in messages),
                    "response_characters": len(text),
                    "stream_chunks": token_count,
                    "elapsed_seconds": round(elapsed, 6),
                    "chunks_per_second": round(token_count / elapsed if elapsed > 0 else 0.0, 6),
                    "prompt_eval_count": final_chunk.get("prompt_eval_count"),
                    "eval_count": final_chunk.get("eval_count"),
                    "prompt_eval_duration_ns": final_chunk.get("prompt_eval_duration"),
                    "eval_duration_ns": final_chunk.get("eval_duration"),
                    "load_duration_ns": final_chunk.get("load_duration"),
                }
                return text

            except httpx.TimeoutException as e:
                last_error = e
                logger.warning(
                    "[Ollama] ✗ Request TIMED OUT (attempt %d/%d, timeout=%ds): %s",
                    attempt,
                    self.max_retries,
                    self.timeout,
                    e,
                )
            except httpx.HTTPStatusError as e:
                last_error = e
                err_text = ""
                try:
                    err_text = e.response.text
                except Exception:
                    pass
                logger.warning(
                    "[Ollama] ✗ HTTP error (attempt %d/%d): %s — Body: %s",
                    attempt,
                    self.max_retries,
                    e,
                    err_text,
                )
            except OllamaError:
                raise
            except Exception as e:
                self._raise_if_cancelled()
                last_error = e
                logger.warning(
                    "[Ollama] ✗ Unexpected error (attempt %d/%d): %s: %s",
                    attempt,
                    self.max_retries,
                    type(e).__name__,
                    e,
                )

            if attempt < self.max_retries:
                wait = min(30, 2**attempt)
                if self.max_retry_seconds:
                    remaining = self.max_retry_seconds - (time.monotonic() - retry_started)
                    if remaining <= 0:
                        logger.error("[Ollama] Retry budget exhausted; not starting another request")
                        break
                    wait = min(wait, max(0, int(remaining)))
                logger.info("[Ollama] Retrying in %d seconds...", wait)
                self._wait_for_retry(wait)

        raise OllamaError(f"Failed after bounded retries: {last_error}") from last_error

    def _has_repeated_tail(self, response_parts: list[str]) -> bool:
        """Detect exact periodic output at the tail, regardless of loop length."""
        max_period = self.repetition_window_chars
        repeats = self.repetition_count
        if not max_period:
            return False
        text = "".join(response_parts)
        max_period = min(max_period, len(text) // repeats)
        if max_period <= 0:
            return False
        # Very small periods are still caught by a 32-character block made of
        # that pattern, while avoiding noisy single-character comparisons.
        min_period = min(32, max_period)
        for period in range(min_period, max_period + 1):
            required = period * repeats
            tail = text[-required:]
            block = tail[-period:]
            if block.strip() and all(tail[offset : offset + period] == block for offset in range(0, required, period)):
                return True
        return False

    def _record_generation_abort(
        self,
        *,
        attempt: int,
        messages: list[dict[str, str]],
        response_parts: list[str],
        token_count: int,
        elapsed: float,
        reason: str,
    ) -> None:
        """Record bounded, text-free diagnostics for an aborted generation."""
        response_characters = sum(len(part) for part in response_parts)
        self.last_generation_metrics = {
            "attempt": attempt,
            "prompt_characters": sum(len(message["content"]) for message in messages),
            "response_characters": response_characters,
            "stream_chunks": token_count,
            "elapsed_seconds": round(elapsed, 6),
            "termination_reason": reason,
        }
        logger.error(
            "[Ollama] Generation aborted safely: reason=%s chunks=%d chars=%d elapsed=%.1fs",
            reason,
            token_count,
            response_characters,
            elapsed,
        )

    def generate_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.4,
        top_p: float = 0.9,
        system: str | None = None,
    ) -> dict[str, Any]:
        """Generate a structured JSON response from the LLM.

        Extracts JSON from the response text, handling cases where the LLM
        wraps it in markdown code fences or adds explanatory text.

        Args:
            prompt: The user prompt (should request JSON output).
            temperature: Sampling temperature.
            top_p: Nucleus sampling parameter.
            system: Optional system prompt.

        Returns:
            Parsed JSON dict.

        Raises:
            OllamaError: If the response can't be parsed as JSON.
        """
        raw = self.generate(
            prompt,
            temperature=temperature,
            top_p=top_p,
            system=system,
            format="json",
        )

        return self._extract_json(raw)

    def unload_model(self, model: str | None = None) -> bool:
        """Best-effort release of the model from Ollama GPU memory."""
        target_model = model or self.model
        try:
            # Use a short-lived client because immediate cancellation may have
            # deliberately closed the streaming client.
            response = httpx.post(
                f"{self.host}/api/generate",
                json={"model": target_model, "keep_alive": 0},
                timeout=httpx.Timeout(30.0, connect=5.0),
            )
            response.raise_for_status()
            logger.info("[Ollama] Unloaded model '%s'", target_model)
            return True
        except Exception as exc:
            logger.warning(
                "[Ollama] Could not unload model '%s': %s",
                target_model,
                exc,
            )
            return False

    def _extract_json(self, text: str) -> dict[str, Any]:
        """Extract and parse JSON from LLM output.

        Handles several common LLM output formats:
        1. Pure JSON
        2. JSON wrapped in markdown code fences (```json ... ```)
        3. JSON with leading/trailing text
        4. DeepSeek/Qwen3 thinking tags (<think>...</think>) before JSON
        """
        # Strip thinking tags (DeepSeek-R1 and Qwen3 both use these)
        think_match = re.search(r"<think>.*?</think>", text, flags=re.DOTALL)
        if think_match:
            think_len = think_match.end() - think_match.start()
            logger.info(
                "[JSON] Stripped <think> block (%d chars of reasoning)",
                think_len,
            )
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        text = text.strip()

        # Try 1: Parse directly
        try:
            result = json.loads(text)
            logger.info("[JSON] Parsed directly (try 1). Keys: %s", list(result.keys())[:8])
            return result
        except json.JSONDecodeError as e:
            logger.debug("[JSON] Direct parse failed: %s", e)

        # Check for markdown code fence block ```json ... ```
        fenced_match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, re.DOTALL)
        if fenced_match:
            try:
                result = json.loads(fenced_match.group(1))
                logger.info("[JSON] Parsed from markdown fenced block successfully (try 2).")
                return result
            except json.JSONDecodeError:
                pass

        import json_repair

        # Try robust parsing using json_repair
        try:
            logger.info("[JSON] Attempting robust json_repair parsing...")
            target_str = fenced_match.group(1) if fenced_match else text
            brace_match = re.search(r"\{.*\}", target_str, re.DOTALL)
            json_text = brace_match.group(0) if brace_match else target_str

            result = json_repair.loads(json_text)
            if isinstance(result, dict):
                logger.info("[JSON] Parsed via json_repair successfully. Keys: %s", list(result.keys())[:8])
                return result
            logger.warning("[JSON] json_repair returned non-dict type: %s", type(result))
        except Exception as e:
            logger.error("[JSON] json_repair failed: %s", e)

        logger.error(
            "[JSON] ✗ All parse attempts failed. Response head: %r | Response tail: %r",
            text[:300],
            text[-200:],
        )
        raise OllamaError(f"Could not extract valid JSON from LLM response. Response starts with: {text[:200]!r}")

    def check_health(self, *, quiet: bool = False) -> bool:
        """Check if Ollama is running and the model is available."""
        try:
            response = self._ensure_client().get(f"{self.host}/api/tags")
            response.raise_for_status()
            data = response.json()
            models = [m.get("name", "") for m in data.get("models", [])]

            available = any(self._model_names_match(self.model, installed) for installed in models)

            if not available and not quiet:
                logger.warning(
                    "Model '%s' not found. Available: %s",
                    self.model,
                    models,
                )
            return available

        except Exception as e:
            if quiet:
                logger.debug("Ollama health preflight failed: %s", e)
            else:
                logger.error("Ollama health check failed: %s", e)
            return False

    @staticmethod
    def _model_names_match(configured: str, installed: str) -> bool:
        """Match one exact Ollama tag, allowing a registry/library prefix."""
        configured = configured.strip()
        installed = installed.strip()
        return installed == configured or installed.endswith(f"/library/{configured}")

    def _resolve_configured_fallback(self) -> str | None:
        """Return the first installed fallback from the explicit allowlist."""
        if not self.fallback_models:
            return None
        try:
            response = self._ensure_client().get(f"{self.host}/api/tags", timeout=5.0)
            if response.status_code == 200:
                data = response.json()
                models = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
                logger.info("[Ollama] Discovered installed models: %s", models)
                for fallback in self.fallback_models:
                    if any(self._model_names_match(fallback, installed) for installed in models):
                        return fallback
        except Exception as e:
            logger.warning("[Ollama] Failed to resolve configured fallback models: %s", e)
        return None

    def close(self) -> None:
        """Close the HTTP client."""
        self.cancel_current(force=True)

    def __enter__(self) -> OllamaClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class OllamaError(Exception):
    """Raised when Ollama communication fails."""


class OllamaGenerationLimitError(OllamaError):
    """Raised when a generation safeguard terminates a runaway response."""
