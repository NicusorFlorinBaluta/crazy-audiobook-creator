"""An inbox where every entry is advisory teaches you to ignore it.

`the-finest-edge-of-twilight` reported 291 review items and zero blocking. 173
of them were unverified pronunciations -- names the engine was audibly getting
wrong, "Catti-brie" read aloud as "Cadbury" -- sitting behind 83 observations
that change nothing, and the UI defaulted to "All statuses" because nothing was
blocking. The entries that mattered were the ones being ignored.

`changes_output` splits the two: acting on an attribution, a pronunciation, an
audio segment or an extraction alters the audio that gets produced. A voice
trend or a character note is worth reading and never worth acting on
mechanically.
"""

from __future__ import annotations

import pytest

from brain.orchestrator.review_gate import (
    _CHANGES_OUTPUT_CATEGORIES,
    RESOLVED_ATTRIBUTION_DISPOSITIONS,
    ReviewGate,
    ReviewItem,
)


def _item(category: str, item_id: str, *, blocking: bool = False, disposition: str = "unreviewed") -> ReviewItem:
    return ReviewItem(
        category=category,
        item_id=item_id,
        title=f"{category} {item_id}",
        reason="because",
        blocking=blocking,
        disposition=disposition,
    )


@pytest.mark.parametrize("category", ["attribution", "pronunciation", "audio", "extraction"])
def test_categories_that_change_the_audio_are_actionable(category) -> None:
    gate = ReviewGate(items=(_item(category, "x1"),))
    assert gate.to_dict()["actionable_count"] == 1
    assert gate.to_dict()["items"][0]["changes_output"] is True


@pytest.mark.parametrize("category", ["audio_trend", "character"])
def test_advisory_categories_are_not(category) -> None:
    gate = ReviewGate(items=(_item(category, "x1"),))
    assert gate.to_dict()["actionable_count"] == 0
    assert gate.to_dict()["items"][0]["changes_output"] is False


@pytest.mark.parametrize("disposition", sorted(RESOLVED_ATTRIBUTION_DISPOSITIONS))
def test_a_resolved_item_stops_being_actionable(disposition) -> None:
    """Acting on it is done; it should not keep asking."""
    gate = ReviewGate(items=(_item("pronunciation", "x1", disposition=disposition),))
    assert gate.to_dict()["actionable_count"] == 0


def test_the_twilight_shape() -> None:
    """291 items, 0 blocking, and the 173 that matter buried in the middle."""
    items = tuple(
        [_item("pronunciation", f"p{i}") for i in range(173)]
        + [_item("audio_trend", f"t{i}") for i in range(47)]
        + [_item("character", f"c{i}") for i in range(36)]
        + [_item("audio", f"a{i}", disposition="acceptable") for i in range(25)]
        + [_item("audio", f"b{i}") for i in range(10)]
    )
    summary = ReviewGate(items=items).to_dict()
    assert summary["total_count"] == 291
    assert summary["blocking_count"] == 0
    assert summary["release_ready"] is True
    assert summary["actionable_count"] == 183, "173 pronunciations + 10 unreviewed audio"


def test_blocking_is_still_reported_separately() -> None:
    gate = ReviewGate(items=(_item("attribution", "x1", blocking=True), _item("audio_trend", "t1")))
    summary = gate.to_dict()
    assert summary["blocking_count"] == 1
    assert summary["actionable_count"] == 1
    assert summary["release_ready"] is False


def test_the_advisory_set_is_the_complement_of_the_actionable_one() -> None:
    """A new category defaults to advisory, which is the safe direction."""
    assert "audio_trend" not in _CHANGES_OUTPUT_CATEGORIES
    assert "character" not in _CHANGES_OUTPUT_CATEGORIES
    assert ReviewGate(items=(_item("something_new", "x1"),)).to_dict()["actionable_count"] == 0


class TestTheUiLandsOnSomethingUseful:
    """The default filter is chosen in app.js from these two counts."""

    def test_the_filter_offers_the_distinction(self) -> None:
        from pathlib import Path

        html = Path("brain/dashboard/frontend/index.html").read_text(encoding="utf-8")
        assert 'value="changes"' in html

    def test_the_default_prefers_blocking_then_actionable(self) -> None:
        from pathlib import Path

        app = Path("brain/dashboard/frontend/js/app.js").read_text(encoding="utf-8")
        assert "actionable_count" in app
        assert "'changes'" in app
