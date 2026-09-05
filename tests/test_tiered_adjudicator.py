"""Unit tests for Tiered Attribution Detector and Adjudicator Guardrails."""

import pytest

from brain.director.attribution_detector import (
    SuspiciousTurn,
    detect_suspicious_turns,
)
from brain.director.script_generator import ScriptGenerator
from brain.validators.tiered_adjudicator import (
    AdjudicationResult,
    TieredAttributionAdjudicator,
    _check_gender_pronoun_consistency,
    _extract_json,
    _fuzzy_quote_in_context,
    _resolve_speaker_alias,
)
from shared.constants import Gender
from shared.models import Character, CharacterRegistry, ScriptChapter, ScriptLine


@pytest.fixture
def sample_registry() -> CharacterRegistry:
    return CharacterRegistry(
        characters={
            "dusk": Character(
                id="dusk",
                name="Sixth of the Dusk",
                gender=Gender.MALE,
                age_range="adult",
                voice_description="Gravelly, cautious trapper voice",
                aliases=["Dusk", "trapper"],
            ),
            "dajer": Character(
                id="dajer",
                name="Colonel Dajer",
                gender=Gender.MALE,
                age_range="middle-aged",
                voice_description="Commanding, polite military officer",
                aliases=["Colonel", "Dajer"],
            ),
            "catti_brie": Character(
                id="catti_brie",
                name="Catti-brie",
                gender=Gender.FEMALE,
                age_range="young adult",
                voice_description="Fierce, clear archer voice",
                aliases=["Brie", "Catti"],
            ),
            "allefaero": Character(
                id="allefaero",
                name="Allefaero",
                gender=Gender.MALE,
                age_range="adult",
                voice_description="Refined nobleman voice",
                aliases=["Lord Allefaero"],
            ),
        }
    )


# =====================================================================
# Guardrail 1: Fuzzy Evidence Quote Verification
# =====================================================================


def test_fuzzy_quote_in_context_exact():
    scene = "Dusk glanced at the dark cavern. 'What cave?' he asked Colonel Dajer."
    quote = "'What cave?' he asked"
    passed, detail = _fuzzy_quote_in_context(quote, scene)
    assert passed is True
    assert "exact_match" in detail


def test_fuzzy_quote_in_context_smart_quotes_and_dashes():
    scene = "Dusk glanced at the dark cavern\u2014'What cave?' he asked."
    # Quote with smart quotes and normal hyphen
    quote = "\u201cWhat cave?\u201d he asked"
    passed, detail = _fuzzy_quote_in_context(quote, scene)
    assert passed is True


def test_fuzzy_quote_in_context_minor_paraphrase():
    scene = "Dajer smiled faintly. The glowing energy was unlike anything on First of the Sun."
    quote = "The glowing energy was unlike anything on First of Sun"  # omitted 'the'
    passed, detail = _fuzzy_quote_in_context(quote, scene)
    assert passed is True
    assert "fuzzy_match" in detail


def test_fuzzy_quote_in_context_fabricated():
    scene = "Dusk walked along the shore in silence. The waves crashed gently."
    quote = "He whispered about the dark cave of ancient secrets"
    passed, detail = _fuzzy_quote_in_context(quote, scene)
    assert passed is False
    assert "evidence_not_found" in detail


# =====================================================================
# Guardrail 2: Gender / Pronoun Consistency Check
# =====================================================================


def test_gender_pronoun_consistency_valid(sample_registry):
    passed, detail = _check_gender_pronoun_consistency(
        "dusk",
        "He frowned and replied slowly.",
        sample_registry,
    )
    assert passed is True
    assert detail == "gender_consistent"


def test_gender_pronoun_consistency_male_with_female_tag(sample_registry):
    # Dusk is Male, evidence says "she said"
    passed, detail = _check_gender_pronoun_consistency(
        "dusk",
        "She said with a slight frown.",
        sample_registry,
    )
    assert passed is False
    assert "Male speaker contradicts cited female speech tag" in detail


def test_gender_pronoun_consistency_female_with_male_tag(sample_registry):
    # Catti-brie is Female, evidence says "he asked"
    passed, detail = _check_gender_pronoun_consistency(
        "catti_brie",
        "He asked, waving his hand toward the door.",
        sample_registry,
    )
    assert passed is False
    assert "Female speaker contradicts cited male speech tag" in detail


def test_gender_pronoun_consistency_overconfidence_benchmark(sample_registry):
    # Reproduces the ch17_0119 benchmark trap
    # Allefaero is Male; if attributed to Catti-brie with male pronoun tag, it must be caught
    passed, detail = _check_gender_pronoun_consistency(
        "catti_brie",
        "he said, but waved his hand when Breezy started to speak",
        sample_registry,
    )
    assert passed is False


# =====================================================================
# Guardrail 3: Canonical Alias Resolution
# =====================================================================


def test_resolve_speaker_alias_exact_id(sample_registry):
    cid, detail = _resolve_speaker_alias("dusk", sample_registry)
    assert cid == "dusk"
    assert detail == "exact_id"


def test_resolve_speaker_alias_known_alias(sample_registry):
    # "Brie" should resolve to "catti_brie"
    cid, detail = _resolve_speaker_alias("brie", sample_registry)
    assert cid == "catti_brie"
    assert detail == "alias_resolved"


def test_resolve_speaker_alias_full_name(sample_registry):
    # "Sixth of the Dusk" should resolve to "dusk"
    cid, detail = _resolve_speaker_alias("Sixth of the Dusk", sample_registry)
    assert cid == "dusk"
    assert detail == "alias_resolved"


def test_resolve_speaker_alias_unknown(sample_registry):
    cid, detail = _resolve_speaker_alias("random_person", sample_registry)
    assert cid is None
    assert "unresolved_speaker" in detail


# =====================================================================
# JSON Extraction Helper
# =====================================================================


def test_extract_json_markdown_fence():
    text = 'Here is the result:\n```json\n{"speaker_id": "dusk", "confidence": 0.98}\n```\nDone.'
    data = _extract_json(text)
    assert data["speaker_id"] == "dusk"
    assert data["confidence"] == 0.98


def test_extract_json_raw():
    text = '{"speaker_id": "dajer", "confidence": 0.95, "reason": "action beat"}'
    data = _extract_json(text)
    assert data["speaker_id"] == "dajer"


# =====================================================================
# Detector Tests
# =====================================================================


def test_detect_consecutive_collapse():
    # Two dialogue lines assigned to the same speaker, one is short / question
    lines = [
        ScriptLine(line_id="ch01_0001", speaker="dajer", text='"Do you know about the cave?"', dialogue_kind="spoken"),
        ScriptLine(line_id="ch01_0002", speaker="dajer", text='"What cave?"', dialogue_kind="spoken"),
    ]
    chapter = ScriptChapter(chapter_number=1, chapter_title="Chapter 1", lines=lines)
    suspicious = detect_suspicious_turns([chapter])

    assert len(suspicious) == 1
    assert suspicious[0].line_id == "ch01_0002"
    assert suspicious[0].detection_pattern == "consecutive_collapse"


def test_detect_narrator_separated_collapse():
    # Dialogue speaker A -> narrator -> Dialogue speaker A (question)
    lines = [
        ScriptLine(line_id="ch01_0001", speaker="dajer", text='"Do you know about the cave?"', dialogue_kind="spoken"),
        ScriptLine(
            line_id="ch01_0002", speaker="narrator", text="Dusk stared at him in disbelief.", dialogue_kind=None
        ),
        ScriptLine(line_id="ch01_0003", speaker="dajer", text='"What cave?"', dialogue_kind="spoken"),
    ]
    chapter = ScriptChapter(chapter_number=1, chapter_title="Chapter 1", lines=lines)
    suspicious = detect_suspicious_turns([chapter])

    assert any(s.line_id == "ch01_0003" for s in suspicious)


def test_detect_clean_alternation_not_flagged():
    # Clean alternation between Dajer and Dusk with good confidence
    lines = [
        ScriptLine(
            line_id="ch01_0001",
            speaker="dajer",
            text='"Do you know about the cave?"',
            dialogue_kind="spoken",
            speaker_confidence=0.95,
        ),
        ScriptLine(
            line_id="ch01_0002", speaker="dusk", text='"What cave?"', dialogue_kind="spoken", speaker_confidence=0.98
        ),
        ScriptLine(
            line_id="ch01_0003",
            speaker="dajer",
            text='"The one near the ridge."',
            dialogue_kind="spoken",
            speaker_confidence=0.95,
        ),
    ]
    chapter = ScriptChapter(chapter_number=1, chapter_title="Chapter 1", lines=lines)
    suspicious = detect_suspicious_turns([chapter])

    # Nothing should be flagged
    assert len(suspicious) == 0


def test_detect_low_confidence_flagged():
    lines = [
        ScriptLine(
            line_id="ch01_0001",
            speaker="dajer",
            text='"Do you know about the cave?"',
            dialogue_kind="spoken",
            speaker_confidence=0.50,
        ),
    ]
    chapter = ScriptChapter(chapter_number=1, chapter_title="Chapter 1", lines=lines)
    suspicious = detect_suspicious_turns([chapter], min_confidence=0.70)

    assert len(suspicious) == 1
    assert suspicious[0].line_id == "ch01_0001"
    assert suspicious[0].detection_pattern == "low_confidence"


# =====================================================================
# Guardrail 4: Reciprocal Turn Consistency Check
# =====================================================================


def test_reciprocal_turn_guardrail_reverts_same_speaker_qa_pair(sample_registry):
    lines = [
        ScriptLine(line_id="ch39_0563", speaker="dajer", text='"Do you know about the cave?"', dialogue_kind="spoken"),
        ScriptLine(line_id="ch39_0564", speaker="dajer", text='"What cave?"', dialogue_kind="spoken"),
    ]
    chapter = ScriptChapter(chapter_number=39, chapter_title="Chapter 39", lines=lines)

    # Mock adjudicator instance just to call _apply_reciprocal_turn_guardrail
    adjudicator = TieredAttributionAdjudicator(
        ollama=None,  # Not invoked for this unit test
        external_validator=None,
        registry=sample_registry,
    )

    # Both lines erroneously resolved as dajer by Tier 1
    results = [
        AdjudicationResult(
            line_id="ch39_0563",
            chapter_number=39,
            text='"Do you know about the cave?"',
            original_speaker="dajer",
            resolved_speaker="dajer",
            resolver_tier="local_qwen",
            confidence=0.98,
            reason="Dialogue turn without explicit speech tag",
            evidence_quote="cave",
            guardrail_results={},
        ),
        AdjudicationResult(
            line_id="ch39_0564",
            chapter_number=39,
            text='"What cave?"',
            original_speaker="dajer",
            resolved_speaker="dajer",
            resolver_tier="local_qwen",
            confidence=0.96,
            reason="Dialogue turn without explicit speech tag",
            evidence_quote="cave",
            guardrail_results={},
        ),
    ]

    chapter_map = {39: chapter}
    adjudicator._apply_reciprocal_turn_guardrail(results, chapter_map)

    # Both must have their local_qwen tier invalidated and escalated to gemini_api!
    assert results[0].resolver_tier == "gemini_api"
    assert "Guardrail 4" in results[0].reason
    assert results[1].resolver_tier == "gemini_api"
    assert "Guardrail 4" in results[1].reason


def test_generic_descriptor_alias_filtering():
    """Verify bare generic nouns like 'stranger' or 'alien' are not resolved as character aliases."""
    registry = CharacterRegistry(
        characters={
            "armored_alien": Character(
                id="armored_alien",
                name="Armored Alien",
                gender=Gender.MALE,
                age_range="adult",
                voice_description="Metallic alien voice",
                aliases=["Armored Alien", "The Other Alien", "Stranger", "Alien"],
            ),
            "dusk": Character(
                id="dusk",
                name="Sixth of the Dusk",
                gender=Gender.MALE,
                age_range="adult",
                voice_description="Gravelly voice",
                aliases=["Sixth of the Dusk", "Sixth", "Dusk"],
            ),
        }
    )

    # Multi-word alias should resolve
    resolved, detail = _resolve_speaker_alias("Armored Alien", registry)
    assert resolved == "armored_alien"
    assert detail in ("exact_id", "alias_resolved")

    # Full name should resolve
    resolved, detail = _resolve_speaker_alias("The Other Alien", registry)
    assert resolved == "armored_alien"

    # Bare generic noun 'stranger' or 'alien' must NOT resolve to armored_alien
    resolved, detail = _resolve_speaker_alias("stranger", registry)
    assert resolved is None
    assert "unresolved_speaker" in detail or "ambiguous" in detail

    resolved, detail = _resolve_speaker_alias("alien", registry)
    assert resolved is None
    assert "unresolved_speaker" in detail or "ambiguous" in detail


def test_chapter_scoping_ignores_bare_generic_descriptors():
    """Verify chapter scoping does not activate a character solely because of bare generic nouns."""
    registry = CharacterRegistry(
        characters={
            "armored_alien": Character(
                id="armored_alien",
                name="Armored Alien",
                gender=Gender.MALE,
                age_range="adult",
                voice_description="Metallic alien voice",
                aliases=["Armored Alien", "The Other Alien", "Stranger", "Alien"],
            ),
            "dusk": Character(
                id="dusk",
                name="Sixth of the Dusk",
                gender=Gender.MALE,
                age_range="adult",
                voice_description="Gravelly voice",
                aliases=["Sixth", "Dusk"],
            ),
        }
    )

    # Text contains 'stranger' and 'alien', but neither mentions 'Armored Alien' nor 'The Other Alien'
    text = "The stranger walked down the hall and wondered about the strange alien devices."
    scoped = ScriptGenerator._get_chapter_scoped_speakers(text, registry)

    assert "armored_alien" not in scoped

    # Text that actually mentions 'Armored Alien' must activate armored_alien
    text_specific = "The Armored Alien raised his weapon and spoke to the council."
    scoped_specific = ScriptGenerator._get_chapter_scoped_speakers(text_specific, registry)

    assert "armored_alien" in scoped_specific


def test_summary_separates_confirmations_from_reattributions(sample_registry, tmp_path):
    """A resolution that keeps the speaker is not a repair.

    On the 2026-09-04 run, 91 lines were logged as "Repaired" and 89 of them
    were `starling -> starling`. The work is real -- a confirmation raises
    confidence and clears the review flag -- but the combined count reads as
    "attribution was wrong 91 times" when it was wrong twice, and nothing
    downstream could tell the two apart.
    """
    lines = [
        ScriptLine(line_id="ch01_0001", speaker="dajer", text='"One."', dialogue_kind="spoken"),
        ScriptLine(line_id="ch01_0002", speaker="dajer", text='"Two."', dialogue_kind="spoken"),
        ScriptLine(line_id="ch01_0003", speaker="dajer", text='"Three."', dialogue_kind="spoken"),
    ]
    chapter = ScriptChapter(chapter_number=1, chapter_title="Chapter 1", lines=lines)

    def result(line_id, resolved, tier):
        return AdjudicationResult(
            line_id=line_id,
            chapter_number=1,
            text="text",
            original_speaker="dajer",
            resolved_speaker=resolved,
            resolver_tier=tier,
            confidence=0.99,
            reason="test",
            evidence_quote="",
            guardrail_results={},
        )

    canned = {
        # unchanged -> a confirmation
        "ch01_0001": result("ch01_0001", "dajer", "local_qwen"),
        # changed -> a genuine reattribution
        "ch01_0002": result("ch01_0002", "starling", "local_qwen"),
        # not resolved locally -> escalated, neither of the above
        "ch01_0003": result("ch01_0003", "dajer", "gemini_api"),
    }

    adjudicator = TieredAttributionAdjudicator(
        ollama=None,
        external_validator=None,
        registry=sample_registry,
    )
    adjudicator._adjudicate_turn_tier1 = lambda turn, _chapter: canned[turn.line_id]
    adjudicator._apply_reciprocal_turn_guardrail = lambda *_a, **_k: None

    turns = [
        SuspiciousTurn(
            line_id=line_id,
            chapter_number=1,
            text="text",
            current_speaker="dajer",
            detection_reason="test",
            detection_pattern="test",
            surrounding_lines=[],
            scene_text="text",
        )
        for line_id in canned
    ]

    report = adjudicator.adjudicate(turns, tmp_path, [chapter], dry_run=False)
    summary = report.summary

    assert summary["confirmed"] == 1, summary
    assert summary["reattributed"] == 1, summary
    assert summary["escalated_to_tier2"] == 1, summary
    # The total still means what it always meant.
    assert summary["local_resolved"] == summary["confirmed"] + summary["reattributed"]

    # And the confirmation did real work: flag cleared, confidence raised.
    confirmed_line = lines[0]
    assert confirmed_line.speaker == "dajer"
    assert confirmed_line.attribution_review_required is False
    assert confirmed_line.speaker_confidence == pytest.approx(0.99)
