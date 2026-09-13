"""Junk must not reach the lexicon, because the lexicon is applied unreviewed.

A recommendation's `default` is loaded as an active substitution and rewrites
`spoken_text`, which feeds the segment manifest's dependency hash. Nothing
gates it. So a term that has no pronunciation to get right is not a harmless
extra row in a review list -- it is a pending edit to the audio.

All three shapes here were live in `isles-of-the-emberdark` on 2026-09-12, and
the LLM had proposed a respelling for each: `You're` -> "Youre" over 79 lines,
`MW-` -> "MW", and `white-haired being` -> "whitehaired being".
"""

from __future__ import annotations

import pytest

from shared.pronunciation import (
    _is_pronunciation_noise,
    _pronunciation_base_form,
    get_english_dictionary,
)


@pytest.fixture(scope="module")
def english() -> set[str]:
    words = get_english_dictionary()
    if not words:
        pytest.skip("the bundled English word list is unavailable")
    return words


class TestContractions:
    def test_a_capitalised_contraction_is_not_a_name(self, english) -> None:
        """ "You're" leads a quotation, so it looks capitalised and mid-sentence."""
        assert _pronunciation_base_form("You're").casefold() in english

    @pytest.mark.parametrize(
        "term",
        ["You're", "They've", "We'll", "I'm", "Don't", "She'd", "Ship's"],
    )
    def test_every_contraction_tail_is_stripped(self, term, english) -> None:
        assert _pronunciation_base_form(term).casefold() in english, term

    def test_an_apostrophe_inside_a_name_is_kept(self) -> None:
        """`Do'Urden` and `Syn'Dalay` are names, not contractions."""
        assert _pronunciation_base_form("Do'Urden") == "Do'Urden"
        assert _pronunciation_base_form("Syn'Dalay") == "Syn'Dalay"

    def test_a_name_ending_in_a_contraction_tail_is_not_gutted(self) -> None:
        """ "Sos'umptu" does not end in one; "B'shett" must survive too."""
        assert _pronunciation_base_form("Sos'umptu") == "Sos'umptu"
        assert _pronunciation_base_form("B'shett") == "B'shett"


class TestExtractorFragments:
    @pytest.mark.parametrize("term", ["MW-", "mw-", "-ish", "A-", "x"])
    def test_a_fragment_has_no_pronunciation_to_fix(self, term, english) -> None:
        assert _is_pronunciation_noise(term, english)

    def test_a_short_real_name_is_kept(self, english) -> None:
        """The length floor must not eat `Guen`, `Nol` or `Sori`."""
        for name in ("Guen", "Nol", "Sori", "Jax"):
            assert not _is_pronunciation_noise(name, english), name


class TestEnglishDescriptorPhrases:
    def test_a_cast_descriptor_made_of_english_words_is_not_a_risk(self, english) -> None:
        """Emberdark casts "White-Haired Being"; a cast alias skips the
        dictionary check, so the whole phrase became a lexicon candidate."""
        assert _is_pronunciation_noise("White-Haired Being", english)

    @pytest.mark.parametrize(
        "descriptor",
        ["Deep Voice", "Police Officer", "Husband of the Family", "the man"],
    )
    def test_other_descriptor_casts_are_rejected_too(self, descriptor, english) -> None:
        assert _is_pronunciation_noise(descriptor, english)

    def test_a_two_word_name_with_a_foreign_part_is_kept(self, english) -> None:
        """`Braelin Janquay` is the case that most needed a respelling."""
        assert not _is_pronunciation_noise("Braelin Janquay", english)

    def test_a_hyphenated_name_is_kept(self, english) -> None:
        """`Catti-brie` and `Aegis-fang` split on the hyphen; neither half is English."""
        assert not _is_pronunciation_noise("Catti-brie", english)
        assert not _is_pronunciation_noise("Aegis-fang", english)

    def test_a_half_english_compound_is_kept(self, english) -> None:
        """ "Ten-Towns" is half English, and the engine says it correctly -- but
        that is a decision for measurement, not for this filter."""
        assert not _is_pronunciation_noise("Ten-Towns", english)


def test_the_filter_is_inert_without_a_dictionary(monkeypatch) -> None:
    """A missing word list must not start rejecting real names."""
    assert not _is_pronunciation_noise("White-Haired Being", set())
    assert _is_pronunciation_noise("MW-", set()), "shape checks still apply"
