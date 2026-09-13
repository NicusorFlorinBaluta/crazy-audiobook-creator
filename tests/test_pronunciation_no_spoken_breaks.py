"""A respelling may not contain a break the engine will speak.

The TTS acts on hyphens *and* spaces: both come out as an audible word break.
So a respelling that marks syllables with either is worse than none at all --
`Gut-bus-ters` is spoken as three words, not as "Gutbusters".

`normalize_phonetic_text` used to preserve hyphens deliberately, on the theory
that only spaces caused pauses. That is backwards, and it is why all 184 stored
recommendations for `the-finest-edge-of-twilight` were unusable while Whisper
transcribed the synthesised audio as:

    Catti-brie -> "Cadbury" / "Caddy Breeze"      Jarlaxle  -> "Jarl Axel"
    Kimmuriel  -> "Kim Oriel" / "Kim Wario"       Do'Urden  -> "de Worden"

Note `Catti-brie`: the hyphen is in the *source term*, so the engine splits the
name before any respelling is involved. A correct respelling has to collapse it
to one token.

Spaces already in a term are left alone -- a two-word name is two words, and
that break belongs to the author, not the respeller.
"""

from __future__ import annotations

import pytest

from shared.pronunciation import (
    apply_pronunciations,
    generate_phonetic_recommendations,
    normalize_phonetic_text,
)

#: Real terms from the library, including every hyphen shape that occurs.
TERMS = [
    "Catti-brie",
    "Aegis-fang",
    "Ten-Towns",
    "Caer-Konig",
    "Jarlaxle",
    "Kimmuriel",
    "Kokerlii",
    "Homeisle",
    "Gutbusters",
    "Callidaean",
    "Do'Urden",
    "Braelin Janquay",
    "Uncle Jax",
    "Ghaliver Longstocking",
]


@pytest.mark.parametrize("term", TERMS)
def test_no_recommendation_contains_a_hyphen(term) -> None:
    rec = generate_phonetic_recommendations(term)
    assert "-" not in rec["default"], f"{term} -> {rec['default']!r}"
    assert "-" not in rec["alternate"], f"{term} -> {rec['alternate']!r}"


@pytest.mark.parametrize("term", TERMS)
def test_word_count_is_preserved(term) -> None:
    """A one-word term stays one word; a two-word name stays two."""
    expected = len(term.split())
    rec = generate_phonetic_recommendations(term)
    assert len(rec["default"].split()) == expected, f"{term} -> {rec['default']!r}"
    if rec["alternate"]:
        assert len(rec["alternate"].split()) == expected, f"{term} -> {rec['alternate']!r}"


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("Gut-bus-ters", "Gutbusters"),
        ("Cal-li-daean", "Callidaean"),
        ("Koh-ker-lee", "Kohkerlee"),
        ("home-aisle", "homeaisle"),
        ("Kat - tee - bree", "Katteebree"),
        ("Kah–tee—bree", "Kahteebree"),  # en dash and em dash
        ("Braelin Yanquay", "Braelin Yanquay"),  # a real word break survives
    ],
)
def test_stored_hyphens_are_joined_out_on_read(stored, expected) -> None:
    """The 184 recommendations already on disk are corrected without a migration."""
    assert normalize_phonetic_text(stored) == expected


def test_substitution_never_injects_a_break() -> None:
    """Even a hand-typed hyphenated mapping reaches the engine as one word."""
    mapping = {"catti-brie": "Katty-bree", "jarlaxle": "Jar-lax-ul"}
    assert apply_pronunciations("Catti-brie nodded.", mapping) == "Kattybree nodded."
    assert apply_pronunciations("Jarlaxle smiled.", mapping) == "Jarlaxul smiled."


def test_a_term_that_already_reads_correctly_is_left_alone() -> None:
    rec = generate_phonetic_recommendations("Gutbusters")
    assert rec["default"] == "Gutbusters"


class TestThePromptStatesTheConstraint:
    """The rule has to reach the model, not only the post-processing."""

    def test_the_prompt_forbids_hyphens_and_explains_why(self) -> None:
        from shared.pronunciation import _PRONUNCIATION_PROMPT_HEADER as header

        assert "never write a hyphen" in header
        assert "never add a space" in header
        assert "break" in header
        # The worked examples must themselves obey the rule.
        for line in header.splitlines():
            if "-> default" in line:
                respelling = line.split('"')[3]
                assert "-" not in respelling, line

    def test_a_model_answer_with_the_wrong_word_count_is_refused(self) -> None:
        from shared.pronunciation import _clean_rec

        # Splitting a one-word term invents a break the engine speaks.
        assert " " not in _clean_rec("Catti-brie", "Catty Bree")
        # Squashing a two-word name loses one the author wrote.
        assert len(_clean_rec("Uncle Jax", "UncleYax").split()) == 2
