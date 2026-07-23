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
            try:
                from faster_whisper import WhisperModel

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
                self._backend = "faster_whisper"
            except ImportError:
                import whisper
                import torch

                device = self.device
                if device == "auto":
                    device = "cuda" if torch.cuda.is_available() else "cpu"

                logger.info("faster_whisper not found, using openai-whisper on %s...", device)
                self._model = whisper.load_model(self.model_name, device=device)
                self._backend = "openai_whisper"

            self._is_loaded = True
            logger.info("Whisper model loaded using %s (device=%s)", getattr(self, "_backend", "faster_whisper"), device)

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

        try:
            if getattr(self, "_backend", "faster_whisper") == "openai_whisper":
                result = self._model.transcribe(audio_file, language="en")
                return result.get("text", "").strip()
            else:
                segments, info = self._model.transcribe(
                    audio_file,
                    language="en",
                    beam_size=5,
                    vad_filter=True,
                )
                text = " ".join(segment.text for segment in segments)
                return text.strip()
        except Exception as e:
            logger.warning("[WhisperValidator] STT transcription failed for '%s': %s", audio_file, e)
            return ""

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
            norm_ref = self._normalize_text(reference)
            norm_hyp = self._normalize_text(hypothesis)
        else:
            norm_ref, norm_hyp = reference, hypothesis

        ref_words = norm_ref.split()
        hyp_words = norm_hyp.split()

        # Check for hallucinated prompt prefixes (e.g., hypothesis starts with "you" or "u" when reference does not)
        if hyp_words and ref_words:
            first_hyp = hyp_words[0].lower()
            first_ref = ref_words[0].lower()
            if first_hyp in {"you", "u", "user"} and first_ref not in {"you", "u", "user"}:
                logger.warning("[WhisperValidator] Detected leading prompt token hallucination: %r vs ref %r", hyp_words[:3], ref_words[:3])
                return 0.50  # Instantly fail threshold for leading prompt hallucinations

        try:
            import jiwer
            wer = jiwer.wer(norm_ref, norm_hyp)
            return min(wer, 1.0)  # Cap at 1.0
        except ImportError:
            if not ref_words:
                return 0.0 if not hyp_words else 1.0
            d = [[0] * (len(hyp_words) + 1) for _ in range(len(ref_words) + 1)]
            for i in range(len(ref_words) + 1):
                d[i][0] = i
            for j in range(len(hyp_words) + 1):
                d[0][j] = j
            for i in range(1, len(ref_words) + 1):
                for j in range(1, len(hyp_words) + 1):
                    cost = 0 if ref_words[i - 1] == hyp_words[j - 1] else 1
                    d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            return min(d[len(ref_words)][len(hyp_words)] / len(ref_words), 1.0)
        except Exception as e:
            logger.error("WER calculation failed: %s", e)
            return 1.0  # Assume worst case

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Fully generic text normalizer for WER calculation across any book.

        Handles:
          - OpenAI EnglishTextNormalizer (spelling variants, contractions, symbols, abbreviations)
          - Dynamic cardinal & ordinal number expansion via num2words (e.g. 1st->first, 12->twelve, 1999->one thousand...)
          - Punctuation stripping & whitespace collapsing
        """
        if not text:
            return ""

        # Step 1: Use OpenAI Whisper's official English normalizer if available
        try:
            from whisper.normalizers import EnglishTextNormalizer
            if not hasattr(WhisperValidator, "_english_normalizer"):
                WhisperValidator._english_normalizer = EnglishTextNormalizer()
            text = WhisperValidator._english_normalizer(text)
        except Exception:
            text = text.lower()

        # Step 2: Dynamically convert any remaining numbers/ordinals to words
        try:
            import num2words

            def replace_ordinal(match):
                num_str, suffix = match.group(1), match.group(2)
                try:
                    return " " + num2words.num2words(int(num_str), to="ordinal") + " "
                except Exception:
                    return match.group(0)

            def replace_cardinal(match):
                try:
                    return " " + num2words.num2words(int(match.group(0))) + " "
                except Exception:
                    return match.group(0)

            # Match ordinals first (e.g., 21st, 100th)
            text = re.sub(r"\b(\d+)(st|nd|rd|th)\b", replace_ordinal, text, flags=re.IGNORECASE)
            # Match cardinal numbers (e.g., 42, 1000)
            text = re.sub(r"\b\d+\b", replace_cardinal, text)
        except ImportError:
            pass

        # Step 3: Remove punctuation and collapse whitespace
        text = re.sub(r"[^\w\s]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        return text
