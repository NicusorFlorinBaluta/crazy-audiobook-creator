"""The refutation pass the pipeline runs, and what a generic descriptor proves.

This pass lived only in `scripts/repair_tagged_contradictions.py` for its first
week, so a book the pipeline scripted never received it. It is now
`attribution_audit.apply_refutation_repairs`, called from
`_run_script_director`, and the script is a file-I/O wrapper around the same
function -- two copies of attribution logic drift, and the drift is invisible
until a book ships with it.

The descriptor rule here is the 2026-09-11 correction. A tag reaching
`minor_male` through "the man" was treated as contradicting *any* differently
labelled speaker. What a generic description actually establishes is a
**gender**, and nothing else: it is compatible with everyone it fits. Three
real lines paid for that distinction, and all three stored speakers were right.
"""

from __future__ import annotations

import pytest

from brain.director.attribution_audit import apply_refutation_repairs, tag_names_a_proper_noun
from shared.constants import Gender
from shared.models import Character, CharacterRegistry, ScriptChapter, ScriptLine


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
            "starling": char("starling", "Starling", Gender.FEMALE),
            "vathi": char("vathi", "Vathi", Gender.FEMALE),
            # Verbatim from isles-of-the-emberdark: descriptions, not names.
            "minor_male": char("minor_male", "Unnamed Man", Gender.MALE),
            "minor_female": char("minor_female", "Unnamed Woman", Gender.FEMALE),
            "woman_of_family": char("woman_of_family", "Woman of the Family", Gender.FEMALE, ["Woman of the Family"]),
            "one_of_the_ones_above_male": char(
                "one_of_the_ones_above_male",
                "One of the Ones Above (Male)",
                Gender.MALE,
                ["The Male Alien"],
            ),
        }
    )


def _chapter(number: int, rows: list[tuple[str, str, str]]) -> ScriptChapter:
    return ScriptChapter(
        chapter_number=number,
        chapter_title=f"Chapter {number}",
        scenes=[],
        lines=[ScriptLine(line_id=lid, speaker=speaker, text=text) for lid, speaker, text in rows],
    )


class TestGenericTagsAgainstDescriptors:
    """The 2026-09-11 correction, from the three lines it was written for."""

    def test_the_woman_does_not_contradict_the_woman_of_the_family(self, registry) -> None:
        """ch28_0089. The narration two lines up is "The woman of the family caught her"."""
        chapter = _chapter(
            28,
            [
                ("ch28_0086", "narrator", "She collapsed toward the rooftop. The woman of the family caught her."),
                ("ch28_0089", "woman_of_family", '"Your—Your Majesty?"'),
                ("ch28_0090", "narrator", "the woman said in Yolish, one of the more common languages."),
                ("ch28_0091", "woman_of_family", '"Are you all right?"'),
            ],
        )
        result = apply_refutation_repairs([chapter], registry)

        assert result["counts"]["flagged"] == 0, "a hypernym is not a contradiction"
        assert chapter.lines[1].speaker == "woman_of_family"
        assert chapter.lines[1].attribution_review_required is False

    def test_the_man_does_not_contradict_one_of_the_ones_above(self, registry) -> None:
        """ch38_0057. The line before reads "The man seemed to think he knew everything"."""
        chapter = _chapter(
            38,
            [
                ("ch38_0055", "narrator", "Dusk didn't reply. The man seemed to think he knew everything."),
                ("ch38_0057", "one_of_the_ones_above_male", '"Come now,"'),
                ("ch38_0058", "narrator", "the man said, moving as if to put his arm around Dusk's shoulders."),
            ],
        )
        result = apply_refutation_repairs([chapter], registry)

        assert result["counts"]["flagged"] == 0
        assert chapter.lines[1].speaker == "one_of_the_ones_above_male"

    def test_the_man_does_not_contradict_a_named_man(self, registry) -> None:
        """ch38_0118. The line itself is "My name is Colonel Dajer,".

        The narration calls him "the man" precisely because this is where he is
        introduced. The older rule read that as the narration declining to use
        a name it had, and flagged a correct attribution.
        """
        chapter = _chapter(
            38,
            [
                ("ch38_0118", "dusk", '"My name is Sixth of the Dusk,"'),
                ("ch38_0119", "narrator", "the man said, turning away."),
            ],
        )
        result = apply_refutation_repairs([chapter], registry)

        assert result["counts"]["flagged"] == 0

    def test_a_descriptor_of_the_other_gender_contradicts(self, registry) -> None:
        """Gender is the whole of what a generic description establishes."""
        chapter = _chapter(
            28,
            [
                ("ch28_0089", "minor_male", '"Your Majesty?"'),
                ("ch28_0090", "narrator", "the woman said in Yolish."),
            ],
        )
        result = apply_refutation_repairs([chapter], registry)

        assert result["counts"]["flagged"] == 1

    def test_a_generic_tag_contradicts_a_named_character_of_the_other_gender(self, registry) -> None:
        chapter = _chapter(
            38,
            [
                ("ch38_0057", "starling", '"Come now,"'),
                ("ch38_0058", "narrator", "the man said, turning away."),
            ],
        )
        result = apply_refutation_repairs([chapter], registry)

        assert result["counts"]["flagged"] == 1
        assert chapter.lines[0].attribution_review_required is True

    def test_a_stale_flag_from_the_older_rule_is_retracted(self, registry) -> None:
        """A rule that stops standing behind a review item must withdraw it.

        Otherwise the item sits in the queue forever, and clearing it costs the
        operator a read of the passage -- which is the spoiler this whole layer
        exists to avoid.
        """
        chapter = _chapter(
            28,
            [
                ("ch28_0089", "woman_of_family", '"Your Majesty?"'),
                ("ch28_0090", "narrator", "the woman said in Yolish."),
            ],
        )
        chapter.lines[0].attribution_review_required = True
        chapter.lines[0].attribution_review_reason = (
            "The attached speech tag describes the speaker in terms that fit 'minor_female', not 'woman_of_family'."
        )

        result = apply_refutation_repairs([chapter], registry)

        assert result["counts"]["unflagged"] == 1
        assert chapter.lines[0].attribution_review_required is False
        assert chapter.lines[0].attribution_review_reason == ""

    def test_someone_elses_review_flag_is_left_alone(self, registry) -> None:
        """Only flags this pass wrote may be retracted by this pass."""
        chapter = _chapter(
            28,
            [
                ("ch28_0089", "woman_of_family", '"Your Majesty?"'),
                ("ch28_0090", "narrator", "the woman said in Yolish."),
            ],
        )
        chapter.lines[0].attribution_review_required = True
        chapter.lines[0].attribution_review_reason = "Whisper transcription disagreed with the script."

        result = apply_refutation_repairs([chapter], registry)

        assert result["counts"]["unflagged"] == 0
        assert chapter.lines[0].attribution_review_required is True


class TestTheNamingTagStillWins:
    def test_a_tag_that_names_someone_renames_the_line(self, registry) -> None:
        chapter = _chapter(
            11,
            [
                ("ch11_0001", "starling", '"You will never be invited into my tower,"'),
                ("ch11_0002", "narrator", "said Vathi, turning away."),
            ],
        )
        result = apply_refutation_repairs([chapter], registry)

        assert result["counts"]["renamed"] == 1
        assert chapter.lines[0].speaker == "vathi"
        assert chapter.lines[0].speaker_confidence == 1.0
        assert chapter.lines[0].attribution_resolver == "deterministic_attached_tag"

    def test_a_name_in_the_tag_is_a_name_and_a_descriptor_is_not(self, registry) -> None:
        assert tag_names_a_proper_noun("said Vathi, turning away.", "vathi", registry)
        assert not tag_names_a_proper_noun("the man said to Dusk.", "minor_male", registry)


class TestDryRunChangesNothing:
    def test_apply_false_reports_without_writing(self, registry) -> None:
        chapter = _chapter(
            11,
            [
                ("ch11_0001", "starling", '"Quote,"'),
                ("ch11_0002", "narrator", "said Vathi, turning away."),
            ],
        )
        result = apply_refutation_repairs([chapter], registry, apply=False)

        assert result["counts"]["renamed"] == 1
        assert chapter.lines[0].speaker == "starling", "a dry run must not mutate the chapter"
