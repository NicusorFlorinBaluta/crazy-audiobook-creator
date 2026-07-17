"""EPUB text extraction module.

Handles parsing EPUB files into clean, chapter-separated text
with metadata extraction and fantasy-content handling.
"""

from brain.extractor.epub_parser import EpubParser
from brain.extractor.text_cleaner import TextCleaner

__all__ = ["EpubParser", "TextCleaner"]
