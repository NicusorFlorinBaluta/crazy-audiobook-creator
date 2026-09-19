"""Tests for F13: ranking, collapsing, and top_actions in ReviewGate."""

from __future__ import annotations

from brain.orchestrator.review_gate import (
    ReviewGate,
    ReviewItem,
    collapse_audio_trends,
    group_audio_rejections,
)


def _pronunciation_item(term: str, occurrences: int) -> ReviewItem:
    return ReviewItem(
        category="pronunciation",
        item_id=term,
        title=f"Pronunciation: {term}",
        reason="Recurring name or term has no verified pronunciation mapping.",
        blocking=False,
        details={"term": term, "occurrences": occurrences, "chapters": [1, 2]},
    )


def _trend_item(voice_id: str, chapter: int, ratio: float) -> ReviewItem:
    return ReviewItem(
        category="audio_trend",
        item_id=f"within_chapter_pitch_jump:{voice_id}:{chapter}",
        title="Abrupt pitch change between lines",
        reason=f"This voice's pitch jumps {ratio:.0%} between two adjacent lines. Listen before final release.",
        blocking=False,
        chapter_number=chapter,
        details={
            "voice_id": voice_id,
            "chapter_number": chapter,
            "kind": "within_chapter_pitch_jump",
            "largest_adjacent_pitch_jump_ratio": ratio,
        },
    )


def _audio_item(
    line_id: str,
    chapter: int,
    reason: str,
    *,
    blocking: bool = False,
    disposition: str = "unreviewed",
) -> ReviewItem:
    return ReviewItem(
        category="audio",
        item_id=line_id,
        title=f"Audio segment {line_id}",
        reason=reason,
        blocking=blocking,
        disposition=disposition,
        chapter_number=chapter,
        details={
            "audio_url": f"api/projects/demo/segments/{line_id}/audio",
            "speaker": "narrator",
            "text": f"Some text for {line_id}",
        },
    )


def _attribution_item(line_id: str, chapter: int, *, blocking: bool = True) -> ReviewItem:
    return ReviewItem(
        category="attribution",
        item_id=line_id,
        title=f"Speaker attribution {line_id}",
        reason="Ambiguous speaker attribution.",
        blocking=blocking,
        chapter_number=chapter,
        details={"speaker": "unknown"},
    )


def test_pronunciation_items_sorted_by_occurrences_descending() -> None:
    items = [
        _pronunciation_item("Adbar", 3),
        _pronunciation_item("Breezy", 1108),
        _pronunciation_item("Regis", 148),
        _pronunciation_item("Dahlia", 370),
    ]
    gate = ReviewGate.from_items(items)
    terms = [item.item_id for item in gate.items]
    assert terms == ["Breezy", "Dahlia", "Regis", "Adbar"]


def test_audio_trends_collapse_to_one_row_per_voice() -> None:
    items = [
        _trend_item("narrator", 1, 0.50),
        _trend_item("narrator", 2, 1.80),
        _trend_item("narrator", 3, 0.65),
        _trend_item("jarlaxle", 4, 0.90),
        _trend_item("jarlaxle", 5, 0.48),
        _trend_item("breezy", 6, 0.72),
    ]
    collapsed = collapse_audio_trends(items)
    assert len(collapsed) == 3
    narrator_row = next(it for it in collapsed if it.item_id == "trend:narrator")
    assert narrator_row.details["warning_count"] == 3
    assert narrator_row.details["chapters"] == [1, 2, 3]
    assert narrator_row.chapter_number == 2
    assert "Worst in ch 2" in narrator_row.reason


def test_audio_rejections_grouped_by_glossary_term() -> None:
    items = [
        _audio_item("ch26_0362", 26, "The speaker mispronounces the character's name 'Entreri' as 'And Trary'"),
        _audio_item("ch29_0179", 29, "The transcribed text contains 'Entreri' mispronounced"),
        _audio_item("ch29_0248", 29, "The transcribed text 'and Trary demanded' differs from 'Entreri demanded'"),
        _audio_item("ch08_0005", 8, "The transcribed text 'Caddy Bree' differs from 'Catti-brie'"),
        _audio_item("ch13_0112", 13, "The transcribed text 'Caddy Breeze' differs from 'Catti-brie'"),
        _audio_item("ch01_0100", 1, "Acoustic distortion detected (no glossary term)"),
    ]
    grouped = group_audio_rejections(items, candidate_terms=["Entreri", "Catti-brie"])
    # 3 Entreri -> 1 group; 2 Catti-brie -> 1 group; 1 unrelated -> 1 item = total 3 items
    assert len(grouped) == 3
    entreri_group = next(it for it in grouped if "entreri" in it.item_id)
    assert entreri_group.details["segment_count"] == 3
    assert set(entreri_group.details["segment_ids"]) == {"ch26_0362", "ch29_0179", "ch29_0248"}
    assert entreri_group.details["chapters"] == [26, 29]

    catti_group = next(it for it in grouped if "catti-brie" in it.item_id)
    assert catti_group.details["segment_count"] == 2


def test_large_mixed_fixture_review_gate() -> None:
    """Fixture with 200 mixed items: assert ordering, collapsing, and top_actions."""
    raw_items: list[ReviewItem] = []

    # 1. 2 blocking attributions
    raw_items.append(_attribution_item("attr-block-1", 1, blocking=True))
    raw_items.append(_attribution_item("attr-block-2", 2, blocking=True))

    # 2. 80 pronunciations (occurrences from 1 to 800)
    for i in range(1, 81):
        raw_items.append(_pronunciation_item(f"Name_{i:02d}", i * 10))

    # 3. 60 audio trends across 6 voices (10 per voice)
    voices = ["narrator", "breezy", "jarlaxle", "drizzt", "catti_brie", "bruenor"]
    for v_idx, voice in enumerate(voices):
        for ch in range(1, 11):
            raw_items.append(_trend_item(voice, ch, 0.5 + 0.1 * ch))

    # 4. 40 audio items (including 15 name misses for 'Entreri' and 'Catti-brie')
    for i in range(10):
        raw_items.append(_audio_item(f"entreri_{i}", 20 + i, "Audio QA rejected: name 'Entreri' mispronounced"))
    for i in range(5):
        raw_items.append(_audio_item(f"catti_{i}", 10 + i, "Audio QA rejected: name 'Catti-brie' mispronounced"))
    for i in range(25):
        raw_items.append(
            _audio_item(f"noise_{i}", 1 + (i % 10), f"Generic acoustic warning {i}", disposition="acceptable")
        )

    # 5. 18 miscellaneous character notes
    for i in range(18):
        raw_items.append(
            ReviewItem(
                category="character",
                item_id=f"char_{i}",
                title=f"Character note {i}",
                reason="Advisory character note",
                blocking=False,
            )
        )

    assert len(raw_items) == 200

    # Build ReviewGate using from_items
    gate = ReviewGate.from_items(raw_items, candidate_terms=["Entreri", "Catti-brie"])

    # 1. Check collapsing:
    # 60 audio trends across 6 voices -> 6 items
    trend_items = [it for it in gate.items if it.category == "audio_trend"]
    assert len(trend_items) == 6

    # 10 Entreri + 5 Catti-brie -> 2 grouped items; plus 25 generic audio -> 27 audio items
    audio_items = [it for it in gate.items if it.category == "audio"]
    assert len(audio_items) == 27

    # Pronunciations remain 80
    pron_items = [it for it in gate.items if it.category == "pronunciation"]
    assert len(pron_items) == 80

    # Total items collapsed from 200 down to: 2 + 80 + 6 + 27 + 18 = 133
    assert len(gate.items) == 133

    # 2. Check pronunciation ordering: highest occurrences first
    assert pron_items[0].item_id == "Name_80"  # 800 occurrences
    assert pron_items[-1].item_id == "Name_01"  # 10 occurrences

    # 3. Check serialized report and top_actions
    report = gate.to_dict()
    assert report["total_count"] == 133
    assert report["blocking_count"] == 2
    assert "top_actions" in report
    top_actions = report["top_actions"]
    assert len(top_actions) == 10

    # The 2 blocking items MUST be ranked 1st and 2nd in top_actions
    assert top_actions[0]["blocking"] is True
    assert top_actions[1]["blocking"] is True

    # The remaining 8 top_actions should be the highest-impact actionable items
    for act in top_actions[2:]:
        assert act["changes_output"] is True
