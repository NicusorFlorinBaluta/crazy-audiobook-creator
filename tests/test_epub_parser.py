from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup
from ebooklib import epub

from brain.extractor.epub_parser import EpubParser
from brain.extractor.text_cleaner import TextCleaner


class EpubParserFilteringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser_skip_preface = EpubParser(skip_preface=True, min_chapter_words=5)
        self.parser_keep_preface = EpubParser(skip_preface=False, min_chapter_words=5)

    def test_skippable_title_matches_backmatter_and_metadata(self) -> None:
        skippable_titles = [
            "About the Author",
            "About the Illustrators",
            "About the Artist",
            "About the Publisher",
            "Table of Contents",
            "Contents",
            "Appendix A: The Magic System",
            "Appendices",
            "Addendum 1: Historical Notes",
            "Addenda",
            "Glossary of Terms",
            "Dramatis Personae",
            "Character List",
            "Maps and Illustrations",
            "Acknowledgments",
            "Dedication",
            "Copyright",
            "Colophon",
            "Books by Brandon Sanderson",
            "Also by the Author",
            "Preview of Book 6",
            "Excerpt from Next Novel",
            "Bonus Material",
            "Sneak Peek",
            "Reading Group Guide",
            "Discussion Questions",
            "Newsletter Signup",
            "Praise for Isles of the Emberdark",
            "Advance Praise",
        ]
        for title in skippable_titles:
            self.assertTrue(
                self.parser_skip_preface._is_skippable_title(title),
                f"Expected '{title}' to be recognized as skippable",
            )
            self.assertTrue(
                self.parser_keep_preface._is_skippable_title(title),
                f"Expected '{title}' to be recognized as skippable even with skip_preface=False",
            )

    def test_preface_and_author_notes_toggle(self) -> None:
        preface_titles = [
            "Preface",
            "Foreword",
            "Author's Note",
            "Author Note",
            "Afterword",
            "Postscript",
        ]
        for title in preface_titles:
            self.assertTrue(
                self.parser_skip_preface._is_skippable_title(title),
                f"Expected '{title}' to be skippable when skip_preface=True",
            )
            self.assertFalse(
                self.parser_keep_preface._is_skippable_title(title),
                f"Expected '{title}' to NOT be skippable when skip_preface=False",
            )

    def test_narrative_chapters_are_not_skipped(self) -> None:
        story_titles = [
            "Prologue",
            "Chapter 1",
            "Chapter One",
            "Chapter Fifty-Four",
            "Interlude: The Lighthouse",
            "Epilogue",
            "Part 1: The Gathering",
        ]
        for title in story_titles:
            self.assertFalse(
                self.parser_skip_preface._is_skippable_title(title),
                f"Expected narrative title '{title}' to NOT be skipped",
            )
            self.assertFalse(
                self.parser_keep_preface._is_skippable_title(title),
                f"Expected narrative title '{title}' to NOT be skipped",
            )

    def test_finalize_chapters_skips_matched_titles(self) -> None:
        raw_chapters = [
            {"title": "Preface", "text": "This is author commentary explaining the novella."},
            {"title": "Prologue", "text": "The dragon transformed on the grand balcony under first light."},
            {"title": "Chapter 1", "text": "Sixth of Dusk walked along the beach carrying Sak on his shoulder."},
            {"title": "Epilogue", "text": "The journey ended and peace returned to the islands."},
            {
                "title": "About the Illustrators",
                "text": "The illustrations were created by talented artists worldwide.",
            },
        ]
        # With skip_preface=True
        chapters = self.parser_skip_preface._finalize_chapters(raw_chapters)
        titles = [c.title for c in chapters]
        self.assertEqual(titles, ["Prologue", "Chapter 1", "Epilogue"])
        self.assertEqual(chapters[0].number, 1)
        self.assertEqual(chapters[1].number, 2)
        self.assertEqual(chapters[2].number, 3)

        # With skip_preface=False
        chapters_with_preface = self.parser_keep_preface._finalize_chapters(raw_chapters)
        titles_with_preface = [c.title for c in chapters_with_preface]
        self.assertEqual(titles_with_preface, ["Preface", "Prologue", "Chapter 1", "Epilogue"])

    def test_reference_title_detection(self) -> None:
        ref_titles = [
            "Glossary",
            "Glossary of Terms",
            "Dramatis Personae",
            "Character List",
            "Cast of Characters",
            "List of Characters",
            "Names and Terms",
            "Appendix: Dramatis Personae",
            "Appendix B: Cast and Characters",
        ]
        for title in ref_titles:
            self.assertTrue(
                self.parser_skip_preface._is_reference_title(title),
                f"Expected '{title}' to be recognized as character reference material",
            )

    def test_split_by_headings_captures_reference_material(self) -> None:
        html = """
        <html>
            <body>
                <h1>Chapter 1: The Departure</h1>
                <p>Kvothe adjusted his cloak and looked at the horizon.</p>
                <h1>Glossary of Characters</h1>
                <p>Kvothe: An arcanist and musician of great renown. Gender: Male.</p>
                <p>Bast: A fae prince posing as an assistant. Gender: Male.</p>
                <h1>Chapter 2: The Waystone Inn</h1>
                <p>The night was quiet and three silences hung in the air.</p>
            </body>
        </html>
        """
        soup = BeautifulSoup(html, "lxml")
        chapters, ref_material = self.parser_skip_preface._split_by_headings(soup)

        # Reference material must be captured
        self.assertIn("Glossary of Characters", ref_material)
        self.assertIn("Kvothe: An arcanist", ref_material["Glossary of Characters"])
        self.assertIn("Bast: A fae prince", ref_material["Glossary of Characters"])

        # Narrative chapters must NOT include the glossary
        chapter_titles = [c["title"] for c in chapters]
        self.assertEqual(chapter_titles, ["Chapter 1: The Departure", "Chapter 2: The Waystone Inn"])

    def test_each_skip_flag_can_be_disabled_independently(self) -> None:
        parser = EpubParser(
            skip_toc=False,
            skip_appendices=False,
            skip_front_matter=False,
            skip_preface=False,
            min_chapter_words=1,
        )
        for title in ["Table of Contents", "Appendix A", "Dedication", "Preface"]:
            self.assertFalse(parser._is_skippable_title(title), title)

    def test_reference_heading_does_not_leak_through_document_fallback(self) -> None:
        soup = BeautifulSoup(
            "<html><body><h1>Glossary</h1><p>Vathi: a scholar.</p></body></html>",
            "lxml",
        )
        chapters, references = self.parser_skip_preface._split_by_headings(soup)
        self.assertEqual(chapters, [])
        self.assertIn("Glossary", references)

    def test_all_caps_story_text_is_preserved_but_known_running_header_is_removed(self) -> None:
        cleaner = TextCleaner()
        text = "A RUNNING HEADER\n\nRUN FOR YOUR LIFE\n\nThe gate shattered behind them."
        cleaned = cleaner.clean(text, repeated_headers={"a running header"})
        self.assertNotIn("A RUNNING HEADER", cleaned)
        self.assertIn("RUN FOR YOUR LIFE", cleaned)

    def test_short_narrative_is_kept_and_short_generic_section_is_merged(self) -> None:
        parser = EpubParser(min_chapter_words=10)
        chapters = parser._finalize_chapters(
            [
                {"title": "A small ornament", "text": "Three silver stars."},
                {"title": "Interlude: The Bell", "text": "It rang once."},
                {
                    "title": "Chapter 2",
                    "text": "Enough ordinary words follow here to make the main chapter comfortably long today.",
                },
            ]
        )
        self.assertEqual([chapter.title for chapter in chapters], ["Interlude: The Bell", "Chapter 2"])
        self.assertIn("Three silver stars", chapters[0].text)

    def test_long_split_prefers_paragraph_and_sentence_boundaries(self) -> None:
        parser = EpubParser(max_chapter_words=8, min_chapter_words=1)
        parts = parser._split_long_chapter(
            "Chapter One",
            "One two three four. Five six.\n\nSeven eight nine ten. Eleven twelve.",
            0,
        )
        self.assertGreaterEqual(len(parts), 2)
        self.assertTrue(all(part.word_count <= 8 for part in parts))
        self.assertTrue(parts[0].text.endswith("six."))


class EpubGoldenStructureTests(unittest.TestCase):
    """Exercise actual ZIP/container EPUBs, not only BeautifulSoup fragments."""

    @staticmethod
    def _write_book(path: Path, documents: list[dict]) -> None:
        book = epub.EpubBook()
        book.set_identifier(path.stem)
        book.set_title(f"Golden {path.stem}")
        book.set_language("en")
        book.add_author("Test Author")
        spine = ["nav"]
        toc = []
        for index, spec in enumerate(documents, 1):
            item = epub.EpubHtml(
                title=spec.get("nav_title", spec.get("title", f"Section {index}")),
                file_name=f"text/section-{index}.xhtml",
                lang="en",
            )
            item.set_content(spec["html"])
            book.add_item(item)
            spine.append((item, spec.get("linear", "yes")))
            toc.append(item)
        book.toc = tuple(toc)
        book.spine = spine
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        epub.write_epub(str(path), book)

    def test_ten_structural_variants_preserve_story_and_trim_non_story(self) -> None:
        variants = [
            [
                {
                    "title": "Chapter 1",
                    "html": "<h1>Chapter 1</h1><p>MARKER alpha story words continue beyond the threshold safely.</p>",
                }
            ],
            [
                {
                    "title": "Chapter 2",
                    "html": "<section><h3>Chapter 2</h3><p>MARKER beta story words continue beyond the threshold safely.</p></section>",
                }
            ],
            [
                {
                    "nav_title": "Chapter Three",
                    "html": "<div><p>MARKER gamma nav labelled narrative remains intact and readable.</p></div>",
                }
            ],
            [
                {
                    "title": "Prologue",
                    "html": "<h1>Prologue</h1><p>RUN FOR YOUR LIFE</p><p>MARKER delta story remains intact.</p>",
                }
            ],
            [{"title": "Interlude", "html": "<h2>Interlude</h2><p>MARKER epsilon.</p>"}],
            [
                {"title": "Copyright", "html": "<h1>Copyright</h1><p>TRIM-ME publishing boilerplate only.</p>"},
                {
                    "title": "Chapter 6",
                    "html": "<h1>Chapter 6</h1><p>MARKER zeta narrative words continue safely here.</p>",
                },
            ],
            [
                {
                    "title": "Chapter 7",
                    "html": "<h1>Chapter 7</h1><p>MARKER eta continued.</p><aside epub:type='footnote'>TRIM-ME footnote.</aside>",
                }
            ],
            [
                {
                    "title": "Chapter 8",
                    "html": "<h1>Chapter 8</h1><div><p>MARKER theta first paragraph.</p><p>Second paragraph is not duplicated.</p></div>",
                }
            ],
            [
                {
                    "title": "Chapter 9",
                    "html": "<p>Chapter 9</p><p>MARKER iota pattern based body remains available.</p>",
                }
            ],
            [
                {"title": "Chapter 10", "html": "<h1>Chapter 10</h1><p>MARKER kappa primary narrative remains.</p>"},
                {
                    "title": "Bonus",
                    "linear": "no",
                    "html": "<h2>Bonus</h2><p>TRIM-ME non linear promotional supplement.</p>",
                },
            ],
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, documents in enumerate(variants, 1):
                path = root / f"variant-{index}.epub"
                self._write_book(path, documents)
                parser = EpubParser(min_chapter_words=1)
                extracted = parser.parse(path)
                combined = "\n".join(chapter.text for chapter in extracted.chapters)
                self.assertIn("MARKER", combined, f"variant {index}")
                self.assertNotIn("TRIM-ME", combined, f"variant {index}")
                self.assertEqual(
                    parser.last_audit["summary"]["spine_documents"],
                    len(documents) + 1,  # generated EPUB nav is itself in the spine
                )

    def test_large_excluded_share_is_blocking_and_audited(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large-appendix.epub"
            large = " ".join(["reference"] * 1_100)
            self._write_book(
                path,
                [
                    {"title": "Chapter 1", "html": "<h1>Chapter 1</h1><p>MARKER short narrative remains here.</p>"},
                    {"title": "Appendix", "html": f"<h1>Appendix</h1><p>{large}</p>"},
                ],
            )
            parser = EpubParser(min_chapter_words=1)
            parser.parse(path)
            appendix = next(section for section in parser.last_audit["sections"] if section["title"] == "Appendix")
            self.assertTrue(appendix["review_required"])
            self.assertIn("large_excluded_word_ratio", appendix["anomalies"])
            self.assertGreater(parser.last_audit["anomalies"]["excluded_ratio"], 0.9)

    def test_nested_heading_wrapper_falls_back_without_dropping_story(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested-heading.epub"
            story = " ".join(["MARKER nested narrative remains intact"] * 30)
            self._write_book(
                path,
                [
                    {
                        "title": "Chapter One",
                        "html": (
                            f"<section><header><h1>Chapter One</h1></header><article><p>{story}</p></article></section>"
                        ),
                    }
                ],
            )
            parser = EpubParser(min_chapter_words=5)
            extracted = parser.parse(path)
            combined = "\n".join(chapter.text for chapter in extracted.chapters)
            self.assertIn("MARKER", combined)
            section = next(item for item in parser.last_audit["sections"] if item.get("title") == "Chapter One")
            self.assertGreater(section["extracted_word_count"], 0)
            self.assertGreater(section["coverage_ratio"], 0.5)


class BookTitleNormalizationTests(unittest.TestCase):
    """A title reaches the dashboard, the M4B metadata and the Android app.

    Separator debris from whatever produced the EPUB should be cleaned once at
    the source rather than in each consumer -- but conservatively, because a
    single interior separator is real punctuation in a great many titles.
    """

    def test_duplicate_separator_debris_is_collapsed(self) -> None:
        # Observed verbatim in this library.
        self.assertEqual(
            EpubParser._normalize_book_title("The Finest Edge of Twilight - - Book"),
            "The Finest Edge of Twilight - Book",
        )
        self.assertEqual(
            EpubParser._normalize_book_title("Title - : Subtitle"),
            "Title - Subtitle",
        )

    def test_a_single_interior_separator_is_preserved(self) -> None:
        for title in (
            "Dune - Book Two",
            "Isles of the Emberdark: A Cosmere Novel",
            "A Perfectly Normal Title",
        ):
            self.assertEqual(EpubParser._normalize_book_title(title), title)

    def test_an_intra_word_double_hyphen_is_left_alone(self) -> None:
        """`--` inside a word is an ASCII em-dash, not separator debris."""
        self.assertEqual(
            EpubParser._normalize_book_title("Wait--what? A Novel"),
            "Wait--what? A Novel",
        )

    def test_leading_and_trailing_separators_are_stripped(self) -> None:
        self.assertEqual(
            EpubParser._normalize_book_title("  - Leading and trailing -  "),
            "Leading and trailing",
        )

    def test_whitespace_runs_are_collapsed(self) -> None:
        self.assertEqual(
            EpubParser._normalize_book_title("Mixed   whitespace   title"),
            "Mixed whitespace title",
        )

    def test_a_title_of_only_separators_is_returned_unchanged(self) -> None:
        """Never normalise a title down to nothing; something is better."""
        self.assertEqual(EpubParser._normalize_book_title("---"), "---")

    def test_empty_input_stays_empty(self) -> None:
        self.assertEqual(EpubParser._normalize_book_title(""), "")


if __name__ == "__main__":
    unittest.main()
