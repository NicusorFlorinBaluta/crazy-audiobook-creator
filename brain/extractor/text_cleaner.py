"""Text cleaner — Clean raw extracted text for TTS processing.

Handles:
  - Page numbers, headers, footers
  - Ligatures and smart quotes normalization
  - Whitespace cleanup while preserving paragraph structure
  - Fantasy-specific: preserves unusual character names
"""

from __future__ import annotations

import re
import unicodedata


class TextCleaner:
    """Clean raw text extracted from EPUB for audiobook production."""

    def __init__(self, preserve_poetry: bool = True):
        self.preserve_poetry = preserve_poetry

        # Pre-compiled regex patterns for performance
        self._page_number = re.compile(r"^\s*\d{1,4}\s*$", re.MULTILINE)
        self._header_footer = re.compile(
            r"^\s*(?:[A-Z][A-Z\s]{3,}|[\w\s]+\|\s*\d+)\s*$", re.MULTILINE
        )
        self._multiple_newlines = re.compile(r"\n{3,}")
        self._multiple_spaces = re.compile(r"[ \t]{2,}")
        self._leading_trailing_whitespace = re.compile(r"^[ \t]+|[ \t]+$", re.MULTILINE)
        self._empty_lines_start_end = re.compile(r"^\s+|\s+$")

    def clean(self, text: str) -> str:
        """Apply all cleaning steps to raw text.

        Preserves paragraph structure (double newlines) since it affects
        TTS pacing and emotion context.

        Args:
            text: Raw text extracted from EPUB.

        Returns:
            Cleaned text ready for LLM script generation.
        """
        if not text or not text.strip():
            return ""

        text = self._normalize_unicode(text)
        text = self._normalize_quotes(text)
        text = self._normalize_dashes(text)
        text = self._remove_page_numbers(text)
        text = self._remove_headers_footers(text)
        text = self._clean_whitespace(text)

        return text.strip()

    # ------------------------------------------------------------------
    # Unicode normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_unicode(text: str) -> str:
        """Normalize unicode characters, expand ligatures."""
        # NFC normalization first
        text = unicodedata.normalize("NFC", text)

        # Common ligatures
        ligature_map = {
            "\ufb00": "ff",
            "\ufb01": "fi",
            "\ufb02": "fl",
            "\ufb03": "ffi",
            "\ufb04": "ffl",
            "\ufb05": "st",   # long st
            "\ufb06": "st",
        }
        for lig, replacement in ligature_map.items():
            text = text.replace(lig, replacement)

        # Replace common problematic unicode
        text = text.replace("\u00a0", " ")     # Non-breaking space
        text = text.replace("\u200b", "")      # Zero-width space
        text = text.replace("\u200c", "")      # Zero-width non-joiner
        text = text.replace("\u200d", "")      # Zero-width joiner
        text = text.replace("\ufeff", "")      # BOM
        text = text.replace("\u2028", "\n")    # Line separator
        text = text.replace("\u2029", "\n\n")  # Paragraph separator

        return text

    @staticmethod
    def _normalize_quotes(text: str) -> str:
        """Normalize smart/curly quotes to standard ASCII quotes.

        We keep the standard quotes because TTS engines handle them well
        and they help with dialogue detection.
        """
        # Smart double quotes → standard double quotes
        text = text.replace("\u201c", '"')  # Left double
        text = text.replace("\u201d", '"')  # Right double
        text = text.replace("\u201e", '"')  # Low double
        text = text.replace("\u201f", '"')  # High-reversed double

        # Smart single quotes → standard apostrophe
        text = text.replace("\u2018", "'")  # Left single
        text = text.replace("\u2019", "'")  # Right single
        text = text.replace("\u201a", "'")  # Low single
        text = text.replace("\u201b", "'")  # High-reversed single

        # Guillemets → standard double quotes
        text = text.replace("\u00ab", '"')  # «
        text = text.replace("\u00bb", '"')  # »

        return text

    @staticmethod
    def _normalize_dashes(text: str) -> str:
        """Normalize various dash characters.

        Em-dashes are preserved as they affect TTS pacing.
        """
        # En-dash → em-dash (most EPUBs use them interchangeably)
        text = text.replace("\u2013", "\u2014")  # – → —

        # Multiple hyphens → em-dash
        text = re.sub(r"---+", "\u2014", text)
        text = re.sub(r"--", "\u2014", text)

        # Figure dash, horizontal bar → em-dash
        text = text.replace("\u2012", "\u2014")
        text = text.replace("\u2015", "\u2014")

        return text

    # ------------------------------------------------------------------
    # Content removal
    # ------------------------------------------------------------------

    def _remove_page_numbers(self, text: str) -> str:
        """Remove standalone page numbers (common in EPUB from OCR or PDF conversion)."""
        return self._page_number.sub("", text)

    def _remove_headers_footers(self, text: str) -> str:
        """Remove repeated headers/footers (all-caps titles, page refs)."""
        # Only remove lines that look like headers (all-caps, short, repeated)
        lines = text.split("\n")
        cleaned_lines: list[str] = []

        for line in lines:
            stripped = line.strip()
            # Skip all-caps lines that are short (likely headers)
            if stripped and stripped == stripped.upper() and len(stripped) < 60:
                # But only if they don't look like dialogue or shouting
                word_count = len(stripped.split())
                if word_count <= 5 and not stripped.startswith('"'):
                    continue
            cleaned_lines.append(line)

        return "\n".join(cleaned_lines)

    # ------------------------------------------------------------------
    # Whitespace cleanup
    # ------------------------------------------------------------------

    def _clean_whitespace(self, text: str) -> str:
        """Clean up whitespace while preserving paragraph breaks."""
        # Normalize line endings
        text = text.replace("\r\n", "\n")
        text = text.replace("\r", "\n")

        # Remove leading/trailing whitespace from each line
        text = self._leading_trailing_whitespace.sub("", text)

        # Collapse multiple spaces within lines
        text = self._multiple_spaces.sub(" ", text)

        # Collapse 3+ newlines to double newline (paragraph break)
        text = self._multiple_newlines.sub("\n\n", text)

        return text
