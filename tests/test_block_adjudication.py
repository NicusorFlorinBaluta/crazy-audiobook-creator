"""Unit tests for Targeted Block Adjudication (2026-09-06).

Tests grouping and capping, hard-constraint retry and fallback, malformed/partial/
hallucinated responses, and a reproduction fixture for ch11_0145..0152.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from brain.director.attribution_audit import detect_possessive_contradictions
from brain.director.attribution_detector import SuspiciousTurn
from brain.validators.gemini_validation import (
    DETERMINISTIC_REVIEW_PREFIX,
    _is_deterministic_contradiction,
)
from brain.validators.tiered_adjudicator import (
    TieredAttributionAdjudicator,
    _extract_block_json,
    _find_unconfirmed_run_line_ids,
)
from shared.constants import Gender
from shared.models import Character, CharacterRegistry, ScriptChapter, ScriptLine


@pytest.fixture
def twilight_cast() -> CharacterRegistry:
    def char(cid: str, name: str, gender: Gender, aliases: list[str]) -> Character:
        return Character(
            id=cid,
            name=name,
            gender=gender,
            age_range="adult",
            voice_description=f"{name} voice",
            aliases=aliases,
        )

    return CharacterRegistry(
        characters={
            "narrator": char("narrator", "Narrator", Gender.OTHER, []),
            "dahlia": char("dahlia", "Dahlia", Gender.FEMALE, ["Lady Delilah"]),
            "effron": char("effron", "Effron", Gender.MALE, ["Effron Alegni", "the warlock"]),
        }
    )


class MockBlockOllama:
    def __init__(self, responses: list[str | dict[str, Any]] | None = None) -> None:
        self.responses = list(responses or [])
        self.call_history: list[str] = []
        self.max_output_tokens: int = 8192

    def generate(self, prompt: str, *args, **kwargs) -> str:
        self.call_history.append(prompt)
        if not self.responses:
            raise RuntimeError("MockBlockOllama ran out of canned responses")
        resp = self.responses.pop(0)
        if isinstance(resp, str):
            return resp
        return json.dumps(resp)


# =====================================================================
# 1. Grouping & Capping Unit Tests
# =====================================================================


def test_dialogue_block_grouping_separated_by_narrator_lines(twilight_cast):
    """Spoken lines with <=2 narrator lines group into one block; >2 separate into different blocks."""
    lines = [
        # Block 1: 3 spoken lines separated by 1 and 2 narrator lines
        ScriptLine(line_id="ch01_0001", speaker="effron", text='"Line one."', dialogue_kind="spoken"),
        ScriptLine(line_id="ch01_0002", speaker="narrator", text="He paused.", dialogue_kind=None),
        ScriptLine(line_id="ch01_0003", speaker="dahlia", text='"Line two."', dialogue_kind="spoken"),
        ScriptLine(line_id="ch01_0004", speaker="narrator", text="She looked up.", dialogue_kind=None),
        ScriptLine(line_id="ch01_0005", speaker="narrator", text="A cold wind blew.", dialogue_kind=None),
        ScriptLine(line_id="ch01_0006", speaker="effron", text='"Line three."', dialogue_kind="spoken"),
        # Gap of 3 narrator lines -> separates blocks!
        ScriptLine(line_id="ch01_0007", speaker="narrator", text="Silence fell.", dialogue_kind=None),
        ScriptLine(line_id="ch01_0008", speaker="narrator", text="Minutes passed.", dialogue_kind=None),
        ScriptLine(line_id="ch01_0009", speaker="narrator", text="The fire died down.", dialogue_kind=None),
        # Block 2: 2 spoken lines
        ScriptLine(line_id="ch01_0010", speaker="dahlia", text='"Line four."', dialogue_kind="spoken"),
        ScriptLine(line_id="ch01_0011", speaker="narrator", text="She whispered.", dialogue_kind=None),
        ScriptLine(line_id="ch01_0012", speaker="effron", text='"Line five."', dialogue_kind="spoken"),
    ]
    chapter = ScriptChapter(chapter_number=1, chapter_title="Chapter 1", lines=lines)

    suspicious = [
        SuspiciousTurn("ch01_0001", 1, '"Line one."', "effron", "r", "p", [], ""),
        SuspiciousTurn("ch01_0006", 1, '"Line three."', "effron", "r", "p", [], ""),
        SuspiciousTurn("ch01_0010", 1, '"Line four."', "dahlia", "r", "p", [], ""),
    ]

    adjudicator = TieredAttributionAdjudicator(
        ollama=MockBlockOllama(),
        external_validator=None,
        registry=twilight_cast,
        block_adjudication_enabled=True,
    )
    blocks = adjudicator._group_suspicious_into_blocks(suspicious, {1: chapter})

    assert len(blocks) == 2
    # Block 1 contains ch01_0001 and ch01_0006
    assert [t.line_id for t in blocks[0].suspicious_turns] == ["ch01_0001", "ch01_0006"]
    assert blocks[0].spoken_line_indices == [0, 2, 5]

    # Block 2 contains ch01_0010
    assert [t.line_id for t in blocks[1].suspicious_turns] == ["ch01_0010"]
    assert blocks[1].spoken_line_indices == [9, 11]


def test_dialogue_block_capping_at_widest_narration_gap(twilight_cast):
    """Blocks exceeding max_suspicious_per_call are split at the widest narration gap."""
    # Create 10 spoken lines, each with a suspicious turn, with varying narration gaps
    lines: list[ScriptLine] = []
    suspicious: list[SuspiciousTurn] = []

    # Let gaps be: turn0 - (1 narrator line, 50 chars) - turn1 ... turn4 - (2 narrator lines, 300 chars) - turn5 ...
    for i in range(10):
        line_id = f"ch01_{i:04d}"
        lines.append(ScriptLine(line_id=line_id, speaker="effron", text=f'"Quote {i}."', dialogue_kind="spoken"))
        suspicious.append(SuspiciousTurn(line_id, 1, f'"Quote {i}."', "effron", "r", "p", [], ""))
        if i < 9:
            # Between 4 and 5, put the widest gap: 2 narrator lines, long text
            if i == 4:
                lines.append(
                    ScriptLine(
                        line_id=f"narr_{i}_a",
                        speaker="narrator",
                        text="A very wide narration break with lots of descriptive text.",
                        dialogue_kind=None,
                    )
                )
                lines.append(
                    ScriptLine(
                        line_id=f"narr_{i}_b",
                        speaker="narrator",
                        text="Continuing the very wide narration break with even more prose.",
                        dialogue_kind=None,
                    )
                )
            else:
                lines.append(ScriptLine(line_id=f"narr_{i}", speaker="narrator", text="Short gap.", dialogue_kind=None))

    chapter = ScriptChapter(chapter_number=1, chapter_title="Chapter 1", lines=lines)
    adjudicator = TieredAttributionAdjudicator(
        ollama=MockBlockOllama(),
        external_validator=None,
        registry=twilight_cast,
        block_adjudication_enabled=True,
        max_suspicious_per_call=6,  # Cap at 6 (so 10 turns must be split)
    )

    blocks = adjudicator._group_suspicious_into_blocks(suspicious, {1: chapter})
    # Should split at the widest gap (between turn 4 and 5) into two 5-turn sub-blocks
    assert len(blocks) == 2
    assert len(blocks[0].suspicious_turns) == 5
    assert len(blocks[1].suspicious_turns) == 5
    assert blocks[0].suspicious_turns[-1].line_id == "ch01_0004"
    assert blocks[1].suspicious_turns[0].line_id == "ch01_0005"


def test_unconfirmed_run_detection(twilight_cast):
    """Verify that runs of >=3 same-speaker turns without tag confirmation are detected."""
    lines = [
        # Run 1: 3 turns of effron with NO speech tag
        ScriptLine(line_id="ch01_0001", speaker="effron", text='"Line 1."', dialogue_kind="spoken"),
        ScriptLine(line_id="ch01_0002", speaker="effron", text='"Line 2."', dialogue_kind="spoken"),
        ScriptLine(line_id="ch01_0003", speaker="effron", text='"Line 3."', dialogue_kind="spoken"),
        # Turn of dahlia
        ScriptLine(line_id="ch01_0004", speaker="dahlia", text='"Line 4."', dialogue_kind="spoken"),
        # Run 2: 3 turns of effron with a confirmed speech tag on the second turn
        ScriptLine(line_id="ch01_0005", speaker="effron", text='"Line 5."', dialogue_kind="spoken"),
        ScriptLine(line_id="ch01_0006", speaker="effron", text='"Line 6."', dialogue_kind="spoken"),
        ScriptLine(line_id="ch01_0007", speaker="narrator", text="he said quietly.", dialogue_kind=None),
        ScriptLine(line_id="ch01_0008", speaker="effron", text='"Line 7."', dialogue_kind="spoken"),
    ]
    chapter = ScriptChapter(chapter_number=1, chapter_title="Chapter 1", lines=lines)

    unconfirmed_ids = _find_unconfirmed_run_line_ids(chapter, twilight_cast)

    # Run 1 lines should be in unconfirmed_ids
    assert "ch01_0001" in unconfirmed_ids
    assert "ch01_0002" in unconfirmed_ids
    assert "ch01_0003" in unconfirmed_ids

    # Run 2 has a tag ("he said"), so it is NOT an unconfirmed run!
    assert "ch01_0005" not in unconfirmed_ids
    assert "ch01_0006" not in unconfirmed_ids
    assert "ch01_0007" not in unconfirmed_ids
    assert "ch01_0008" not in unconfirmed_ids


# =====================================================================
# 2. Hard-Constraint Violation -> Retry -> Fallback Unit Tests
# =====================================================================


def test_hard_constraint_retry_success(twilight_cast):
    """Hard constraint violation on attempt 1 triggers retry; valid attempt 2 succeeds."""
    lines = [
        ScriptLine(line_id="ch01_0001", speaker="effron", text='"First quote."', dialogue_kind="spoken"),
        ScriptLine(line_id="ch01_0002", speaker="narrator", text="he replied softly,", dialogue_kind=None),
        ScriptLine(line_id="ch01_0003", speaker="effron", text='"Second quote."', dialogue_kind="spoken"),
    ]
    chapter = ScriptChapter(chapter_number=1, chapter_title="Chapter 1", lines=lines)

    turns = [
        SuspiciousTurn(
            "ch01_0001",
            1,
            '"First quote."',
            "effron",
            "r",
            "p",
            [
                {"line_id": "ch01_0001", "speaker": "effron", "text": '"First quote."', "is_target": True},
                {"line_id": "ch01_0002", "speaker": "narrator", "text": "he replied softly,"},
                {"line_id": "ch01_0003", "speaker": "effron", "text": '"Second quote."'},
            ],
            "First quote. he replied softly, Second quote.",
        ),
        SuspiciousTurn(
            "ch01_0003",
            1,
            '"Second quote."',
            "effron",
            "r",
            "p",
            [
                {"line_id": "ch01_0001", "speaker": "effron", "text": '"First quote."'},
                {"line_id": "ch01_0002", "speaker": "narrator", "text": "he replied softly,"},
                {"line_id": "ch01_0003", "speaker": "effron", "text": '"Second quote."', "is_target": True},
            ],
            "First quote. he replied softly, Second quote.",
        ),
    ]

    # Attempt 1 assigns ch01_0001 to dahlia (female), violating "he replied" (male)
    attempt1 = {
        "ch01_0001": {"speaker_id": "dahlia", "confidence": 0.95, "reason": "test", "evidence_quote": "First quote"},
        "ch01_0003": {"speaker_id": "dahlia", "confidence": 0.95, "reason": "test", "evidence_quote": "Second quote"},
    }
    # Attempt 2 corrects ch01_0001 to effron (male)
    attempt2 = {
        "ch01_0001": {
            "speaker_id": "effron",
            "confidence": 0.98,
            "reason": "he replied tag",
            "evidence_quote": "he replied softly",
        },
        "ch01_0003": {"speaker_id": "dahlia", "confidence": 0.95, "reason": "reply", "evidence_quote": "Second quote"},
    }

    mock_ollama = MockBlockOllama([attempt1, attempt2])
    adjudicator = TieredAttributionAdjudicator(
        ollama=mock_ollama,
        external_validator=None,
        registry=twilight_cast,
        block_adjudication_enabled=True,
    )

    results = adjudicator._adjudicate_block_tier1(turns, chapter)

    assert len(results) == 2
    assert len(mock_ollama.call_history) == 2  # Proves retry occurred!
    assert results[0].resolved_speaker == "effron"
    assert results[0].resolver_tier == "local_qwen_block"
    assert results[1].resolved_speaker == "dahlia"


def test_hard_constraint_persistent_violation_falls_back(twilight_cast, tmp_path):
    """Persistent constraint violation after retry fails block and triggers per-line fallback."""
    lines = [
        ScriptLine(line_id="ch01_0001", speaker="effron", text='"First quote."', dialogue_kind="spoken"),
        ScriptLine(line_id="ch01_0002", speaker="narrator", text="he replied softly,", dialogue_kind=None),
    ]
    chapter = ScriptChapter(chapter_number=1, chapter_title="Chapter 1", lines=lines)
    turns = [
        SuspiciousTurn(
            "ch01_0001",
            1,
            '"First quote."',
            "effron",
            "r",
            "p",
            [
                {"line_id": "ch01_0001", "speaker": "effron", "text": '"First quote."', "is_target": True},
                {"line_id": "ch01_0002", "speaker": "narrator", "text": "he replied softly,"},
            ],
            "First quote. he replied softly,",
        ),
    ]

    # Both attempts assign ch01_0001 to female dahlia despite male "he replied"
    violating_resp = {
        "ch01_0001": {"speaker_id": "dahlia", "confidence": 0.95, "reason": "test", "evidence_quote": "First quote"},
    }
    # Per-line fallback mock response
    per_line_resp = {
        "speaker_id": "effron",
        "confidence": 0.98,
        "reason": "tag check",
        "evidence_quote": "he replied softly",
    }

    mock_ollama = MockBlockOllama([violating_resp, violating_resp, per_line_resp])
    adjudicator = TieredAttributionAdjudicator(
        ollama=mock_ollama,
        external_validator=None,
        registry=twilight_cast,
        block_adjudication_enabled=True,
        only_unconfirmed_runs=False,
    )

    report = adjudicator.adjudicate(turns, tmp_path, [chapter], dry_run=True)

    # Result resolved via per-line fallback
    assert len(report.results) == 1
    assert report.results[0].line_id == "ch01_0001"
    assert report.results[0].resolved_speaker == "effron"


# =====================================================================
# 3. Malformed / Partial / Hallucinated-line_id Response Tests
# =====================================================================


def test_malformed_json_triggers_fallback(twilight_cast, tmp_path):
    """Malformed response triggers per-line fallback."""
    lines = [ScriptLine(line_id="ch01_0001", speaker="effron", text='"A quote."', dialogue_kind="spoken")]
    chapter = ScriptChapter(chapter_number=1, chapter_title="Chapter 1", lines=lines)
    turns = [SuspiciousTurn("ch01_0001", 1, '"A quote."', "effron", "r", "p", [], "A quote.")]

    mock_ollama = MockBlockOllama(
        [
            "NOT JSON AT ALL",
            {"speaker_id": "effron", "confidence": 0.95, "reason": "per-line fallback", "evidence_quote": "A quote"},
        ]
    )
    adjudicator = TieredAttributionAdjudicator(
        ollama=mock_ollama,
        external_validator=None,
        registry=twilight_cast,
        block_adjudication_enabled=True,
        only_unconfirmed_runs=False,
    )

    report = adjudicator.adjudicate(turns, tmp_path, [chapter], dry_run=True)
    assert len(report.results) == 1
    assert report.results[0].resolved_speaker == "effron"


def test_missing_line_id_triggers_fallback(twilight_cast, tmp_path):
    """Response missing a required line_id triggers per-line fallback."""
    lines = [
        ScriptLine(line_id="ch01_0001", speaker="effron", text='"Quote 1."', dialogue_kind="spoken"),
        ScriptLine(line_id="ch01_0002", speaker="effron", text='"Quote 2."', dialogue_kind="spoken"),
    ]
    chapter = ScriptChapter(chapter_number=1, chapter_title="Chapter 1", lines=lines)
    turns = [
        SuspiciousTurn("ch01_0001", 1, '"Quote 1."', "effron", "r", "p", [], "Quote 1. Quote 2."),
        SuspiciousTurn("ch01_0002", 1, '"Quote 2."', "effron", "r", "p", [], "Quote 1. Quote 2."),
    ]

    partial_block = {
        "ch01_0001": {"speaker_id": "effron", "confidence": 0.95, "reason": "test", "evidence_quote": "Quote 1"}
        # ch01_0002 is MISSING
    }
    per_line_1 = {"speaker_id": "effron", "confidence": 0.95, "reason": "turn 1", "evidence_quote": "Quote 1"}
    per_line_2 = {"speaker_id": "dahlia", "confidence": 0.95, "reason": "turn 2", "evidence_quote": "Quote 2"}

    mock_ollama = MockBlockOllama([partial_block, partial_block, per_line_1, per_line_2])
    adjudicator = TieredAttributionAdjudicator(
        ollama=mock_ollama,
        external_validator=None,
        registry=twilight_cast,
        block_adjudication_enabled=True,
        only_unconfirmed_runs=False,
    )

    report = adjudicator.adjudicate(turns, tmp_path, [chapter], dry_run=True)
    assert len(report.results) == 2
    assert report.results[0].resolved_speaker == "effron"
    assert report.results[1].resolved_speaker == "dahlia"


def test_hallucinated_line_id_triggers_fallback(twilight_cast, tmp_path):
    """Response with an invented/hallucinated line_id triggers per-line fallback."""
    lines = [ScriptLine(line_id="ch01_0001", speaker="effron", text='"Quote 1."', dialogue_kind="spoken")]
    chapter = ScriptChapter(chapter_number=1, chapter_title="Chapter 1", lines=lines)
    turns = [SuspiciousTurn("ch01_0001", 1, '"Quote 1."', "effron", "r", "p", [], "Quote 1.")]

    hallucinated_block = {
        "ch01_0001": {"speaker_id": "effron", "confidence": 0.95, "reason": "test", "evidence_quote": "Quote 1"},
        "ch01_9999": {"speaker_id": "dahlia", "confidence": 0.95, "reason": "fake", "evidence_quote": "fake"},
    }
    per_line = {"speaker_id": "effron", "confidence": 0.95, "reason": "turn 1", "evidence_quote": "Quote 1"}

    mock_ollama = MockBlockOllama([hallucinated_block, per_line])
    adjudicator = TieredAttributionAdjudicator(
        ollama=mock_ollama,
        external_validator=None,
        registry=twilight_cast,
        block_adjudication_enabled=True,
        only_unconfirmed_runs=False,
    )

    report = adjudicator.adjudicate(turns, tmp_path, [chapter], dry_run=True)
    assert len(report.results) == 1
    assert report.results[0].resolved_speaker == "effron"


# =====================================================================
# 4. Fixture Reproducing ch11_0145..0152
# =====================================================================


def test_fixture_ch11_0145_to_0152_applies_block_response_to_results(twilight_cast, tmp_path):
    """Verifies plumbing: mock block JSON response is correctly parsed and applied to script lines and results."""
    lines = [
        ScriptLine(line_id="ch11_0145", speaker="effron", text='"Of course!"', dialogue_kind="spoken"),
        ScriptLine(
            line_id="ch11_0146",
            speaker="narrator",
            text="Effron took a raspy breath and calmed down.",
            dialogue_kind=None,
        ),
        ScriptLine(
            line_id="ch11_0147",
            speaker="effron",
            text='"And I have even done you small favors, as you mention."',
            dialogue_kind="spoken",
        ),
        ScriptLine(
            line_id="ch11_0148",
            speaker="effron",
            text='"But that is all. You never come to the house I have built. You have never invited me to be a guest in your tower."',
            dialogue_kind="spoken",
        ),
        ScriptLine(
            line_id="ch11_0149",
            speaker="effron",
            text='"You will never be invited into my tower, mother,"',
            dialogue_kind="spoken",
        ),
        ScriptLine(
            line_id="ch11_0150",
            speaker="narrator",
            text="he replied, then whispered in her ear,",
            dialogue_kind=None,
        ),
        ScriptLine(line_id="ch11_0151", speaker="effron", text='"You are a vampire."', dialogue_kind="spoken"),
        ScriptLine(line_id="ch11_0152", speaker="dahlia", text='"And you are my son."', dialogue_kind="spoken"),
    ]
    chapter = ScriptChapter(chapter_number=11, chapter_title="Chapter 11", lines=lines)

    # 147 and 148 are suspicious consecutive collapse turns
    turns = [
        SuspiciousTurn(
            line_id="ch11_0147",
            chapter_number=11,
            text='"And I have even done you small favors, as you mention."',
            current_speaker="effron",
            detection_reason="Consecutive dialogue collapse",
            detection_pattern="consecutive_collapse",
            surrounding_lines=[
                {"line_id": l.line_id, "speaker": l.speaker, "text": l.text, "is_target": (l.line_id == "ch11_0147")}
                for l in lines
            ],
            scene_text=" ".join(l.text for l in lines),
        ),
        SuspiciousTurn(
            line_id="ch11_0148",
            chapter_number=11,
            text='"But that is all. You never come to the house I have built. You have never invited me to be a guest in your tower."',
            current_speaker="effron",
            detection_reason="Consecutive dialogue collapse",
            detection_pattern="consecutive_collapse",
            surrounding_lines=[
                {"line_id": l.line_id, "speaker": l.speaker, "text": l.text, "is_target": (l.line_id == "ch11_0148")}
                for l in lines
            ],
            scene_text=" ".join(l.text for l in lines),
        ),
    ]

    # Block model produces joint assignments: Dahlia complains about favors and house vs tower
    block_response = {
        "ch11_0147": {
            "speaker_id": "dahlia",
            "confidence": 0.98,
            "reason": "Dahlia begins her rebuttal regarding small favors",
            "evidence_quote": "done you small favors, as you mention",
        },
        "ch11_0148": {
            "speaker_id": "dahlia",
            "confidence": 0.98,
            "reason": "Dahlia contrasts the house she built with Effron's tower",
            "evidence_quote": "You never come to the house I have built",
        },
    }

    mock_ollama = MockBlockOllama([block_response])
    adjudicator = TieredAttributionAdjudicator(
        ollama=mock_ollama,
        external_validator=None,
        registry=twilight_cast,
        block_adjudication_enabled=True,
        only_unconfirmed_runs=True,
    )

    report = adjudicator.adjudicate(turns, tmp_path, [chapter], dry_run=False)

    # Acceptance criteria verification:
    # 1. ch11_0147 and ch11_0148 resolve to dahlia with local_qwen_block provenance
    res_map = {r.line_id: r for r in report.results}
    assert res_map["ch11_0147"].resolved_speaker == "dahlia"
    assert res_map["ch11_0147"].resolver_tier == "local_qwen_block"
    assert res_map["ch11_0148"].resolved_speaker == "dahlia"
    assert res_map["ch11_0148"].resolver_tier == "local_qwen_block"

    # 2. Applied changes on chapter lines
    assert chapter.lines[2].speaker == "dahlia"
    assert chapter.lines[3].speaker == "dahlia"
    # 3. 0149 remains effron with tag confirmation untouched
    assert chapter.lines[4].speaker == "effron"
    # 4. Observability metrics
    assert report.summary["blocks_adjudicated"] == 1
    assert report.summary["block_fallbacks"] == 0


def test_extract_block_json_bare_string_assigns_low_confidence():
    """Bare string response in block JSON produces low confidence (0.50) so it escalates instead of auto-accepting."""
    raw = json.dumps({"ch01_0001": "dahlia"})
    parsed = _extract_block_json(raw, {"ch01_0001"})
    assert parsed["ch01_0001"]["speaker_id"] == "dahlia"
    assert parsed["ch01_0001"]["confidence"] == 0.50
    assert parsed["ch01_0001"]["confidence"] < 0.85


def test_uncovered_turn_not_silently_dropped_when_chapter_missing_from_map(twilight_cast, tmp_path):
    """If a suspicious turn's chapter is omitted from chapter_map, it falls back to per-line instead of being dropped."""
    lines = [
        ScriptLine(line_id="ch01_0001", speaker="effron", text='"First quote."', dialogue_kind="spoken"),
    ]
    chapter1 = ScriptChapter(chapter_number=1, chapter_title="Chapter 1", lines=lines)

    # Turn is in chapter 2, which is NOT in chapter_map
    turns = [
        SuspiciousTurn(
            line_id="ch02_0001",
            chapter_number=2,
            text='"Quote in ch2."',
            current_speaker="effron",
            detection_reason="Consecutive dialogue collapse",
            detection_pattern="consecutive_collapse",
            surrounding_lines=[],
            scene_text="Quote in ch2.",
        )
    ]

    mock_ollama = MockBlockOllama(
        [
            {"speaker_id": "effron", "confidence": 0.95, "reason": "per-line fallback", "evidence_quote": "A quote"},
        ]
    )
    adjudicator = TieredAttributionAdjudicator(
        ollama=mock_ollama,
        external_validator=None,
        registry=twilight_cast,
        block_adjudication_enabled=True,
    )

    # Only pass chapter 1 in chapters list
    report = adjudicator.adjudicate(turns, tmp_path, [chapter1], dry_run=True)

    # The turn must NOT be silently dropped!
    assert len(report.results) == 1
    assert report.results[0].line_id == "ch02_0001"
    assert report.summary["total_suspicious"] == 1


class TestPossessiveContradictionCheck:
    """Risk 2's mitigation: a consistency check kept outside the adjudicator.

    The plan warns that block adjudication is *instructed* to produce a
    self-consistent assignment, so it resolves the tension that made the ch11
    error visible in the first place -- "trading loud, detectable errors for
    smooth, plausible, invisible ones". The mitigation it asks for is an
    independent post-hoc check that the block prompt never sees.

    The signature it looks for, from the shipped script:

        ch11_0148 [effron] "...never invited me to be a guest in YOUR tower."
        ch11_0149 [effron] "You will never be invited into MY tower, mother,"

    One speaker, one unbroken turn, both owning and not owning the tower.
    """

    @staticmethod
    def _chapter(number: int, rows: list[tuple[str, str, str]]) -> ScriptChapter:
        return ScriptChapter(
            chapter_number=number,
            chapter_title=f"Chapter {number}",
            lines=[
                ScriptLine(line_id=lid, speaker=speaker, text=text)
                for lid, speaker, text in rows
            ],
        )

    def test_the_ch11_tower_contradiction_is_caught(self) -> None:
        chapter = self._chapter(11, [
            ("ch11_0147", "effron", '"And I have even done you small favors, as you mention."'),
            ("ch11_0148", "effron", '"You have never invited me to be a guest in your tower."'),
            ("ch11_0149", "effron", '"You will never be invited into my tower, mother,"'),
        ])
        found = detect_possessive_contradictions([chapter])
        assert len(found) == 1
        assert found[0]["speaker"] == "effron"
        assert found[0]["noun"] == "tower"
        assert found[0]["claimed_line_id"] == "ch11_0149"
        assert found[0]["disclaimed_line_id"] == "ch11_0148"

    def test_a_contrast_inside_one_line_is_not_a_contradiction(self) -> None:
        """"Your tower is grander than my tower" is one speaker, two towers."""
        chapter = self._chapter(1, [
            ("ch01_0001", "effron", '"Your tower is grander than my tower, mother."'),
            ("ch01_0002", "effron", '"That has always been true."'),
        ])
        assert detect_possessive_contradictions([chapter]) == []

    def test_two_speakers_may_disagree_about_ownership(self) -> None:
        """The check is about ONE speaker contradicting themselves."""
        chapter = self._chapter(1, [
            ("ch01_0001", "dahlia", '"You have never invited me into your tower."'),
            ("ch01_0002", "effron", '"You will never be invited into my tower."'),
        ])
        assert detect_possessive_contradictions([chapter]) == []

    def test_a_narrator_line_does_not_break_the_run(self) -> None:
        """Speech tags sit between turns; the run is the speaker's, not the text's."""
        chapter = self._chapter(11, [
            ("ch11_0148", "effron", '"...a guest in your tower."'),
            ("ch11_0149", "effron", '"You will never enter my tower."'),
        ])
        assert len(detect_possessive_contradictions([chapter])) == 1

    def test_it_is_reported_and_never_blocking(self) -> None:
        """One item per book is a reading, not a queue.

        Measured on both analysed books it fires exactly once each: the real
        ch11 error, and one false positive in Isles of the Emberdark where
        "knowing your way home" and "find our way back" are routes rather than
        possessions. With a single false positive to learn from, a stop-list of
        abstract nouns would be fitting to noise, so none is applied and the
        result informs rather than gates.
        """
        chapter = self._chapter(25, [
            ("ch25_0101", "dusk", '"Setting off without knowing your way home is stupid."'),
            ("ch25_0103", "dusk", '"No, I don\'t know how we\'ll find our way back,"'),
        ])
        found = detect_possessive_contradictions([chapter])
        assert len(found) == 1, "the known false positive is documented, not suppressed"
        assert found[0]["noun"] == "way"


class TestDeterministicRefutationOutranksAModel:
    """A model may not restate a speaker the text has already refuted.

    `ch11_0148` is the case that forced this. The possessive check proves
    Effron cannot be the speaker -- he disowns the tower on that line and owns
    it on the next, which is tag-confirmed as his. Both models say Effron
    anyway: qwen at 0.98, Gemini triage at 0.95. Escalation used to clear the
    review flag on that basis, turning a proven defect into a confident wrong
    answer.

    Measured live, twice, on the same line: one run had adjudication answer
    `dahlia` at 1.0 once triage's restatement was refused; the next run had it
    answer `effron` at 0.74, below the threshold, so the line stayed flagged.
    Gemini is not stable here. The guard is correct either way -- it never lets
    the refuted speaker be restated at high confidence, and a line the models
    cannot better is left for a human.
    """

    def test_the_marker_identifies_a_deterministic_finding(self) -> None:
        line = SimpleNamespace(
            attribution_review_reason=DETERMINISTIC_REVIEW_PREFIX + "'effron' both owns and does not own 'tower'"
        )
        assert _is_deterministic_contradiction(line)

    def test_an_ordinary_review_reason_is_not_one(self) -> None:
        line = SimpleNamespace(attribution_review_reason="Confidence 0.80 < threshold 0.85")
        assert not _is_deterministic_contradiction(line)

    def test_a_missing_reason_is_not_one(self) -> None:
        assert not _is_deterministic_contradiction(SimpleNamespace(attribution_review_reason=""))
        assert not _is_deterministic_contradiction(SimpleNamespace())

    def test_the_detector_output_carries_the_marker_verbatim(self) -> None:
        """The audit's `reason` is what gets prefixed, so the two must agree."""
        chapter = ScriptChapter(
            chapter_number=11,
            chapter_title="Eleven",
            lines=[
                ScriptLine(line_id="ch11_0148", speaker="effron", text='"...a guest in your tower."'),
                ScriptLine(line_id="ch11_0149", speaker="effron", text='"...into my tower, mother,"'),
            ],
        )
        finding = detect_possessive_contradictions([chapter])[0]
        flagged = SimpleNamespace(attribution_review_reason=DETERMINISTIC_REVIEW_PREFIX + finding["reason"])
        assert _is_deterministic_contradiction(flagged)
        assert "effron" in flagged.attribution_review_reason
