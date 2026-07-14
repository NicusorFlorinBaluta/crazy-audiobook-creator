"""EPUB parser — Extract structured text and metadata from EPUB files.

Supports:
  - Metadata extraction (title, author, language, cover image)
  - Chapter boundary detection via three strategies (auto, heading, pattern)
  - Fantasy-specific content filtering (skip maps, appendices, glossaries)
  - Cover image extraction
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import ebooklib
from bs4 import BeautifulSoup, Tag
from ebooklib import epub

from brain.extractor.text_cleaner import TextCleaner
from shared.models import BookMetadata, ExtractedBook, ExtractedChapter

logger = logging.getLogger(__name__)

# Patterns for content to skip
SKIP_TITLE_PATTERNS = [
    re.compile(r"(?i)\b(table\s+of\s+contents|contents)\b"),
    re.compile(r"(?i)\b(appendix|appendices)\b"),
    re.compile(r"(?i)\b(glossary|dramatis\s+personae)\b"),
    re.compile(r"(?i)\b(map|maps)\b"),
    re.compile(r"(?i)\b(acknowledgment|acknowledgement)s?\b"),
    re.compile(r"(?i)\b(dedication|copyright|colophon)\b"),
    re.compile(r"(?i)\b(about\s+the\s+author)\b"),
    re.compile(r"(?i)\b(also\s+by|other\s+books?\s+by)\b"),
    re.compile(r"(?i)\b(preview|excerpt|teaser)\b"),
]

# Patterns for chapter title detection
CHAPTER_HEADING_PATTERNS = [
    re.compile(r"(?i)^chapter\s+(\d+|[ivxlcdm]+)", re.MULTILINE),
    re.compile(r"(?i)^part\s+(\d+|[ivxlcdm]+)", re.MULTILINE),
    re.compile(r"(?i)^prologue\b", re.MULTILINE),
    re.compile(r"(?i)^epilogue\b", re.MULTILINE),
    re.compile(r"(?i)^interlude\b", re.MULTILINE),
    re.compile(r"^\d+\.\s+", re.MULTILINE),   # "1. Title"
]


class EpubParser:
    """Parse an EPUB file into structured, chapter-separated text."""

    def __init__(
        self,
        skip_toc: bool = True,
        skip_appendices: bool = True,
        skip_front_matter: bool = True,
        min_chapter_words: int = 100,
        max_chapter_words: int = 20_000,
        chapter_detection: str = "auto",
        preserve_poetry: bool = True,
    ):
        self.skip_toc = skip_toc
        self.skip_appendices = skip_appendices
        self.skip_front_matter = skip_front_matter
        self.min_chapter_words = min_chapter_words
        self.max_chapter_words = max_chapter_words
        self.chapter_detection = chapter_detection
        self.preserve_poetry = preserve_poetry
        self.cleaner = TextCleaner(preserve_poetry=preserve_poetry)

    def parse(self, epub_path: str | Path) -> ExtractedBook:
        """Parse an EPUB file and return structured book data.

        Args:
            epub_path: Path to the EPUB file.

        Returns:
            ExtractedBook with metadata and chapters.

        Raises:
            FileNotFoundError: If the EPUB file doesn't exist.
            ValueError: If the EPUB can't be parsed.
        """
        epub_path = Path(epub_path)
        if not epub_path.exists():
            raise FileNotFoundError(f"EPUB file not found: {epub_path}")

        logger.info("Parsing EPUB: %s", epub_path.name)

        try:
            book = epub.read_epub(str(epub_path), options={"ignore_ncx": False})
        except Exception as e:
            raise ValueError(f"Failed to parse EPUB: {e}") from e

        metadata = self._extract_metadata(book, epub_path)
        cover_path = self._extract_cover(book, epub_path)
        if cover_path:
            metadata.cover_image_path = str(cover_path)

        raw_chapters = self._extract_chapters(book)
        chapters = self._finalize_chapters(raw_chapters)

        metadata.total_chapters = len(chapters)
        metadata.total_words = sum(ch.word_count for ch in chapters)

        logger.info(
            "Extracted %d chapters, %d total words from '%s'",
            metadata.total_chapters,
            metadata.total_words,
            metadata.title,
        )

        return ExtractedBook(metadata=metadata, chapters=chapters)

    # ------------------------------------------------------------------
    # Metadata extraction
    # ------------------------------------------------------------------

    def _extract_metadata(self, book: epub.EpubBook, epub_path: Path) -> BookMetadata:
        """Extract book metadata from EPUB."""
        title = self._get_meta(book, "title") or epub_path.stem
        author = self._get_meta(book, "creator") or "Unknown"
        language = self._get_meta(book, "language") or "en"

        return BookMetadata(
            title=title,
            author=author,
            language=language,
        )

    @staticmethod
    def _get_meta(book: epub.EpubBook, field: str) -> str | None:
        """Safely get a metadata field from the EPUB."""
        try:
            values = book.get_metadata("DC", field)
            if values:
                return str(values[0][0])
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Cover image extraction
    # ------------------------------------------------------------------

    def _extract_cover(self, book: epub.EpubBook, epub_path: Path) -> Path | None:
        """Extract cover image from EPUB if available."""
        cover_item = None

        # Try to find cover via metadata
        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_COVER:
                cover_item = item
                break

        # Fallback: look for common cover image names
        if cover_item is None:
            for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
                name = item.get_name().lower()
                if "cover" in name:
                    cover_item = item
                    break

        if cover_item is None:
            return None

        # Determine extension from content type
        content_type = cover_item.media_type or ""
        ext = ".jpg"
        if "png" in content_type:
            ext = ".png"
        elif "gif" in content_type:
            ext = ".gif"

        cover_path = epub_path.parent / f"{epub_path.stem}_cover{ext}"
        try:
            cover_path.write_bytes(cover_item.get_content())
            logger.info("Extracted cover image: %s", cover_path.name)
            return cover_path
        except Exception as e:
            logger.warning("Failed to extract cover image: %s", e)
            return None

    # ------------------------------------------------------------------
    # Chapter extraction
    # ------------------------------------------------------------------

    def _extract_chapters(self, book: epub.EpubBook) -> list[dict]:
        """Extract chapters from EPUB documents.

        Returns a list of dicts with 'title' and 'text' keys.
        """
        # Get spine items (reading order)
        spine_ids = [item_id for item_id, _ in book.spine]
        items_by_id: dict[str, epub.EpubItem] = {}
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            items_by_id[item.get_id()] = item

        raw_chapters: list[dict] = []

        for item_id in spine_ids:
            item = items_by_id.get(item_id)
            if item is None:
                continue

            html_content = item.get_content().decode("utf-8", errors="replace")
            soup = BeautifulSoup(html_content, "lxml")

            # Check if this document should be skipped
            if self._should_skip_document(soup):
                logger.debug("Skipping document: %s", item.get_name())
                continue

            if self.chapter_detection == "heading":
                chapters = self._split_by_headings(soup)
            elif self.chapter_detection == "pattern":
                chapters = self._split_by_patterns(soup)
            elif self.chapter_detection == "none":
                chapters = [{"title": "", "text": self._extract_text(soup)}]
            else:  # "auto"
                chapters = self._split_by_headings(soup)
                if not chapters:
                    chapters = self._split_by_patterns(soup)
                if not chapters:
                    chapters = [{"title": "", "text": self._extract_text(soup)}]

            raw_chapters.extend(chapters)

        return raw_chapters

    def _should_skip_document(self, soup: BeautifulSoup) -> bool:
        """Check if this HTML document should be skipped (ToC, appendix, etc.)."""
        # Get the document title from headings or title tag
        title_text = ""
        for tag_name in ["h1", "h2", "title"]:
            tag = soup.find(tag_name)
            if tag and tag.get_text(strip=True):
                title_text = tag.get_text(strip=True)
                break

        if not title_text:
            # Check full text if very short (likely front/back matter)
            full_text = soup.get_text(strip=True)
            if len(full_text) < 50:
                return True
            return False

        for pattern in SKIP_TITLE_PATTERNS:
            if pattern.search(title_text):
                return True

        return False

    def _split_by_headings(self, soup: BeautifulSoup) -> list[dict]:
        """Split a document into chapters using HTML heading tags."""
        chapters: list[dict] = []
        heading_tags = soup.find_all(["h1", "h2"])

        if not heading_tags:
            return []

        for i, heading in enumerate(heading_tags):
            title = heading.get_text(strip=True)

            # Skip if title matches a skip pattern
            if any(p.search(title) for p in SKIP_TITLE_PATTERNS):
                continue

            # Collect all content between this heading and the next
            content_parts: list[str] = []
            sibling = heading.next_sibling

            while sibling is not None:
                if isinstance(sibling, Tag) and sibling.name in ["h1", "h2"]:
                    break
                if isinstance(sibling, Tag):
                    text = self._extract_element_text(sibling)
                    if text:
                        content_parts.append(text)
                sibling = sibling.next_sibling

            text = "\n\n".join(content_parts)
            if text.strip():
                chapters.append({"title": title, "text": text})

        return chapters

    def _split_by_patterns(self, soup: BeautifulSoup) -> list[dict]:
        """Split a document into chapters using text patterns (Chapter 1, etc.)."""
        full_text = self._extract_text(soup)
        if not full_text:
            return []

        # Find all chapter-like headings in the text
        split_points: list[tuple[int, str]] = []
        for pattern in CHAPTER_HEADING_PATTERNS:
            for match in pattern.finditer(full_text):
                split_points.append((match.start(), match.group(0).strip()))

        if not split_points:
            return []

        # Sort by position
        split_points.sort(key=lambda x: x[0])

        chapters: list[dict] = []
        for i, (start, title) in enumerate(split_points):
            end = split_points[i + 1][0] if i + 1 < len(split_points) else len(full_text)
            text = full_text[start:end]

            # Remove the title line from the text body
            lines = text.split("\n", 1)
            body = lines[1].strip() if len(lines) > 1 else ""

            if body:
                chapters.append({"title": title, "text": body})

        return chapters

    def _extract_text(self, soup: BeautifulSoup) -> str:
        """Extract all text from a BeautifulSoup document."""
        # Remove script and style elements
        for tag in soup.find_all(["script", "style", "nav"]):
            tag.decompose()

        paragraphs: list[str] = []
        for element in soup.find_all(["p", "div", "blockquote", "pre"]):
            text = self._extract_element_text(element)
            if text:
                paragraphs.append(text)

        if not paragraphs:
            # Fallback: just get all text
            return soup.get_text(separator="\n\n", strip=True)

        return "\n\n".join(paragraphs)

    def _extract_element_text(self, element: Tag) -> str:
        """Extract text from a single HTML element, preserving poetry line breaks."""
        if self.preserve_poetry and element.name in ["pre", "blockquote"]:
            # Preserve line breaks within poetry/songs
            text = element.get_text(separator="\n", strip=False)
            return text.strip()

        # For normal paragraphs, collapse whitespace
        text = element.get_text(separator=" ", strip=True)
        return text

    # ------------------------------------------------------------------
    # Chapter finalization
    # ------------------------------------------------------------------

    def _finalize_chapters(self, raw_chapters: list[dict]) -> list[ExtractedChapter]:
        """Clean, number, and filter chapters."""
        finalized: list[ExtractedChapter] = []
        chapter_number = 0

        for raw in raw_chapters:
            text = self.cleaner.clean(raw["text"])
            if not text:
                continue

            word_count = len(text.split())

            # Skip chapters that are too short
            if word_count < self.min_chapter_words:
                logger.debug(
                    "Skipping short chapter '%s' (%d words)",
                    raw["title"],
                    word_count,
                )
                continue

            # Split chapters that are too long
            if word_count > self.max_chapter_words:
                sub_chapters = self._split_long_chapter(
                    raw["title"], text, chapter_number
                )
                finalized.extend(sub_chapters)
                chapter_number += len(sub_chapters)
                continue

            chapter_number += 1
            title = raw["title"] or f"Chapter {chapter_number}"

            finalized.append(
                ExtractedChapter(
                    number=chapter_number,
                    title=title,
                    text=text,
                    word_count=word_count,
                )
            )

        # If no chapters were found, treat the entire content as one chapter
        if not finalized and raw_chapters:
            all_text = "\n\n".join(
                self.cleaner.clean(r["text"]) for r in raw_chapters if r["text"]
            )
            if all_text.strip():
                logger.warning("No chapter boundaries detected — treating entire book as one chapter")
                finalized.append(
                    ExtractedChapter(
                        number=1,
                        title="Full Text",
                        text=all_text,
                        word_count=len(all_text.split()),
                    )
                )

        return finalized

    def _split_long_chapter(
        self, title: str, text: str, start_number: int
    ) -> list[ExtractedChapter]:
        """Split a chapter that exceeds max_chapter_words."""
        words = text.split()
        parts: list[ExtractedChapter] = []
        part_num = 0

        i = 0
        while i < len(words):
            end = min(i + self.max_chapter_words, len(words))

            # Try to split at a paragraph break (double newline)
            chunk_text = " ".join(words[i:end])
            if end < len(words):
                # Find the last paragraph break
                last_para = chunk_text.rfind("\n\n")
                if last_para > len(chunk_text) // 2:
                    chunk_text = chunk_text[:last_para]
                    end = i + len(chunk_text.split())

            part_num += 1
            part_title = f"{title} (Part {part_num})" if title else f"Chapter {start_number + part_num}"
            word_count = len(chunk_text.split())

            parts.append(
                ExtractedChapter(
                    number=start_number + part_num,
                    title=part_title,
                    text=chunk_text.strip(),
                    word_count=word_count,
                )
            )

            i = end

        return parts
