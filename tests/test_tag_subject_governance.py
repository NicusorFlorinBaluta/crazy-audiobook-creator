"""A name governed by a preposition is that preposition's object, not the subject.

`_dialogue_tag_evidence`'s post-verbal branch has always rejected a candidate
with an intervening preposition. The pre-verbal branch checked only what sits
*between* the name and the speech verb, never what precedes the name -- so

    "He squatted near Dusk and muttered,"   ->  dusk

read the object of "near" as the speaker. That is not a reporting nuisance:
`repair_deterministic_named_attribution` acts on a `named_tag` finding by
renaming the line at confidence 1.0 with review disabled. On the library as it
stood, the next script-director run over `isles-of-the-emberdark` would have
written `dusk` over Dajer's line, silently.

Measured over both scripted books -- 3,274 `named_tag` readings -- the rule
changes 7 readings across 4 tags, every one of them a prepositional object, and
loses no correct reading. On the audit it removes one false positive and
surfaces one true one (`ch24_0096`, where Athrogate's rhyming couplet was stored
as Jarlaxle).

The sibling rule that was measured and **rejected**: treating a clause boundary
between the name and the verb as disqualifying. "and" there is nearly always
verb coordination sharing one subject -- "Allefaero shrugged and asked,",
"Breezy nodded and continued," -- and that rule destroyed 23 correct readings to
catch 9. Same shape as the co-occurrence veto removed on 2026-09-10: a plausible
rule that fires hardest on what it must not touch.
"""

from __future__ import annotations

import pytest

from brain.director.script_generator import ScriptGenerator
from shared.constants import Gender
from shared.models import Character, CharacterRegistry


@pytest.fixture
def registry() -> CharacterRegistry:
    def char(cid: str, name: str, gender: Gender, aliases: list[str] | None = None) -> Character:
        return Character(
            id=cid,
            name=name,
            gender=gender,
            age_range="adult",
            voice_description=f"{name} voice",
            aliases=aliases or [],
        )

    return CharacterRegistry(
        characters={
            "narrator": char("narrator", "Narrator", Gender.OTHER),
            "dusk": char("dusk", "Sixth of the Dusk", Gender.MALE, ["Dusk"]),
            "dajer": char("dajer", "Colonel Dajer", Gender.MALE, ["Dajer"]),
            "savahn": char("savahn", "Savahn", Gender.FEMALE),
            "perrywinkle_shin": char("perrywinkle_shin", "Perrywinkle Shin", Gender.MALE, ["Perrywinkle Shin"]),
            "allefaero": char("allefaero", "Allefaero", Gender.MALE),
            "breezy": char("breezy", "Breezy", Gender.FEMALE),
            "effron": char("effron", "Effron", Gender.MALE),
            # Verbatim from isles-of-the-emberdark: the alias matches inside the
            # full name, with a preposition immediately before it.
            "general_second_of_saplings": char(
                "general_second_of_saplings",
                "General Second of Saplings",
                Gender.MALE,
                ["Saplings", "General Second of Saplings"],
            ),
        }
    )


class TestAPrepositionalObjectIsNotTheSpeaker:
    @pytest.mark.parametrize(
        "tag",
        [
            "He squatted near Dusk and muttered,",
            "He looked to Dusk as he continued,",
            "He turned toward Dusk and said,",
        ],
    )
    def test_a_governed_name_does_not_become_the_speaker(self, registry, tag) -> None:
        named, kind, _gender = ScriptGenerator._dialogue_tag_evidence(tag, registry)
        assert named is None, f"{tag!r} named a prepositional object"
        assert kind != "named_tag"

    def test_the_savahn_case(self, registry) -> None:
        """ch14_0300: Perrywinkle Shin speaks; Savahn is the object of "of"."""
        tag = "Perrywinkle Shin shook his head, but very differently than the lighthearted manner of Savahn, and said,"
        named, _kind, _gender = ScriptGenerator._dialogue_tag_evidence(tag, registry)
        assert named != "savahn"

    def test_a_pronoun_subject_still_yields_its_gender(self, registry) -> None:
        """Suppressing the name must not suppress what the tag does establish."""
        named, kind, gender = ScriptGenerator._dialogue_tag_evidence("He squatted near Dusk and muttered,", registry)
        assert named is None
        assert kind == "pronoun_gender"
        assert gender is Gender.MALE


class TestTheNameKeepsItsOwnPreposition:
    def test_an_alias_inside_a_longer_name_is_not_governed(self, registry) -> None:
        """ "Second of Saplings said" -- `of` belongs to the name, governs nothing.

        The alias "Saplings" matches at the last token, so the naive rule sees a
        preposition immediately before it and throws away a correct reading.
        """
        named, kind, _gender = ScriptGenerator._dialogue_tag_evidence(
            "Second of Saplings said, the outburst unsettling his grey and brown Aviar.", registry
        )
        assert named == "general_second_of_saplings"
        assert kind == "named_tag"


class TestVerbCoordinationSurvives:
    """The rule that was measured and rejected must stay rejected.

    "and" between a name and a speech verb coordinates two verbs of one
    subject far more often than it starts a new clause. Rejecting on it cost 23
    correct readings for 9 catches.
    """

    @pytest.mark.parametrize(
        ("tag", "expected"),
        [
            ("Allefaero shrugged and asked,", "allefaero"),
            ("Breezy nodded and continued,", "breezy"),
            ("Effron swallowed hard and whispered,", "effron"),
            ("For the first time in a long time, Breezy laughed and agreed.", "breezy"),
        ],
    )
    def test_a_coordinated_verb_still_names_the_speaker(self, registry, tag, expected) -> None:
        named, kind, _gender = ScriptGenerator._dialogue_tag_evidence(tag, registry)
        assert (named, kind) == (expected, "named_tag")


class TestOrdinaryTagsAreUntouched:
    @pytest.mark.parametrize(
        ("tag", "expected"),
        [
            ("Dajer muttered,", "dajer"),
            ("said Savahn, turning away.", "savahn"),
            ("Breezy said quietly,", "breezy"),
        ],
    )
    def test_a_plain_tag_still_names_its_subject(self, registry, tag, expected) -> None:
        named, _kind, _gender = ScriptGenerator._dialogue_tag_evidence(tag, registry)
        assert named == expected
