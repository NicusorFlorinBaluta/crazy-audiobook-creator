"""EPUB parser — Extract structured text and metadata from EPUB files.

Supports:
  - Metadata extraction (title, author, language, cover image)
  - Chapter boundary detection via three strategies (auto, heading, pattern)
  - Fantasy-specific content filtering (skip maps, appendices, glossaries)
  - Cover image extraction
"""

from __future__ import annotations

import logging
import math
import re
import warnings
from collections import Counter
from pathlib import Path

import ebooklib
from bs4 import BeautifulSoup, Tag, XMLParsedAsHTMLWarning
from ebooklib import epub

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from brain.extractor.text_cleaner import TextCleaner
from shared.models import BookMetadata, ExtractedBook, ExtractedChapter

logger = logging.getLogger(__name__)

TOC_TITLE_PATTERNS = [
    re.compile(r"(?i)\b(table\s+of\s+contents|contents|toc)\b"),
]

APPENDIX_TITLE_PATTERNS = [
    re.compile(r"(?i)\b(appendix|appendices|addendum|addenda)\b"),
    re.compile(r"(?i)\b(glossary|dramatis\s+personae|character\s+list)\b"),
    re.compile(r"(?i)\b(map|maps|illustrations?|figures?)\b"),
    re.compile(r"(?i)\b(about\s+the\s+(author|illustrator|artist|creator|publisher)s?)\b"),
    re.compile(r"(?i)\b(preview|excerpt|teaser|sneak\s+peek|bonus\s+(material|content))\b"),
    re.compile(r"(?i)\b(reading\s+group\s+guide|discussion\s+questions|reader'?s?\s+guide)\b"),
    re.compile(r"(?i)\b(newsletter|sign\s+up|connect\s+with|stay\s+in\s+touch)\b"),
]

FRONT_MATTER_TITLE_PATTERNS = [
    re.compile(r"(?i)\b(acknowledgment|acknowledgement)s?\b"),
    re.compile(r"(?i)\b(dedication|copyright|colophon|title\s+page|half\s+title)\b"),
    re.compile(r"(?i)\b(also\s+by|other\s+books?\s+by|books\s+by)\b"),
    re.compile(r"(?i)\b(praise\s+for|advance\s+praise|reviews?)\b"),
]

# Compatibility export for callers/tests that inspect the complete catalog.
SKIP_TITLE_PATTERNS = TOC_TITLE_PATTERNS + APPENDIX_TITLE_PATTERNS + FRONT_MATTER_TITLE_PATTERNS

# Patterns for character reference material (glossary, dramatis personae, character list)
REFERENCE_TITLE_PATTERNS = [
    re.compile(
        r"(?i)\b(glossary|dramatis\s+personae|character\s+list|cast\s+of\s+characters|list\s+of\s+characters|names\s+and\s+terms)\b"
    ),
    re.compile(r"(?i)\bappendix\b.*(?:characters|names|dramatis|glossary|people|cast)"),
]

# Patterns for optional front-matter (preface, foreword, author's note, etc.)
PREFACE_TITLE_PATTERNS = [
    re.compile(r"(?i)\b(author'?s?\s+note|preface|foreword|afterword|postscript)\b"),
]

# Patterns for chapter title detection
CHAPTER_HEADING_PATTERNS = [
    re.compile(r"(?i)^chapter\s+([\w-]+(?:\s+[\w-]+){0,3})", re.MULTILINE),
    re.compile(r"(?i)^part\s+(\d+|[ivxlcdm]+)", re.MULTILINE),
    re.compile(r"(?i)^prologue\b", re.MULTILINE),
    re.compile(r"(?i)^epilogue\b", re.MULTILINE),
    re.compile(r"(?i)^interlude\b", re.MULTILINE),
    re.compile(r"^\d+\.\s+", re.MULTILINE),  # "1. Title"
]


class EpubParser:
    """Parse an EPUB file into structured, chapter-separated text."""

    def __init__(
        self,
        skip_toc: bool = True,
        skip_appendices: bool = True,
        skip_front_matter: bool = True,
        skip_preface: bool = True,
        min_chapter_words: int = 100,
        max_chapter_words: int = 20_000,
        chapter_detection: str = "auto",
        preserve_poetry: bool = True,
    ):
        self.skip_toc = skip_toc
        self.skip_appendices = skip_appendices
        self.skip_front_matter = skip_front_matter
        self.skip_preface = skip_preface
        self.min_chapter_words = min_chapter_words
        self.max_chapter_words = max_chapter_words
        self.chapter_detection = chapter_detection
        self.preserve_poetry = preserve_poetry
        self.cleaner = TextCleaner(preserve_poetry=preserve_poetry)
        self.last_audit: dict = {"schema": 1, "sections": [], "summary": {}}
        self._repeated_headers: set[str] = set()
        self._section_overrides: dict[str, str] = {}

    def _is_skippable_title(self, title: str) -> bool:
        """Check whether a section or chapter title matches active skip patterns."""
        if not title:
            return False
        if self.skip_toc and any(p.search(title) for p in TOC_TITLE_PATTERNS):
            return True
        if self.skip_appendices and any(p.search(title) for p in APPENDIX_TITLE_PATTERNS):
            return True
        if self.skip_front_matter and any(p.search(title) for p in FRONT_MATTER_TITLE_PATTERNS):
            return True
        if self.skip_preface and any(p.search(title) for p in PREFACE_TITLE_PATTERNS):
            return True
        return False

    def _is_reference_title(self, title: str) -> bool:
        """Check whether a section title represents character reference material."""
        if not title:
            return False
        return any(p.search(title) for p in REFERENCE_TITLE_PATTERNS)

    @staticmethod
    def _looks_narrative_title(title: str) -> bool:
        normalized = title.strip()
        return any(pattern.search(normalized) for pattern in CHAPTER_HEADING_PATTERNS)

    @staticmethod
    def _semantic_tokens(soup: BeautifulSoup) -> set[str]:
        tokens: set[str] = set()
        for tag in soup.find_all(True):
            for attribute in ("epub:type", "role", "class"):
                value = tag.attrs.get(attribute)
                values = value if isinstance(value, list) else [value]
                for item in values:
                    if item:
                        tokens.update(re.split(r"[\s_-]+", str(item).casefold()))
        return {token for token in tokens if token}

    def _classify_document(
        self,
        item: epub.EpubItem,
        soup: BeautifulSoup,
        title: str,
        extra_semantics: set[str] | None = None,
    ) -> dict:
        """Classify one spine document and record why it is safe or ambiguous."""
        item_id = str(item.get_id())
        href = str(item.get_name())
        words = len(soup.get_text(" ", strip=True).split())
        semantics = sorted(self._semantic_tokens(soup) | set(extra_semantics or ()))
        filename_label = Path(href).stem.replace("_", " ").replace("-", " ")
        override = self._section_overrides.get(item_id)
        if override in {"include", "exclude", "reference"}:
            return {
                "item_id": item_id,
                "href": href,
                "title": title or href,
                "word_count": words,
                "semantics": semantics,
                "decision": override,
                "confidence": 1.0,
                "reason": "Explicit extraction override",
                "review_required": False,
            }

        semantic_set = set(semantics)
        reference_semantics = {"glossary", "bibliography"}
        exclude_semantics = {
            "toc",
            "navigation",
            "cover",
            "titlepage",
            "copyright",
            "dedication",
            "acknowledgments",
            "colophon",
            "index",
        }
        narrative_semantics = {
            "chapter",
            "prologue",
            "epilogue",
            "bodymatter",
            "part",
        }
        if self.skip_appendices and (self._is_reference_title(title) or semantic_set.intersection(reference_semantics)):
            decision, confidence, reason = "reference", 0.98, "Reference-material title or EPUB semantics"
        elif semantic_set.intersection(exclude_semantics):
            decision, confidence, reason = "exclude", 0.98, "Non-narrative EPUB semantics"
        elif semantic_set.intersection(narrative_semantics):
            decision, confidence, reason = "include", 0.98, "Narrative EPUB semantics"
        elif self._is_skippable_title(title):
            decision, confidence, reason = "exclude", 0.94, "Configured non-narrative title pattern"
        elif self._looks_narrative_title(title):
            decision, confidence, reason = "include", 0.97, "Narrative chapter title pattern"
        elif self._is_reference_title(filename_label):
            decision, confidence, reason = "reference", 0.9, "Reference-material filename pattern"
        elif self._is_skippable_title(filename_label):
            decision, confidence, reason = "exclude", 0.9, "Non-narrative filename pattern"
        elif re.search(r"(?i)^(?:ch(?:apter)?\s*\d+|prologue|epilogue|interlude)$", filename_label):
            decision, confidence, reason = "include", 0.9, "Narrative filename pattern"
        elif not title and words < 50:
            decision, confidence, reason = "exclude", 0.9, "Untitled document shorter than 50 words"
        elif not title:
            decision, confidence, reason = "include", 0.65, "Untitled substantial spine document"
        else:
            decision, confidence, reason = "include", 0.84, "Titled spine document with no exclusion evidence"

        ambiguous_large_exclusion = (
            decision == "exclude"
            and words >= 300
            and (
                any(pattern.search(title) for pattern in PREFACE_TITLE_PATTERNS)
                or re.search(r"(?i)\b(appendix|addendum|bonus)\b", title) is not None
            )
        )
        review_required = confidence < 0.75 or ambiguous_large_exclusion
        sample_text = soup.get_text(" ", strip=True)
        return {
            "item_id": item_id,
            "href": href,
            "title": title or href,
            "word_count": words,
            "semantics": semantics,
            "decision": decision,
            "confidence": confidence,
            "reason": reason,
            "review_required": review_required,
            # Kept out of the review API by default. It exists solely for the
            # automated ambiguity resolver and is deliberately bounded.
            "classifier_excerpt": sample_text[:400],
        }

    @staticmethod
    def _strip_non_narrative_markup(soup: BeautifulSoup) -> None:
        """Remove notes/navigation without depending on publisher-specific CSS."""
        markers = {"footnote", "endnote", "rearnote", "noteref", "doc-footnote", "doc-endnote"}
        for tag in list(soup.find_all(True)):
            values: list[str] = []
            for key in ("epub:type", "role", "class"):
                value = tag.attrs.get(key)
                values.extend(value if isinstance(value, list) else [value] if value else [])
            tokens = {token for value in values for token in re.split(r"[\s_-]+", str(value).casefold()) if token}
            if tag.name == "aside" or tokens.intersection(markers):
                tag.decompose()

    def parse(
        self,
        epub_path: str | Path,
        *,
        section_overrides: dict[str, str] | None = None,
    ) -> ExtractedBook:
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

        self._section_overrides = dict(section_overrides or {})
        self._repeated_headers = self._find_repeated_headers(book)
        self.last_audit = {
            "schema": 1,
            "sections": [],
            "summary": {},
            "repeated_headers": sorted(self._repeated_headers),
        }
        raw_chapters, reference_material = self._extract_chapters(book)
        chapters = self._finalize_chapters(raw_chapters)

        self._apply_book_level_anomalies(chapters)

        metadata.total_chapters = len(chapters)
        metadata.total_words = sum(ch.word_count for ch in chapters)

        logger.info(
            "Extracted %d chapters (%d reference sections), %d total words from '%s'",
            metadata.total_chapters,
            len(reference_material),
            metadata.total_words,
            metadata.title,
        )
        sections = self.last_audit["sections"]
        self.last_audit["summary"] = {
            "spine_documents": len(sections),
            "included": sum(item["decision"] == "include" for item in sections),
            "excluded": sum(item["decision"] == "exclude" for item in sections),
            "reference": sum(item["decision"] == "reference" for item in sections),
            "blocking": sum(bool(item.get("review_required")) for item in sections),
            "chapters": len(chapters),
            "words": metadata.total_words,
        }

        return ExtractedBook(
            metadata=metadata,
            chapters=chapters,
            reference_material=reference_material,
        )

    def _apply_book_level_anomalies(
        self,
        chapters: list[ExtractedChapter],
    ) -> None:
        """Turn suspicious whole-book outcomes into explicit review work."""
        sections = self.last_audit["sections"]
        total_spine_words = sum(int(item.get("word_count", 0)) for item in sections)
        excluded = [item for item in sections if item.get("decision") == "exclude"]
        excluded_words = sum(int(item.get("word_count", 0)) for item in excluded)
        excluded_ratio = excluded_words / total_spine_words if total_spine_words else 0.0

        if excluded_words >= 1_000 and excluded_ratio >= 0.35 and excluded:
            largest = max(excluded, key=lambda item: int(item.get("word_count", 0)))
            largest["review_required"] = True
            largest["reason"] = (
                f"{largest.get('reason', 'Excluded locally')}; excluded sections contain "
                f"{excluded_ratio:.0%} of spine words"
            )
            largest.setdefault("anomalies", []).append("large_excluded_word_ratio")

        if not chapters:
            candidates = [item for item in sections if item.get("decision") == "include"]
            for item in candidates or sections[:1]:
                item["review_required"] = True
                item.setdefault("anomalies", []).append("zero_extracted_chapters")
                item["reason"] = f"{item.get('reason', 'Local classification')}; extraction produced no chapters"

        self.last_audit["anomalies"] = {
            "total_spine_words": total_spine_words,
            "excluded_words": excluded_words,
            "excluded_ratio": round(excluded_ratio, 4),
            "zero_chapters": not chapters,
        }

    def _find_repeated_headers(self, book: epub.EpubBook) -> set[str]:
        """Find short exact lines repeated across a meaningful share of documents."""
        documents = []
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            soup = BeautifulSoup(
                item.get_content().decode("utf-8", errors="replace"),
                "html.parser",
            )
            explicit = list(soup.find_all(["header", "footer"]))
            blocks = list(soup.find_all(["p", "div"], recursive=True))
            edge_blocks = blocks[:2] + blocks[-2:]
            candidates = set()
            for tag in explicit + edge_blocks:
                text = tag.get_text(" ", strip=True)
                if 1 <= len(text.split()) <= 8 and len(text) <= 80 and not self._looks_narrative_title(text):
                    candidates.add(text.casefold())
            documents.append(candidates)
        counts = Counter(line for document in documents for line in document)
        threshold = max(3, math.ceil(len(documents) * 0.4))
        return {line for line, count in counts.items() if count >= threshold}

    # ------------------------------------------------------------------
    # Metadata extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_book_title(raw: str) -> str:
        """Tidy separator debris in a book title without rewriting the title.

        Titles arrive either from EPUB metadata or from a filename stem, and
        both routinely carry the residue of whatever produced the file --
        "The Finest Edge of Twilight - - Book" is a real example from this
        library. That string reaches the dashboard, the M4B metadata and the
        Android companion, so it is worth fixing once at the source.

        Deliberately conservative. Only *adjacent duplicate* separators are
        collapsed, and only leading/trailing ones are stripped. A single
        interior separator is load-bearing punctuation in a great many real
        titles ("Dune - Book Two"), so it is left exactly as written.
        """
        if not raw:
            return ""
        title = re.sub(r"\s+", " ", raw.strip())
        # A run of two or more separators standing as its own word, e.g.
        # "Twilight - - Book" or "Title - : Subtitle". Whitespace on both sides
        # is required so an intra-word double hyphen ("Wait--what?"), which is
        # an ASCII em-dash rather than debris, is left alone.
        title = re.sub(r"(?<=\s)[-–—:|](?:\s*[-–—:|])+(?=\s)", "-", title)
        title = title.strip(" -–—:|").strip()
        return title or raw.strip()

    def _extract_metadata(self, book: epub.EpubBook, epub_path: Path) -> BookMetadata:
        """Extract book metadata from EPUB."""
        title = self._normalize_book_title(self._get_meta(book, "title") or epub_path.stem)
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
        except Exception as exc:
            logger.debug("EPUB has no readable DC:%s metadata: %s", field, exc)
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

    def _get_document_title(self, soup: BeautifulSoup) -> str:
        """Get the title of an HTML document from heading or title tags."""
        for tag_name in ["h1", "h2", "h3", "h4", "h5", "h6", "title"]:
            tag = soup.find(tag_name)
            if tag and tag.get_text(strip=True):
                return tag.get_text(strip=True)
        return ""

    @staticmethod
    def _toc_labels(book: epub.EpubBook) -> dict[str, str]:
        """Return nav/NCX href labels without assuming one ebooklib TOC shape."""
        labels: dict[str, str] = {}

        def visit(entry: object) -> None:
            if isinstance(entry, (list, tuple)):
                # ebooklib uses both a plain list and (Section, children).
                if len(entry) == 2 and isinstance(entry[1], (list, tuple)):
                    visit(entry[0])
                    visit(entry[1])
                    return
                for child in entry:
                    visit(child)
                return
            href = getattr(entry, "href", None)
            title = getattr(entry, "title", None)
            if href and title:
                labels[str(href).split("#", 1)[0].replace("\\", "/")] = str(title).strip()

        visit(getattr(book, "toc", []))
        return labels

    @staticmethod
    def _navigation_semantics(book: epub.EpubBook) -> dict[str, set[str]]:
        """Map EPUB2 guide and EPUB3 landmark semantics onto target documents."""
        result: dict[str, set[str]] = {}
        for entry in getattr(book, "guide", []) or []:
            if not isinstance(entry, dict) or not entry.get("href"):
                continue
            href = str(entry["href"]).split("#", 1)[0].replace("\\", "/")
            kind = str(entry.get("type", "")).casefold()
            if kind:
                result.setdefault(href, set()).update(re.split(r"[\s_-]+", kind))
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            soup = BeautifulSoup(
                item.get_content().decode("utf-8", errors="replace"),
                "html.parser",
            )
            for anchor in soup.find_all("a", href=True):
                values = [anchor.attrs.get("epub:type"), anchor.attrs.get("role")]
                tokens = {
                    token for value in values if value for token in re.split(r"[\s_-]+", str(value).casefold()) if token
                }
                if not tokens:
                    continue
                href = str(anchor["href"]).split("#", 1)[0].replace("\\", "/")
                if href:
                    result.setdefault(href, set()).update(tokens)
        return result

    @staticmethod
    def _normalize_chapter_title(toc_title: str, heading_title: str) -> str:
        """Combine or normalize TOC titles and heading titles into clean human-readable names."""
        clean_toc = str(toc_title or "").strip()
        clean_heading = str(heading_title or "").strip()
        if not clean_toc:
            return clean_heading
        # If TOC has "Chapter 1: Title", convert to "1: Title"
        normalized = re.sub(r"(?i)^chapter\s+(\d+\s*[:\-])", r"\1", clean_toc)
        # If TOC is "Chapter 1. Title", convert to "1: Title"
        normalized = re.sub(r"(?i)^chapter\s+(\d+)\.\s*", r"\1: ", normalized)
        # If heading is just digits "1" or short, prefer richer TOC title
        if clean_heading.isdigit() or len(clean_heading) <= 3:
            return normalized
        return normalized or clean_heading

    def _extract_chapters(self, book: epub.EpubBook) -> tuple[list[dict], dict[str, str]]:
        """Extract chapters and reference materials from EPUB documents.

        Returns a tuple of (raw_chapters, reference_materials).
        """
        # Get spine items (reading order)
        spine_entries = list(book.spine)
        toc_labels = self._toc_labels(book)
        navigation_semantics = self._navigation_semantics(book)
        items_by_id: dict[str, epub.EpubItem] = {}
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            items_by_id[item.get_id()] = item

        raw_chapters: list[dict] = []
        reference_material: dict[str, str] = {}

        for item_id, linear in spine_entries:
            item = items_by_id.get(item_id)
            if item is None:
                self.last_audit["sections"].append(
                    {
                        "item_id": str(item_id),
                        "href": "",
                        "title": str(item_id),
                        "word_count": 0,
                        "semantics": [],
                        "decision": "exclude",
                        "confidence": 0.0,
                        "reason": "Spine item could not be resolved from the EPUB manifest",
                        "review_required": True,
                        "linear": str(linear).casefold() != "no",
                    }
                )
                continue

            html_content = item.get_content().decode("utf-8", errors="replace")
            soup = BeautifulSoup(html_content, "html.parser")
            self._strip_non_narrative_markup(soup)

            markup_title = self._get_document_title(soup)
            href_key = str(item.get_name()).split("#", 1)[0].replace("\\", "/")
            doc_title = markup_title or toc_labels.get(href_key, "")
            classification = self._classify_document(
                item,
                soup,
                doc_title,
                navigation_semantics.get(href_key, set()),
            )
            classification["title_source"] = "markup" if markup_title else "navigation" if doc_title else "filename"
            classification["linear"] = str(linear).casefold() != "no"
            if (
                str(linear).casefold() == "no"
                and item_id not in self._section_overrides
                and classification["decision"] == "include"
            ):
                classification.update(
                    {
                        "decision": "exclude",
                        "confidence": 0.86,
                        "reason": "EPUB spine marks this document non-linear",
                        "review_required": classification["word_count"] >= 300,
                    }
                )
            self.last_audit["sections"].append(classification)
            classification["extracted_word_count"] = 0
            classification["coverage_ratio"] = 0.0

            if classification["decision"] == "reference":
                raw_ref_text = self._extract_text(soup)
                cleaned_ref = self.cleaner.clean(
                    raw_ref_text,
                    repeated_headers=self._repeated_headers,
                )
                if cleaned_ref.strip():
                    reference_material[doc_title or item.get_name()] = cleaned_ref
                    logger.info("Captured reference section: '%s' (%d words)", doc_title, len(cleaned_ref.split()))
                classification["extracted_word_count"] = len(cleaned_ref.split())
                classified_words = int(classification.get("word_count", 0) or 0)
                classification["coverage_ratio"] = (
                    round(
                        classification["extracted_word_count"] / classified_words,
                        4,
                    )
                    if classified_words
                    else 1.0
                )
                classification["drop_reason"] = "reference_material"
                continue

            if classification["decision"] == "exclude":
                classification["drop_reason"] = "classified_exclude"
                logger.debug("Skipping document: %s", item.get_name())
                continue

            had_headings = bool(soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]))
            if self.chapter_detection == "heading":
                chapters, ref_sections = self._split_by_headings(soup)
            elif self.chapter_detection == "pattern":
                chapters = self._split_by_patterns(soup)
                ref_sections = {}
            elif self.chapter_detection == "none":
                chapters = [{"title": "", "text": self._extract_text(soup)}]
                ref_sections = {}
            else:  # "auto"
                chapters, ref_sections = self._split_by_headings(soup)
                if not chapters and not had_headings:
                    chapters = self._split_by_patterns(soup)

            reference_material.update(ref_sections)
            if not chapters and self.chapter_detection != "none" and not ref_sections:
                text = self._extract_text(soup)
                if text.strip():
                    classification["extracted_word_count"] = len(text.split())
                    if raw_chapters:
                        raw_chapters[-1]["text"] += "\n\n" + text
                    else:
                        chapters = [{"title": "", "text": text}]
                        raw_chapters.extend(chapters)
            else:
                if len(chapters) == 1 and toc_labels.get(href_key):
                    chapters[0]["title"] = self._normalize_chapter_title(
                        toc_labels[href_key], chapters[0].get("title", "")
                    )
                raw_chapters.extend(chapters)
                classification["extracted_word_count"] = sum(
                    len(str(chapter.get("text") or "").split()) for chapter in chapters
                )

            classified_words = int(classification.get("word_count", 0) or 0)
            extracted_words = int(classification.get("extracted_word_count", 0) or 0)
            classification["coverage_ratio"] = round(extracted_words / classified_words, 4) if classified_words else 1.0
            if (
                classification.get("decision") == "include"
                and classified_words >= self.min_chapter_words
                and extracted_words == 0
            ):
                classification["review_required"] = True
                classification.setdefault("anomalies", []).append("included_document_without_extracted_text")
                classification["reason"] = (
                    f"{classification.get('reason', 'Included locally')}; included document produced no narrative text"
                )
                classification["drop_reason"] = "included_without_extracted_text"

        return raw_chapters, reference_material

    def _should_skip_document(self, soup: BeautifulSoup) -> bool:
        """Check if this HTML document should be skipped (ToC, appendix, etc.)."""
        # Get the document title from headings or title tag
        title_text = self._get_document_title(soup)

        if not title_text:
            # Check full text if very short (likely front/back matter)
            full_text = soup.get_text(strip=True)
            if len(full_text) < 50:
                return True
            return False

        return self._is_skippable_title(title_text)

    def _split_by_headings(self, soup: BeautifulSoup) -> tuple[list[dict], dict[str, str]]:
        """Split a document into chapters using HTML heading tags."""
        chapters: list[dict] = []
        reference_sections: dict[str, str] = {}
        heading_tags = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])

        if not heading_tags:
            return [], {}

        preamble_parts: list[str] = []
        for sibling in reversed(heading_tags[0].find_previous_siblings()):
            if isinstance(sibling, Tag):
                text = self._extract_element_text(sibling)
                if text:
                    preamble_parts.append(text)
        preamble = "\n\n".join(preamble_parts)
        preamble_attached = False
        i = 0
        while i < len(heading_tags):
            heading = heading_tags[i]
            title = heading.get_text(strip=True)
            subtitle_parts: list[str] = []

            # Look ahead for immediately consecutive headings with no narrative content in between.
            # The primary heading becomes the chapter title; any consecutive sub-headings are
            # treated as time-marker / exposition text that the narrator reads as chapter content.
            while i + 1 < len(heading_tags):
                next_heading = heading_tags[i + 1]
                has_intervening_content = False
                cur = heading.next_sibling
                while cur is not None and cur != next_heading:
                    if isinstance(cur, Tag):
                        if self._extract_element_text(cur).strip():
                            has_intervening_content = True
                            break
                    cur = cur.next_sibling

                if not has_intervening_content:
                    next_text = next_heading.get_text(strip=True)
                    if next_text:
                        subtitle_parts.append(next_text)
                    i += 1
                    heading = next_heading
                else:
                    break

            # Collect all body content after the last consecutive heading
            content_parts: list[str] = []
            # Prepend any subtitle/time-marker text so the narrator reads it
            if subtitle_parts:
                content_parts.extend(subtitle_parts)
            sibling = heading.next_sibling

            while sibling is not None:
                if isinstance(sibling, Tag) and sibling.name in ["h1", "h2", "h3", "h4", "h5", "h6"]:
                    break
                if isinstance(sibling, Tag):
                    text = self._extract_element_text(sibling)
                    if text:
                        content_parts.append(text)
                sibling = sibling.next_sibling

            text = "\n\n".join(content_parts)

            # Capture reference material (glossary, dramatis personae)
            if self.skip_appendices and self._is_reference_title(title):
                cleaned_ref = self.cleaner.clean(
                    text,
                    repeated_headers=self._repeated_headers,
                )
                if cleaned_ref.strip():
                    reference_sections[title] = cleaned_ref
                i += 1
                continue

            # Skip if title matches a skip pattern
            if self._is_skippable_title(title):
                i += 1
                continue

            if text.strip():
                if preamble and not preamble_attached:
                    text = preamble + "\n\n" + text
                    preamble_attached = True
                chapters.append({"title": title, "text": text})

            i += 1

        return chapters, reference_sections

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
            if self._is_skippable_title(title):
                continue
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
        block_names = {"p", "div", "blockquote", "pre"}
        for element in soup.find_all(list(block_names)):
            # Prefer semantic leaf blocks. A container div is used only when it
            # has no descendant block, preserving paragraph boundaries without
            # duplicating nested text.
            if element.name == "div" and element.find(list(block_names - {"div"})):
                continue
            if element.name != "div" and any(
                isinstance(parent, Tag) and parent.name in {"p", "blockquote", "pre"} for parent in element.parents
            ):
                continue
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
        pending_short: list[tuple[str, str]] = []

        for raw in raw_chapters:
            title_raw = raw.get("title", "")
            if self._is_skippable_title(title_raw):
                logger.info("Skipping non-narrative section: '%s'", title_raw)
                continue

            text = self.cleaner.clean(
                raw["text"],
                repeated_headers=self._repeated_headers,
            )
            if not text:
                continue

            word_count = len(text.split())

            if word_count < self.min_chapter_words and not self._looks_narrative_title(title_raw):
                pending_short.append((title_raw, text))
                logger.info(
                    "Preserving short section '%s' (%d words) for adjacent merge",
                    title_raw,
                    word_count,
                )
                continue

            if pending_short:
                prefix = "\n\n".join(part for _, part in pending_short)
                text = prefix + "\n\n" + text
                word_count = len(text.split())
                pending_short.clear()

            # Split chapters that are too long
            if word_count > self.max_chapter_words:
                sub_chapters = self._split_long_chapter(raw["title"], text, chapter_number)
                finalized.extend(sub_chapters)
                chapter_number += len(sub_chapters)
                continue

            chapter_number += 1
            title = raw["title"] or f"Chapter {chapter_number}"

            finalized.append(
                ExtractedChapter(
                    number=chapter_number,
                    title=title,
                    source_heading=title_raw,
                    book_chapter_label=(
                        title_raw
                        if re.match(
                            r"^(?:chapter|ch\.?|part|prologue|epilogue|interlude)\b",
                            title_raw,
                            re.IGNORECASE,
                        )
                        else ""
                    ),
                    text=text,
                    word_count=word_count,
                )
            )

        if pending_short:
            suffix = "\n\n".join(part for _, part in pending_short)
            if finalized:
                previous = finalized[-1]
                merged = previous.text + "\n\n" + suffix
                finalized[-1] = previous.model_copy(
                    update={
                        "text": merged,
                        "word_count": len(merged.split()),
                    }
                )
            elif suffix.strip():
                finalized.append(
                    ExtractedChapter(
                        number=1,
                        title=pending_short[0][0] or "Full Text",
                        source_heading=pending_short[0][0],
                        text=suffix,
                        word_count=len(suffix.split()),
                    )
                )

        # If no chapters were found, treat the entire content as one chapter
        if not finalized and raw_chapters:
            all_text = "\n\n".join(self.cleaner.clean(r["text"]) for r in raw_chapters if r["text"])
            if all_text.strip():
                logger.warning("No chapter boundaries detected — treating entire book as one chapter")
                finalized.append(
                    ExtractedChapter(
                        number=1,
                        title="Full Text",
                        source_heading="",
                        text=all_text,
                        word_count=len(all_text.split()),
                    )
                )

        return finalized

    def _split_long_chapter(self, title: str, text: str, start_number: int) -> list[ExtractedChapter]:
        """Split a chapter that exceeds max_chapter_words."""
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
        chunks: list[str] = []
        current: list[str] = []
        current_words = 0
        for paragraph in paragraphs:
            paragraph_words = paragraph.split()
            if current and current_words + len(paragraph_words) > self.max_chapter_words:
                chunks.append("\n\n".join(current))
                current, current_words = [], 0
            while len(paragraph_words) > self.max_chapter_words:
                # A malformed EPUB may contain one enormous paragraph. Prefer
                # the last sentence boundary within the limit, then words.
                window = paragraph_words[: self.max_chapter_words]
                cut = max(
                    (index + 1 for index, word in enumerate(window) if re.search(r"[.!?][\"']?$", word)),
                    default=self.max_chapter_words,
                )
                chunks.append(" ".join(paragraph_words[:cut]))
                paragraph_words = paragraph_words[cut:]
            if paragraph_words:
                current.append(" ".join(paragraph_words))
                current_words += len(paragraph_words)
        if current:
            chunks.append("\n\n".join(current))

        parts: list[ExtractedChapter] = []
        for part_num, chunk_text in enumerate(chunks, 1):
            part_title = f"{title} (Part {part_num})" if title else f"Chapter {start_number + part_num}"
            word_count = len(chunk_text.split())

            parts.append(
                ExtractedChapter(
                    number=start_number + part_num,
                    title=part_title,
                    source_heading=title,
                    book_chapter_label=(
                        title
                        if re.match(
                            r"^(?:chapter|ch\.?|part|prologue|epilogue|interlude)\b",
                            title,
                            re.IGNORECASE,
                        )
                        else ""
                    ),
                    text=chunk_text.strip(),
                    word_count=word_count,
                )
            )

        return parts
