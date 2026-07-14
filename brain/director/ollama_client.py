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
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class OllamaClient:
    """Client for the Ollama LLM API."""

    def __init__(
        self,
        host: str = "http://localhost:11434",
        model: str = "qwen3:32b",
        timeout: int = 120,
        max_retries: int = 3,
    ):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0))

    def generate(
        self,
        prompt: str,
        *,
        temperature: float = 0.4,
        top_p: float = 0.9,
        system: str | None = None,
    ) -> str:
        """Generate a text completion from the LLM.

        Args:
            prompt: The user prompt to send.
            temperature: Sampling temperature (lower = more deterministic).
            top_p: Nucleus sampling parameter.
            system: Optional system prompt.

        Returns:
            The generated text response.

        Raises:
            OllamaError: If the request fails after all retries.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "top_p": top_p,
            },
        }
        if system:
            payload["system"] = system

        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                logger.debug(
                    "Ollama request (attempt %d/%d, model=%s, temp=%.1f)",
                    attempt,
                    self.max_retries,
                    self.model,
                    temperature,
                )

                response = self._client.post(
                    f"{self.host}/api/generate",
                    json=payload,
                )
                response.raise_for_status()

                data = response.json()
                text = data.get("response", "")

                if not text.strip():
                    raise OllamaError("Empty response from Ollama")

                logger.debug(
                    "Ollama response: %d chars, eval_duration=%.1fs",
                    len(text),
                    data.get("eval_duration", 0) / 1e9,
                )
                return text

            except httpx.TimeoutException as e:
                last_error = e
                logger.warning(
                    "Ollama request timed out (attempt %d/%d): %s",
                    attempt,
                    self.max_retries,
                    e,
                )
            except httpx.HTTPStatusError as e:
                last_error = e
                logger.warning(
                    "Ollama HTTP error (attempt %d/%d): %s",
                    attempt,
                    self.max_retries,
                    e,
                )
            except Exception as e:
                last_error = e
                logger.warning(
                    "Ollama request failed (attempt %d/%d): %s",
                    attempt,
                    self.max_retries,
                    e,
                )

            if attempt < self.max_retries:
                wait = 2 ** attempt
                logger.info("Retrying in %d seconds...", wait)
                time.sleep(wait)

        raise OllamaError(
            f"Failed after {self.max_retries} attempts: {last_error}"
        ) from last_error

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
        )

        return self._extract_json(raw)

    def _extract_json(self, text: str) -> dict[str, Any]:
        """Extract and parse JSON from LLM output.

        Handles several common LLM output formats:
        1. Pure JSON
        2. JSON wrapped in markdown code fences (```json ... ```)
        3. JSON with leading/trailing text
        4. Qwen3 thinking tags (<think>...</think>) before JSON
        """
        # Strip Qwen3 thinking tags if present
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        text = text.strip()

        # Try 1: Parse directly
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try 2: Extract from markdown code fences
        fence_match = re.search(
            r"```(?:json)?\s*\n?(.*?)\n?\s*```",
            text,
            re.DOTALL,
        )
        if fence_match:
            try:
                return json.loads(fence_match.group(1))
            except json.JSONDecodeError:
                pass

        # Try 3: Find the first { ... } block
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass

        # Try 4: Find outermost braces more carefully
        depth = 0
        start = -1
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        start = -1

        raise OllamaError(
            f"Could not extract valid JSON from LLM response. "
            f"Response starts with: {text[:200]!r}"
        )

    def check_health(self) -> bool:
        """Check if Ollama is running and the model is available."""
        try:
            response = self._client.get(f"{self.host}/api/tags")
            response.raise_for_status()
            data = response.json()
            models = [m.get("name", "") for m in data.get("models", [])]

            # Check if our model is available (allow partial match)
            model_base = self.model.split(":")[0]
            available = any(model_base in m for m in models)

            if not available:
                logger.warning(
                    "Model '%s' not found. Available: %s",
                    self.model,
                    models,
                )
            return available

        except Exception as e:
            logger.error("Ollama health check failed: %s", e)
            return False

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self) -> OllamaClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class OllamaError(Exception):
    """Raised when Ollama communication fails."""
