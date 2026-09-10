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
    _reads_as_attached_tag,
)
from scripts.repair_tagged_contradictions import _names_a_proper_noun
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
            "effron": char("effron", "Effron", Gender.MALE, ["Effron Alegni", "the warlock"]),
            "jarlaxle": char("jarlaxle", "Jarlaxle", Gender.MALE, []),
            "allefaero": char("allefaero", "Allefaero", Gender.MALE, []),
            # Verbatim from the book's cast: a possessive misparsed into a whole
            # character. Its "Effron's" alias absorbed the possessive marker so
            # the possessive guard never saw one, and its bare "Effron" alias
            # made every tag naming the real effron ambiguous.
            "effron_son": char(
                "effron_son", "Effron's Son", Gender.MALE, ["son", "Effron's Son", "Effron's", "Effron"]
            ),
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

    def __init__(self, speaker: str, confidence: float = 0.98, evidence: str | None = None) -> None:
        # The quote guardrail checks this against the scene, so a test whose
        # scene is a different one must supply its own or lose 0.15 confidence
        # to a fabricated-quote penalty rather than to the guard under test.
        self._payload = json.dumps(
            {
                "speaker_id": speaker,
                "confidence": confidence,
                "reason": "The line addresses the speaker as 'mother'; Effron is her son.",
                "evidence_quote": evidence or "You will never be invited into my tower, mother,",
            }
        )

    def generate(self, prompt: str, **kwargs: object) -> str:  # noqa: ARG002 - stub
        return self._payload


def _adjudicator(
    registry: CharacterRegistry, speaker: str, evidence: str | None = None
) -> TieredAttributionAdjudicator:
    return TieredAttributionAdjudicator(
        ollama=_StubOllama(speaker, evidence=evidence), external_validator=None, registry=registry
    )


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


def test_a_lone_pronoun_elsewhere_does_not_veto(registry) -> None:
    """ch28_0028: the pronoun belongs to someone else in the sentence.

    "the seated halfling said, then hopping to stand and bow as she neared."
    is a tag about Ghaliver, who names himself in the line above; the `she` is
    the traveller approaching him. `_dialogue_tag_evidence` falls back to any
    lone pronoun in the tag, which is fine as advice and far too weak to refuse
    an attribution on -- blocking a correct answer is worse than the failure
    the veto exists to prevent.
    """
    registry.characters["ghaliver"] = Character(
        id="ghaliver",
        name="Ghaliver Longstocking",
        gender=Gender.MALE,
        age_range="adult",
        voice_description="v",
        aliases=["Ghaliver"],
    )
    line = '"Ghaliver Longstocking at your service, traveler,"'
    tag = "the seated halfling said, then hopping to stand and bow as she neared."

    _named, gender, _tag = _attached_tag_evidence(_turn(line, "ghaliver", tag), registry)
    assert gender is None, "a pronoun that is not the subject must not refuse an attribution"

    adjudicator = _adjudicator(registry, "ghaliver", evidence="Ghaliver Longstocking at your service")
    result = adjudicator._adjudicate_turn_tier1(_turn(line, "ghaliver", tag), None)
    assert result.resolved_speaker == "ghaliver"
    assert result.resolver_tier == "local_qwen"


def test_an_alias_claiming_another_characters_name_is_ignored(registry) -> None:
    """A misparsed cast entry must not cost the real character every tag.

    This book's cast contained effron_son ("Effron's Son", 4 lines, age_range
    unknown) -- a possessive minted into a character -- carrying the alias
    "Effron", which is the real effron's name and 110 lines of dialogue. Because
    the parser abstains when a name has more than one owner, that 4-line entry
    silently suppressed every tag naming Effron across the whole book.
    """
    assert "effron_son" in registry.characters, "the fixture must reproduce the collision"
    assert "Effron" in registry.characters["effron_son"].aliases

    for tag in ("said Effron.", "Effron replied quietly."):
        exact, _kind, _gender = ScriptGenerator._dialogue_tag_evidence(tag, registry)
        assert exact == "effron", f"{tag!r} resolved to {exact!r}"


def test_a_descriptor_shared_by_twins_stays_ambiguous(registry) -> None:
    """Only canonical names are protected; a shared descriptor is genuinely unclear."""
    for cid, name in (("ilnezhara", "Ilnezhara"), ("tazmikella", "Tazmikella")):
        registry.characters[cid] = Character(
            id=cid,
            name=name,
            gender=Gender.FEMALE,
            age_range="adult",
            voice_description="v",
            aliases=["copper dragon", "sister"],
        )

    ambiguous, _kind, _gender = ScriptGenerator._dialogue_tag_evidence("said the copper dragon.", registry)
    assert ambiguous is None, "a descriptor both sisters answer to must not pick one"

    named, _kind, _gender = ScriptGenerator._dialogue_tag_evidence("Ilnezhara laughed and said,", registry)
    assert named == "ilnezhara", "their own names must still resolve"


class TestAttachedTagGate:
    """Which narrator lines count as the author naming who just spoke.

    The gate used to be "does it start with a lower-case letter" -- a proxy for
    "does it grammatically continue the quoted sentence". Precise, but it saw
    only 490 of roughly 1,064 tags in `the-finest-edge-of-twilight-book`, and
    that is where `ch13_0362` slipped through: labelled `breezy` with "Savahn
    flatly stated." on the next line.

    Capital-led narration is admitted when it opens with a name and a *speech*
    verb. Measured across two books
    (`scripts/audit_capital_led_speech_tags.py`), that separates tags from
    reactions cleanly:

                            speech-verb   reaction-verb
          trailing              391             6
          leading                 6            12
          both-same             986             4
          unparsed              194           415

    6 of 1,390 parsed speech-verb tags name the *following* speaker rather than
    the preceding one -- 0.4%. Reaction verbs lean the other way and are 90-96%
    unparseable, so they stay out.
    """

    @pytest.mark.parametrize(
        "tag",
        [
            "he replied, then whispered in her ear,",
            "said the dwarf.",
            "asked Breezy.",
        ],
    )
    def test_a_lower_case_lead_is_still_a_tag_whatever_its_verb(self, tag: str) -> None:
        """A lower-case start continues the quoted sentence, so the verb is moot."""
        assert _reads_as_attached_tag(tag)

    @pytest.mark.parametrize(
        "tag",
        [
            "Gregory replied with a blank stare.",
            "Savahn flatly stated.",
            "Jarlaxle admitted with a chuckle, but he grew more serious.",
            "Catti-brie went on, her voice low.",
        ],
    )
    def test_a_capital_led_speech_verb_is_a_tag(self, tag: str) -> None:
        assert _reads_as_attached_tag(tag)

    @pytest.mark.parametrize(
        "tag",
        [
            "Dahlia laughed at that.",
            "She turned away from the window.",
            "Breezy nodded slowly.",
            "The room fell silent.",
        ],
    )
    def test_a_reaction_is_not_a_tag(self, tag: str) -> None:
        """The original reason for the gate, and still the reason it is narrow.

        `_attached_tag_evidence` can overrule the model at confidence 1.0 with
        review suppressed, so a bystander reading here writes a wrong speaker
        that nobody is asked to check.
        """
        assert not _reads_as_attached_tag(tag)

    def test_a_refusal_to_speak_is_not_a_tag(self) -> None:
        """"Dahlia said no more" matches "<Name> said" and is the opposite."""
        assert not _reads_as_attached_tag("Dahlia said no more and let him go.")

    def test_continuing_to_walk_is_not_continuing_to_speak(self) -> None:
        """Caught on the second book: "Dusk continued on" is motion.

        It was read as a tag and pulled a name out of narration three sentences
        later, contradicting a correct attribution.
        """
        assert not _reads_as_attached_tag("Dusk continued on, remaining methodical. As he knelt by the fire,")
        assert _reads_as_attached_tag("Dusk continued, his voice flat.")

    def test_the_gate_admits_the_line_that_slipped_through(self) -> None:
        """`ch13_0362`: labelled `breezy`, tagged "Savahn flatly stated."."""
        assert _reads_as_attached_tag("Savahn flatly stated.")


class TestDescriptorIsNotAName:
    """A descriptor says who did NOT speak; only a name says who did.

    `scripts/repair_tagged_contradictions.py` rewrites a stored speaker when
    the attached tag names someone. "the man said to Dusk." resolves through a
    generic descriptor to a placeholder entry -- decisive that Dusk is the
    addressee, silent about who spoke. Renaming him to `minor_male` on that
    basis would be a downgrade dressed up as a correction, so the tool flags it
    for review instead. Measured on Isles of the Emberdark, this is the
    difference between 3 wrong renames and 3 correct flags.
    """

    @staticmethod
    def _registry() -> CharacterRegistry:
        return CharacterRegistry(
            characters={
                "gregory_antoine": Character(
                    id="gregory_antoine", name="Gregory Antoine", gender=Gender.MALE,
                    age_range="adult", voice_description="dry", aliases=["Gregory", "Gregory Antoine"],
                ),
                "minor_male": Character(
                    id="minor_male", name="Minor Male", gender=Gender.MALE,
                    age_range="adult", voice_description="plain", aliases=[],
                ),
            }
        )

    def test_a_proper_noun_in_the_tag_may_rename(self) -> None:
        assert _names_a_proper_noun("Gregory replied with a blank stare.", "gregory_antoine", self._registry())

    def test_a_descriptor_may_not(self) -> None:
        assert not _names_a_proper_noun("the man said to Dusk.", "minor_male", self._registry())

    def test_an_unknown_id_may_not(self) -> None:
        assert not _names_a_proper_noun("Gregory replied.", "nobody", self._registry())
