"""A speech tag names several people; only its subject is speaking.

Every case here is a real line from the-finest-edge-of-twilight-book. A scan of
its 3,127 spoken lines found 490 carrying a genuine speech tag and 11 where the
stored speaker contradicted that tag -- the author saying outright who spoke,
and the script disagreeing. Nine were caused by two parsing faults:

  "he asked Breezy."                      -> brie        (Breezy is addressed)
  "he said, and Breezy gave a low growl." -> brie        (a following clause)
  "she asked Jarlaxle."                   -> jarlaxle    (addressed)
  "added Breezy, and Effron's head..."    -> effron_son  (a possessive)

`repair_deterministic_named_attribution` writes those at confidence 1.0 with
review_required=False, so they reached neither an LLM nor the review inbox.

The remaining two were the opposite failure: the tag was parsed correctly as
male, and micro-adjudication overruled it anyway. ch11_0149 was accepted as
Dahlia at 0.98 with "he replied, then whispered in her ear," on the next line,
because the gender guardrail was reading the model's own prose rather than the
book's narration.
"""

from __future__ import annotations

import json

import pytest

from brain.director.attribution_detector import SuspiciousTurn
from brain.director.script_generator import ScriptGenerator
from brain.validators.tiered_adjudicator import (
    TieredAttributionAdjudicator,
    _attached_tag_evidence,
)
from shared.constants import Gender
from shared.models import Character, CharacterRegistry


@pytest.fixture
def registry() -> CharacterRegistry:
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
            "brie": char("brie", "Brie", Gender.FEMALE, ["Breezy", "Catti-brie"]),
            "dahlia": char("dahlia", "Dahlia", Gender.FEMALE, ["Lady Delilah"]),
            "effron": char("effron", "Effron", Gender.MALE, []),
            "jarlaxle": char("jarlaxle", "Jarlaxle", Gender.MALE, []),
            "allefaero": char("allefaero", "Allefaero", Gender.MALE, []),
            # A real cast entry from the book. Its "Effron's" alias absorbed the
            # possessive marker, so the possessive guard never saw one.
            "effron_son": char("effron_son", "Effron's Son", Gender.MALE, ["Effron's", "son"]),
        }
    )


# The name in each of these tags is addressed, or belongs to another clause, or
# is a possessive. None of them is the speaker.
NOT_THE_SUBJECT = [
    ("he said, and Breezy gave a low growl.", Gender.MALE),
    ("he asked Breezy.", Gender.MALE),
    ("he said instead, and Breezy felt a bit embarrassed again,", Gender.MALE),
    ("he said, but waved his hand when Breezy moved to dig down deeper", Gender.MALE),
    ("she asked Jarlaxle.", Gender.FEMALE),
    ("she asked Allefaero, who sat at the side of the room", Gender.FEMALE),
    ("he said quietly, but Breezy did not notice", Gender.MALE),
    ("he asked Breezy when he neared the shore.", Gender.MALE),
    ("he replied, then whispered in her ear,", Gender.MALE),
]


@pytest.mark.parametrize(("tag", "expected_gender"), NOT_THE_SUBJECT)
def test_a_named_bystander_is_not_the_speaker(registry, tag, expected_gender) -> None:
    """A pronoun subject settles the speaker; the name after it is not one."""
    exact, _kind, gender = ScriptGenerator._dialogue_tag_evidence(tag, registry)
    assert exact is None, f"{tag!r} resolved to {exact!r}, but its subject is a pronoun"
    assert gender is expected_gender


def test_a_possessive_is_not_a_subject(registry) -> None:
    """'Effron's head snapped around' -- the head snapped, and Breezy spoke."""
    exact, _kind, _gender = ScriptGenerator._dialogue_tag_evidence(
        "added Breezy, and Effron's head snapped around", registry
    )
    assert exact == "brie"


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("Dahlia said through gritted fangs.", "dahlia"),  # plain subject
        ("said Jarlaxle.", "jarlaxle"),  # inversion
        ("Breezy asked Jarlaxle when she caught up", "brie"),  # subject + addressee
        # An alias may legitimately contain a possessive ("Doregardo's second"
        # is how the book names this character). Only a name whose final token
        # is a bare possessive `s` is dropped, so this one must survive.
        ("said Doregardo's second, Showithal Terdidy,", "showithal_terdidy"),
    ],
)
def test_a_real_subject_still_resolves(registry, tag, expected) -> None:
    """The guards must not cost us the tags that were already working."""
    if expected not in registry.characters:
        registry.characters[expected] = Character(
            id=expected,
            name="Showithal Terdidy",
            gender=Gender.MALE,
            age_range="adult",
            voice_description="v",
            aliases=["Doregardo's second", "Showithal Terdidy", "Showithal", "Terdidy"],
        )
    exact, _kind, _gender = ScriptGenerator._dialogue_tag_evidence(tag, registry)
    assert exact == expected


# ------------------------------------------------------------ the veto ----


def _turn(line_text: str, speaker: str, following: str) -> SuspiciousTurn:
    return SuspiciousTurn(
        line_id="ch11_0149",
        chapter_number=11,
        text=line_text,
        current_speaker=speaker,
        detection_reason="Attached speech tag identifies a male speaker",
        detection_pattern="gender_contradiction",
        surrounding_lines=[
            {
                "line_id": "ch11_0148",
                "text": '"You never come to the house I have built."',
                "speaker": "effron",
                "speaker_confidence": 0.6,
                "dialogue_kind": "spoken",
                "is_target": False,
            },
            {
                "line_id": "ch11_0149",
                "text": line_text,
                "speaker": speaker,
                "speaker_confidence": 0.6,
                "dialogue_kind": "spoken",
                "is_target": True,
            },
            {
                "line_id": "ch11_0150",
                "text": following,
                "speaker": "narrator",
                "speaker_confidence": 1.0,
                "dialogue_kind": "narration",
                "is_target": False,
            },
        ],
        scene_text=f"{line_text} {following}",
    )


class _StubOllama:
    """Answers with whatever the test wants the model to have said."""

    model = "stub"

    def __init__(self, speaker: str, confidence: float = 0.98) -> None:
        self._payload = json.dumps(
            {
                "speaker_id": speaker,
                "confidence": confidence,
                "reason": "The line addresses the speaker as 'mother'; Effron is her son.",
                "evidence_quote": "You will never be invited into my tower, mother,",
            }
        )

    def generate(self, prompt: str, **kwargs: object) -> str:  # noqa: ARG002 - stub
        return self._payload


def _adjudicator(registry: CharacterRegistry, speaker: str) -> TieredAttributionAdjudicator:
    return TieredAttributionAdjudicator(ollama=_StubOllama(speaker), external_validator=None, registry=registry)


LINE = '"You will never be invited into my tower, mother,"'


def test_the_attached_tag_is_read_from_the_book_not_the_model(registry) -> None:
    named, gender, tag = _attached_tag_evidence(
        _turn(LINE, "dahlia", "he replied, then whispered in her ear,"), registry
    )
    assert gender is Gender.MALE
    assert named is None
    assert tag.startswith("he replied")


def test_a_capitalised_reaction_is_not_a_speech_tag(registry) -> None:
    """'Dahlia laughed at that.' is a new sentence -- it says nothing about who spoke."""
    named, gender, tag = _attached_tag_evidence(_turn(LINE, "dahlia", "Dahlia laughed at that."), registry)
    assert (named, gender, tag) == (None, None, "")


def test_a_contradicting_gender_tag_blocks_acceptance(registry) -> None:
    """ch11_0149: 0.98 on Dahlia, with 'he replied' on the very next line."""
    result = _adjudicator(registry, "dahlia")._adjudicate_turn_tier1(
        _turn(LINE, "dahlia", "he replied, then whispered in her ear,"), None
    )
    assert result.resolver_tier == "gemini_api", "a refuted speaker must not be accepted"
    assert result.guardrail_results["attached_tag"]["passed"] is False
    assert "male speaker" in result.guardrail_results["attached_tag"]["detail"]


def test_a_named_tag_overrules_the_model(registry) -> None:
    """The author naming the speaker is the answer, not evidence to weigh."""
    result = _adjudicator(registry, "dahlia")._adjudicate_turn_tier1(
        _turn(LINE, "dahlia", "said Effron, turning away."), None
    )
    assert result.resolved_speaker == "effron"
    assert result.resolver_tier == "deterministic_tag"
    assert result.confidence == 1.0


def test_a_tag_that_agrees_leaves_the_model_alone(registry) -> None:
    result = _adjudicator(registry, "dahlia")._adjudicate_turn_tier1(
        _turn(LINE, "dahlia", "she replied, turning away."), None
    )
    assert result.resolved_speaker == "dahlia"
    assert result.resolver_tier == "local_qwen"
    assert result.guardrail_results["attached_tag"]["passed"] is True


def test_no_tag_at_all_changes_nothing(registry) -> None:
    """Most lines have no tag; the guard must be silent on them."""
    result = _adjudicator(registry, "dahlia")._adjudicate_turn_tier1(
        _turn(LINE, "dahlia", "The tower loomed over them both."), None
    )
    assert result.resolver_tier == "local_qwen"
    assert result.guardrail_results["attached_tag"]["detail"] == "no_attached_tag"
