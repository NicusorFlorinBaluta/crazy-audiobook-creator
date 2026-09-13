"""Prose attributes dialogue two ways; the pipeline only ever read one.

A speech tag -- "said Effron" -- is handled everywhere. An **action beat**
sharing the quote's paragraph was not, and it is just as decisive:

    Effron spun around and glared at her. "Never. Should you come to my
    residence, well?" He looked down at the dust.

No speech verb, and the paragraph is unambiguously Effron's. The prologue of
`the-finest-edge-of-twilight` shipped four consecutive lines inverted for want
of this, stored confidently wrong at 0.95-0.98 with no deterministic layer able
to say otherwise, and re-adjudicating them with today's best path got one of
four.

The paragraph break carries as much weight as the beat:

    "Go to your rest, Dahlia." Effron turned for the door.

    "I know where to find you."

The beat closes the *first* quote; the second is a new paragraph and a
different speaker. Reading a beat as attributing whatever follows it gets that
exactly backwards.

Measured over both scripted books: 2,678 quotes covered, **98.2%** agreeing
with the stored speaker, and most of the 49 disagreements are the script being
wrong -- "Dahlia countered." stored as effron, "Catti-brie reminded her da."
stored as bruenor.

Each guard below was earned by a real line that broke an earlier draft, and
each one is cheap to lose by accident, so they are pinned here.
"""

from __future__ import annotations

import pytest

from brain.director.attribution_audit import (
    action_beat_attributions,
    apply_refutation_repairs,
    beat_subject,
)
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
            "effron": char("effron", "Effron", Gender.MALE),
            "dahlia": char("dahlia", "Dahlia", Gender.FEMALE),
            "driver_portly": char("driver_portly", "Portly Driver", Gender.MALE),
        }
    )


def _chapter(rows):
    """Build a chapter, laying the text out so `rows` describes real paragraphs.

    Each row is (line_id, speaker, text, starts_new_paragraph).
    """
    lines, parts, cursor = [], [], 0
    for lid, speaker, text, new_para in rows:
        if parts:
            sep = "\n\n" if new_para else " "
            parts.append(sep)
            cursor += len(sep)
        lines.append(
            ScriptLine(
                line_id=lid,
                speaker=speaker,
                text=text,
                source_start=cursor,
                source_end=cursor + len(text),
                dialogue_kind=None if speaker == "narrator" else "spoken",
            )
        )
        parts.append(text)
        cursor += len(text)
    return ScriptChapter(chapter_number=1, chapter_title="Prologue", scenes=[], lines=lines), "".join(parts)


PROLOGUE = [
    ("ch01_0289", "effron", '"Go to your rest, Dahlia."', True),
    ("ch01_0290", "narrator", "Effron turned for the door.", False),
    ("ch01_0291", "effron", '"I know where to find you."', True),
    ("ch01_0294", "narrator", "Effron spun around and glared at her.", True),
    ("ch01_0295", "dahlia", '"Never. Should you come to my residence, well?"', False),
]


class TestTheParagraphIsTheUnit:
    def test_a_beat_attributes_the_quote_it_shares_a_paragraph_with(self, registry) -> None:
        chapter, text = _chapter(PROLOGUE)
        beats = action_beat_attributions(chapter, text, registry)
        assert beats["ch01_0295"] == "effron"

    def test_a_trailing_beat_attributes_the_quote_above_it(self, registry) -> None:
        chapter, text = _chapter(PROLOGUE)
        assert action_beat_attributions(chapter, text, registry)["ch01_0289"] == "effron"

    def test_a_paragraph_break_severs_the_beat(self, registry) -> None:
        """ "I know where to find you." is a new paragraph -- and is Dahlia's."""
        chapter, text = _chapter(PROLOGUE)
        assert "ch01_0291" not in action_beat_attributions(chapter, text, registry)

    def test_nothing_is_claimed_without_source_offsets(self, registry) -> None:
        chapter, text = _chapter(PROLOGUE)
        chapter.lines[0].source_start = None
        assert action_beat_attributions(chapter, text, registry) == {}

    def test_a_paragraph_naming_two_characters_claims_nothing(self, registry) -> None:
        chapter, text = _chapter(
            [
                ("a1", "narrator", "Effron turned away.", True),
                ("a2", "narrator", "Dahlia watched him go.", False),
                ("a3", "effron", '"Enough."', False),
            ]
        )
        assert action_beat_attributions(chapter, text, registry) == {}


class TestWhatCountsAsABeat:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Effron spun around and glared at her.", "effron"),
            ("Dahlia countered.", "dahlia"),
            ("She looked from Effron to the door.", None),  # pronoun subject
            ("Effron's hand trembled.", None),  # possessive, not the subject
            ("Effronia laughed.", None),  # matched inside a longer word
            ("the man said to Dusk.", None),  # lower case, not a beat
        ],
    )
    def test_only_a_named_subject_counts(self, registry, text, expected) -> None:
        assert beat_subject(text, registry) == expected

    def test_a_beat_naming_two_people_abstains(self, registry) -> None:
        """ch08_0244. Breezy leads the sentence; Holiday says the line.

        Breezy grinned and strode forward, but Holiday grabbed her by the
        arm and held her back. "Fight's over, I say."
        """
        chapter, text = _chapter(
            [
                (
                    "c1",
                    "narrator",
                    "Dahlia grinned and strode forward, but Effron grabbed her by the arm and held her back.",
                    True,
                ),
                ("c2", "dahlia", '"Fight is over, I say."', False),
            ]
        )
        assert action_beat_attributions(chapter, text, registry) == {}

    def test_one_name_matched_by_two_cast_entries_is_still_one_person(self, registry) -> None:
        """A misparsed entry must not make a clear beat look ambiguous.

        `the-finest-edge-of-twilight` carries `effron_child` with the alias
        "Effron" beside the real `effron`. Counting matching character ids
        rather than spans made "Effron spun around and glared at her." look
        like it named two people, and abstained on the prologue's one anchor.
        """
        registry.characters["effron_child"] = Character(
            id="effron_child",
            name="Effron's Child",
            gender=Gender.MALE,
            age_range="child",
            voice_description="v",
            aliases=["Effron"],
        )
        chapter, text = _chapter(PROLOGUE)
        assert action_beat_attributions(chapter, text, registry)["ch01_0295"] == "effron"

    def test_a_fragment_naming_a_rival_blocks_the_beat(self, registry) -> None:
        """ch26_0321. A fragment cannot be a beat, but it can still name a rival.

            Starling glanced over her shoulder. The captain met her eyes, then
            turned and walked out. "That girl," Crow snapped, "will wish she'd
            never taken this job."

        Only Starling heads a complete sentence, so an earlier draft handed the
        quote to her. "Crow snapped," ends in a comma -- no beat -- and
        "snapped" is not in the speech-verb list either, so nothing downstream
        caught it. Every mention in the paragraph has to agree.
        """
        chapter, text = _chapter(
            [
                ("d1", "narrator", "Dahlia glanced over her shoulder.", True),
                ("d2", "dahlia", '"That girl,"', False),
                ("d3", "narrator", "Effron snapped,", False),
            ]
        )
        assert action_beat_attributions(chapter, text, registry) == {}

    def test_a_fragment_running_into_the_quote_is_not_a_beat(self, registry) -> None:
        """ch01_0130: the quote is the narration's grammatical object.

        "...as the coach gained speed despite the driver's cries of "Whoa!""
        reads as a beat by Dahlia and attributes the driver's shout to her.
        """
        chapter, text = _chapter(
            [
                (
                    "ch01_0129",
                    "narrator",
                    "Dahlia dropped silently to the back of the coach, then climbed across the roof "
                    "as the coach gained speed despite the driver's cries of",
                    True,
                ),
                ("ch01_0130", "driver_portly", '"Whoa!"', False),
            ]
        )
        assert action_beat_attributions(chapter, text, registry) == {}


class TestItRunsInThePass:
    def test_the_prologue_line_is_corrected(self, registry) -> None:
        chapter, text = _chapter(PROLOGUE)
        result = apply_refutation_repairs(
            [chapter],
            registry,
            chapter_texts={1: text},
        )
        assert result["counts"]["beat_attributed"] == 1
        by = {line.line_id: line for line in chapter.lines}
        assert by["ch01_0295"].speaker == "effron"
        assert by["ch01_0295"].attribution_resolver == "deterministic_action_beat"
        assert by["ch01_0295"].attribution_review_required is False

    def test_a_speech_tag_outranks_a_beat(self, registry) -> None:
        """The 2026-09-06 principle. A tag is the author saying it outright."""
        chapter, text = _chapter(
            [
                ("b1", "narrator", "Effron spun around and glared at her.", True),
                ("b2", "dahlia", '"Never."', False),
                ("b3", "narrator", "said Dahlia.", False),
            ]
        )
        result = apply_refutation_repairs([chapter], registry, chapter_texts={1: text})
        assert result["counts"]["beat_attributed"] == 0
        assert chapter.lines[1].speaker == "dahlia"

    def test_without_source_text_the_layer_is_skipped(self, registry) -> None:
        chapter, _text = _chapter(PROLOGUE)
        result = apply_refutation_repairs([chapter], registry)
        assert result["counts"]["beat_attributed"] == 0
        assert chapter.lines[4].speaker == "dahlia", "unchanged when the layer cannot run"

    def test_a_dry_run_reports_without_mutating(self, registry) -> None:
        chapter, text = _chapter(PROLOGUE)
        result = apply_refutation_repairs([chapter], registry, chapter_texts={1: text}, apply=False)
        assert result["counts"]["beat_attributed"] == 1
        assert chapter.lines[4].speaker == "dahlia"
