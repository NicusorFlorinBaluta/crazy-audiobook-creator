from __future__ import annotations

import unittest
from pathlib import Path
from bs4 import BeautifulSoup

from brain.extractor.epub_parser import EpubParser, SKIP_TITLE_PATTERNS, PREFACE_TITLE_PATTERNS


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
            {"title": "About the Illustrators", "text": "The illustrations were created by talented artists worldwide."},
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


if __name__ == "__main__":
    unittest.main()
