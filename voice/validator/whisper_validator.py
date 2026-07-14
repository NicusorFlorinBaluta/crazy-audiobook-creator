"""Whisper Validator — Speech-to-text validation using faster-whisper.

Transcribes generated audio and compares it to the expected text
using Word Error Rate (WER) to detect garbled or missing words.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


class WhisperValidator:
    """Validate TTS audio using Whisper speech-to-text."""

    def __init__(
        self,
        model_name: str = "medium",
        device: str = "auto",
    ):
        self.model_name = model_name
        self.device = device
        self._model = None
        self._is_loaded = False

    @property
    def is_loaded(self) -> bool:
        return self._is_loaded

    def load(self) -> None:
        """Load the Whisper model."""
        if self._is_loaded:
            return

        logger.info("Loading Whisper model '%s' (device=%s)...", self.model_name, self.device)

        try:
            from faster_whisper import WhisperModel

            # Determine device
            compute_type = "float16"
            device = self.device

            if device == "auto":
                try:
                    import torch
                    device = "cuda" if torch.cuda.is_available() else "cpu"
                except ImportError:
                    device = "cpu"

            if device == "cpu":
                compute_type = "int8"

            self._model = WhisperModel(
                self.model_name,
                device=device,
                compute_type=compute_type,
            )
            self._is_loaded = True
            logger.info("Whisper model loaded (device=%s)", device)

        except Exception as e:
            logger.error("Failed to load Whisper: %s", e)
            raise

    def unload(self) -> None:
        """Unload the Whisper model to free memory."""
        if self._model:
            del self._model
            self._model = None
            self._is_loaded = False

            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except ImportError:
                pass

            logger.info("Whisper model unloaded")

    def transcribe(self, audio_file: str) -> str:
        """Transcribe an audio file to text.

        Args:
            audio_file: Path to the .wav file.

        Returns:
            Transcribed text.
        """
        if not self._is_loaded:
            self.load()

        segments, info = self._model.transcribe(
            audio_file,
            language="en",
            beam_size=5,
            vad_filter=True,
        )

        text = " ".join(segment.text for segment in segments)
        return text.strip()

    def calculate_wer(
        self,
        reference: str,
        hypothesis: str,
        normalize: bool = True,
    ) -> float:
        """Calculate Word Error Rate between reference and transcribed text.

        Args:
            reference: The original (expected) text.
            hypothesis: The transcribed text from Whisper.
            normalize: Whether to normalize texts before comparison.

        Returns:
            WER as a float between 0.0 and 1.0.
        """
        if normalize:
            reference = self._normalize_text(reference)
            hypothesis = self._normalize_text(hypothesis)

        try:
            import jiwer
            wer = jiwer.wer(reference, hypothesis)
            return min(wer, 1.0)  # Cap at 1.0
        except Exception as e:
            logger.error("WER calculation failed: %s", e)
            return 1.0  # Assume worst case

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Normalize text for WER comparison.

        - Lowercase
        - Remove punctuation
        - Expand common numbers
        - Collapse whitespace
        """
        text = text.lower()

        # Remove punctuation (keep apostrophes for contractions)
        text = re.sub(r"[^\w\s']", " ", text)

        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()

        return text
