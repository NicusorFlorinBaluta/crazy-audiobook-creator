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
        max_retries: int = 15,
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
            format: Optional response format (e.g., 'json').

        Returns:
            The generated text response.

        Raises:
            OllamaError: If the request fails after all retries.
        """
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
                "num_predict": -1,
                "num_ctx": 8192,
                "num_gpu": 99,
            },
        }

        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
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
                response = self._client.post(
                    f"{self.host}/api/chat",
                    json=payload,
                    timeout=httpx.Timeout(self.timeout),
                )
                response.raise_for_status()
                logger.info("[Ollama] ← HTTP 200 received, streaming tokens...")

                full_text = []
                token_count = 0
                last_log_tokens = 0
                for line in response.iter_lines():
                    if line:
                        chunk = json.loads(line)
                        if "message" in chunk and "content" in chunk["message"]:
                            full_text.append(chunk["message"]["content"])
                            token_count += 1
                            # Log every 200 tokens so we know it's alive
                            if token_count - last_log_tokens >= 200:
                                elapsed = time.time() - t0
                                logger.info(
                                    "[Ollama] ↻ Streaming... %d tokens | %.1f tok/s | %.0fs elapsed",
                                    token_count,
                                    token_count / elapsed if elapsed > 0 else 0,
                                    elapsed,
                                )
                                last_log_tokens = token_count

                text = "".join(full_text)
                elapsed = time.time() - t0

                if not text.strip():
                    with open("empty_response_debug.txt", "w", encoding="utf-8") as f:
                        f.write(text)
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
                last_error = e
                logger.warning(
                    "[Ollama] ✗ Unexpected error (attempt %d/%d): %s: %s",
                    attempt,
                    self.max_retries,
                    type(e).__name__,
                    e,
                )

            if attempt < self.max_retries:
                wait = min(30, 2 ** attempt)
                logger.info("[Ollama] Retrying in %d seconds...", wait)
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

        import json_repair
        
        # Try robust parsing using json_repair
        try:
            logger.info("[JSON] Attempting robust json_repair parsing...")
            # We want to feed json_repair just the text after the think block, or the whole text
            # Usually extracting from the first { to the end helps it.
            brace_match = re.search(r"\{.*\}", text, re.DOTALL)
            json_text = brace_match.group(0) if brace_match else text
            
            result = json_repair.loads(json_text)
            if isinstance(result, dict):
                logger.info("[JSON] Parsed via json_repair successfully. Keys: %s", list(result.keys())[:8])
                return result
            else:
                logger.warning("[JSON] json_repair returned non-dict type: %s", type(result))
        except Exception as e:
            logger.error("[JSON] json_repair failed: %s", e)

        logger.error(
            "[JSON] ✗ All parse attempts failed. Response head: %r | Response tail: %r",
            text[:300],
            text[-200:],
        )
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
