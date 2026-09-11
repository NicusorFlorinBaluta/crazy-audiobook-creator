"""A deterministic rename leaves its neighbours stale, and nothing looked.

`ch24_0096`/`ch24_0098` of the-finest-edge-of-twilight are one rhyming couplet
by Athrogate, both stored as Jarlaxle. The named tag on `ch24_0095` repairs the
first half. The second half stays wrong, because the detector's
narrator-separated-collapse pattern requires the pair to lack continuation
evidence -- and *"He looked to Jarlaxle as he continued,"* **is** continuation
evidence. Correct for spotting a collapse, exactly wrong here.

Asking is the whole fix: the local model answers `ch24_0098` correctly 3 of 3 at
0.95 on the narrow window. Measured alternatives, both rejected:

* **wider context** -- no better. Narrow was 3/3; wide was 2/3 on the state as
  shipped, one run answering `jarlaxle`.
* **continuation propagation** ("across a continuation tag the two quotes share
  a speaker") -- 84 pairs across both books, 61 of them backward-pointing tags
  like *"he added."* that attach to the quote above. Of the 23 forward-pointing
  ones exactly one disagreed, and it was a false positive: `ch09_0291` is cut
  mid-sentence and `ch09_0294` completes it, so the subject of "added" is
  Jarlaxle and the stored speaker was right.
* **every neighbour of a deterministic attribution** -- 51 lines across both
  books, 49 re-confirmed, none improved. Ordinary alternation.

Keyed on the rename instead: staleness is created by the change, so the change
is what triggers the second look.
"""

from __future__ import annotations

import pytest

from brain.director.attribution_detector import detect_suspicious_turns, neighbours_of_reattributed
from shared.models import ScriptChapter, ScriptLine


def _chapter(rows: list[tuple[str, str, str]]) -> ScriptChapter:
    return ScriptChapter(
        chapter_number=24,
        chapter_title="Chapter 24",
        scenes=[],
        lines=[
            ScriptLine(line_id=lid, speaker=speaker, text=text, speaker_confidence=0.95)
            for lid, speaker, text in rows
        ],
    )


@pytest.fixture
def couplet() -> ScriptChapter:
    """The real passage, with ch24_0096 already repaired to athrogate."""
    return _chapter([
        ("ch24_0094", "athrogate", '"Really, me King, might we\'d\'ve expected less mischief from this one?"'),
        ("ch24_0095", "narrator", "said Athrogate, and he bounded over between Breezy and her parents and burst into rhyme."),
        ("ch24_0096", "athrogate", '"Well, hey-ho, but their girl\'s a spitfire! A clever young lass and a bit of a liar."'),
        ("ch24_0097", "narrator", "He looked to Jarlaxle as he continued,"),
        ("ch24_0098", "jarlaxle", '"With proper taste and a feathery flair, that\'s sure to land her in a mad dragon\'s lair!"'),
        ("ch24_0099", "pwent", '"Might that that\'ll get them two ma and da out and fightin\', eh me King?"'),
    ])


def test_the_detector_alone_does_not_see_it(couplet) -> None:
    """The premise. Continuation evidence suppresses the only pattern that fits."""
    flagged = {turn.line_id for turn in detect_suspicious_turns([couplet])}
    assert "ch24_0098" not in flagged


def test_the_stale_neighbour_is_raised(couplet) -> None:
    turns = neighbours_of_reattributed([couplet], {"ch24_0096"})

    assert [turn.line_id for turn in turns] == ["ch24_0098"]
    assert turns[0].detection_pattern == "neighbour_reattributed"
    assert "ch24_0096" in turns[0].detection_reason
    assert "athrogate" in turns[0].detection_reason


def test_a_neighbour_that_agrees_is_left_alone(couplet) -> None:
    """`ch24_0094` is already athrogate, so the rename told us nothing new."""
    turns = neighbours_of_reattributed([couplet], {"ch24_0096"})
    assert "ch24_0094" not in {turn.line_id for turn in turns}


def test_nothing_renamed_costs_nothing(couplet) -> None:
    assert neighbours_of_reattributed([couplet], set()) == []


def test_a_line_the_detector_already_flagged_is_not_duplicated(couplet) -> None:
    turns = neighbours_of_reattributed([couplet], {"ch24_0096"}, already_flagged={"ch24_0098"})
    assert turns == []


def test_narration_between_the_pair_does_not_break_adjacency(couplet) -> None:
    """The two halves are separated by a narrator line; they are still neighbours."""
    turns = neighbours_of_reattributed([couplet], {"ch24_0096"})
    assert turns and turns[0].line_id == "ch24_0098"


def test_the_window_is_shaped_like_a_detected_turn(couplet) -> None:
    turn = neighbours_of_reattributed([couplet], {"ch24_0096"})[0]
    assert turn.chapter_number == 24
    assert sum(1 for line in turn.surrounding_lines if line["is_target"]) == 1
    assert turn.scene_text
