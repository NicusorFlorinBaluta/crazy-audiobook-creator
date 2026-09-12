"""An indefinite article in a speech tag does not name anybody.

Some cast entries are named after their role -- Emberdark has characters whose
names are literally "Guard", "Captain" and "Senator" -- so *"a guard snapped,"*
matched the character called `guard` and was read as the author naming a
speaker.

It blocked the release gate on `ch55_0026`:

    ch55_0024  guard_woman  '"My orders,"'
    ch55_0025  narrator     'a guard snapped,'
    ch55_0026  guard_woman  '"are to keep these prisoners prisoners."'
    ch55_0027  narrator     'Starling gritted her teeth, locking gazes with the woman'

Two guards are in the room. The next sentence says which one spoke, and it is
not the one the tag was taken to name. An indefinite article is a refusal to
identify an individual -- that is what makes it indefinite.

Measured over both books before shipping: 167 named-tag readings, 3 of them
behind an "a"/"an", and the other two (`ch22_0197`, `ch63_0047`) already agree
with the line they sit beside. No correct reading is lost.
"""

from __future__ import annotations

import pytest

from brain.director.script_generator import ScriptGenerator
from shared.constants import Gender
from shared.models import Character, CharacterRegistry


@pytest.fixture
def registry() -> CharacterRegistry:
    def char(cid: str, name: str, gender: Gender, aliases=()) -> Character:
        return Character(
            id=cid,
            name=name,
            gender=gender,
            age_range="adult",
            voice_description=f"{name} voice",
            aliases=list(aliases),
        )

    return CharacterRegistry(
        characters={
            "narrator": char("narrator", "Narrator", Gender.OTHER),
            "guard": char("guard", "Guard", Gender.MALE),
            "guard_woman": char("guard_woman", "Saja", Gender.FEMALE, ["Saja", "Guard"]),
            "vathi": char("vathi", "Vathi", Gender.FEMALE),
            "senator_male": char("senator_male", "Senator", Gender.MALE),
        }
    )


class TestAnIndefiniteArticleBlocksTheNaming:
    def test_a_guard_snapped_names_nobody(self, registry) -> None:
        assert ScriptGenerator._dialogue_tag_evidence("a guard snapped,", registry) == (None, None, None)

    def test_a_senator_said_names_nobody(self, registry) -> None:
        """`ch63_0047`, which happened to agree with its line anyway."""
        who, kind, _ = ScriptGenerator._dialogue_tag_evidence(
            "a senator said, looking over Vathi's shoulder.", registry
        )
        assert who is None
        assert kind != "named_tag"

    def test_an_before_a_name_is_treated_the_same(self, registry) -> None:
        assert ScriptGenerator._dialogue_tag_evidence("an officer said.", registry)[0] is None


class TestWhatStillNames:
    def test_a_definite_article_still_names(self, registry) -> None:
        """ "the guard" can point at a guard already established in the scene."""
        who, kind, _ = ScriptGenerator._dialogue_tag_evidence("the guard snapped,", registry)
        assert (who, kind) == ("guard", "named_tag")

    def test_a_bare_proper_noun_still_names(self, registry) -> None:
        who, kind, _ = ScriptGenerator._dialogue_tag_evidence("Saja snapped,", registry)
        assert (who, kind) == ("guard_woman", "named_tag")

    def test_an_inverted_tag_still_names(self, registry) -> None:
        """The 2026-09-06 case: the book's tag outranks the model."""
        who, kind, _ = ScriptGenerator._dialogue_tag_evidence("said Vathi, turning away.", registry)
        assert (who, kind) == ("vathi", "named_tag")

    def test_an_article_earlier_in_the_tag_does_not_block(self, registry) -> None:
        """Only the word immediately before the name counts."""
        who, _, _ = ScriptGenerator._dialogue_tag_evidence("a moment later Vathi said,", registry)
        assert who == "vathi"
