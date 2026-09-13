"""A trend warning must describe what was measured.

`quality_trends` emits three within-chapter kinds, and all three fire on *too
much* variation:

    within_chapter_pitch_variation   pitch spread   >= PITCH_VARIATION_WARN_RATIO
    within_chapter_rate_variation    rate spread    >= RATE_VARIATION_WARN_RATIO
    within_chapter_pitch_jump        adjacent jump  >= PITCH_JUMP_WARN_RATIO

The review gate described every one of them as "flagged as monotone", which is
the opposite. On `the-finest-edge-of-twilight` that was 47 items, 11 of them on
the narrator, all telling the operator to go and listen for a problem that was
not the one detected -- and it misread the same way on the way in.
"""

from __future__ import annotations

import pytest

from brain.orchestrator.review_gate import _TREND_TITLES, _trend_reason

WITHIN_CHAPTER = [
    "within_chapter_pitch_variation",
    "within_chapter_rate_variation",
    "within_chapter_pitch_jump",
]

WARNING = {
    "pitch_relative_spread": 0.459525,
    "speaking_rate_relative_spread": 0.34296,
    "largest_adjacent_pitch_jump_ratio": 1.81,
}


@pytest.mark.parametrize("kind", WITHIN_CHAPTER)
def test_no_within_chapter_warning_claims_monotone(kind) -> None:
    text = f"{_TREND_TITLES[kind]} {_trend_reason(kind, WARNING)}".lower()
    assert "monotone" not in text
    assert "listen before final release" in text


@pytest.mark.parametrize(
    ("kind", "fragment"),
    [
        ("within_chapter_pitch_variation", "pitch varies by 46%"),
        ("within_chapter_rate_variation", "speaking rate varies by 34%"),
        ("within_chapter_pitch_jump", "pitch jumps 181%"),
    ],
)
def test_the_measured_number_is_quoted(kind, fragment) -> None:
    """A bare "looks uneven" cannot be triaged; the number can."""
    assert fragment in _trend_reason(kind, WARNING)


def test_each_kind_gets_its_own_title() -> None:
    titles = [_TREND_TITLES[k] for k in WITHIN_CHAPTER]
    assert len(set(titles)) == len(titles)


def test_cross_chapter_drift_keeps_its_own_wording() -> None:
    reason = _trend_reason("cross_chapter_voice_drift", {})
    assert "book-wide identity baseline" in reason
    assert "monotone" not in reason.lower()


def test_a_missing_metric_degrades_instead_of_crashing() -> None:
    reason = _trend_reason("within_chapter_pitch_jump", {})
    assert "widely" in reason
    assert "%" not in reason


def test_an_unknown_kind_is_still_readable() -> None:
    assert "uneven" in _trend_reason("something_new", {}).lower()
    assert _TREND_TITLES.get("something_new") is None
