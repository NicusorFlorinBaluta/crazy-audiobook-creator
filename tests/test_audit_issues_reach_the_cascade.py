"""A line the audit blocks on must reach the escalation cascade.

The audit and the detector look for different things, and until 2026-09-12
neither told the other. The detector fires on collapse, low confidence and
staccato turns; the audit checks a finished script against the cast and the
source. So a line could be a release-blocking audit failure and completely
invisible to the auto-fix -- flagged forever, repaired never.

Ten lines of `isles-of-the-emberdark` were in that state. Every one carried a
confidence of 0.95 or higher, which is precisely why the one detector pattern
that might have caught them -- `low_confidence` -- did not.
"""

from __future__ import annotations

import pytest

from brain.director.attribution_detector import (
    AUDITABLE_ISSUE_KINDS,
    turns_from_audit_issues,
)
from shared.models import ScriptChapter, ScriptLine


def _line(line_id: str, speaker: str, text: str, confidence: float = 0.99) -> ScriptLine:
    return ScriptLine(
        line_id=line_id,
        speaker=speaker,
        text=text,
        speaker_confidence=confidence,
        dialogue_kind="spoken" if text.strip().startswith('"') else None,
    )


@pytest.fixture
def chapter() -> ScriptChapter:
    """The real neighbourhood of `ch23_0093`, where a female captain speaks."""
    return ScriptChapter(
        chapter_number=23,
        chapter_title="Chapter 23",
        lines=[
            _line("ch23_0092", "narrator", "It was just that he was a tad overly fond of it."),
            _line("ch23_0093", "deep_voice", '"Hey,"', 1.0),
            _line("ch23_0094", "narrator", "a commanding female voice said in Star's earpiece."),
            _line("ch23_0095", "deep_voice", '"Are you wasting time again?"', 1.0),
            _line("ch23_0096", "starling", '"No, Captain."', 0.95),
            _line("ch23_0097", "captain", '"Then why isn\'t my engine working yet?"', 0.95),
        ],
    )


def _issue(line_id: str, kind: str = "absent_character_in_chapter", **extra) -> dict:
    return {
        "chapter_number": 23,
        "line_id": line_id,
        "speaker": "deep_voice",
        "kind": kind,
        "message": "Character 'deep_voice' has no presence or mention in Chapter 23",
        **extra,
    }


class TestAuditFindingsBecomeSuspiciousTurns:
    def test_a_blocked_line_is_flagged(self, chapter) -> None:
        turns = turns_from_audit_issues([chapter], [_issue("ch23_0093")])
        assert [turn.line_id for turn in turns] == ["ch23_0093"]
        assert turns[0].detection_pattern == "audit_blocking_issue"

    def test_confidence_is_not_consulted(self, chapter) -> None:
        """These lines are wrong *at 1.00*. Certainty is part of the defect."""
        turns = turns_from_audit_issues([chapter], [_issue("ch23_0093")])
        assert turns[0].current_speaker == "deep_voice"
        assert chapter.lines[1].speaker_confidence == 1.0

    def test_the_reason_carries_what_the_audit_found(self, chapter) -> None:
        """The adjudicator is shown why, not merely that, the line is suspect."""
        turns = turns_from_audit_issues([chapter], [_issue("ch23_0093")])
        assert "absent_character_in_chapter" in turns[0].detection_reason
        assert "no presence or mention" in turns[0].detection_reason

    def test_an_expected_speaker_is_passed_along(self, chapter) -> None:
        """`ch55_0026` -- the audit already knows the tag names 'guard'."""
        issue = _issue("ch23_0093", kind="named_tag", expected_speaker="captain")
        turns = turns_from_audit_issues([chapter], [issue])
        assert "'captain'" in turns[0].detection_reason

    def test_the_window_shows_the_evidence(self, chapter) -> None:
        """The tag that settles `ch23_0093` is on the *next* line."""
        turns = turns_from_audit_issues([chapter], [_issue("ch23_0093")])
        window = " ".join(entry["text"] for entry in turns[0].surrounding_lines)
        assert "a commanding female voice" in window
        assert '"No, Captain."' in window

    def test_several_findings_on_one_line_produce_one_turn(self, chapter) -> None:
        turns = turns_from_audit_issues(
            [chapter],
            [_issue("ch23_0093"), _issue("ch23_0093", kind="unknown_speaker")],
        )
        assert len(turns) == 1


class TestWhatItRefuses:
    def test_a_line_the_detector_already_flagged_is_not_duplicated(self, chapter) -> None:
        turns = turns_from_audit_issues([chapter], [_issue("ch23_0093")], already_flagged={"ch23_0093"})
        assert turns == []

    def test_a_finding_with_no_line_is_skipped(self, chapter) -> None:
        """Cast-hygiene findings have no single line to re-attribute."""
        turns = turns_from_audit_issues([chapter], [{"kind": "absent_character_in_chapter"}])
        assert turns == []

    def test_an_unknown_issue_kind_is_skipped(self, chapter) -> None:
        """The list is explicit so a new audit check cannot silently start
        driving GPU work before anyone decides it should."""
        turns = turns_from_audit_issues([chapter], [_issue("ch23_0093", kind="pacing_anomaly")])
        assert turns == []

    def test_a_finding_for_a_line_not_in_these_chapters_is_skipped(self, chapter) -> None:
        turns = turns_from_audit_issues([chapter], [_issue("ch99_0001")])
        assert turns == []

    def test_no_findings_costs_nothing(self, chapter) -> None:
        assert turns_from_audit_issues([chapter], []) == []


def test_the_blocking_kinds_are_the_ones_the_audit_emits() -> None:
    """A kind that blocks release but is missing here is a line nothing repairs."""
    assert "absent_character_in_chapter" in AUDITABLE_ISSUE_KINDS
    assert "unknown_speaker" in AUDITABLE_ISSUE_KINDS
    assert "named_tag" in AUDITABLE_ISSUE_KINDS


class TestTheAbsentSpeakerIsTakenOffTheBallot:
    """Escalating `ch21_0173` was not enough on its own.

    Shown the line and its neighbours, the model reasoned correctly -- *"This
    explicitly attributes the preceding dialogue to the male character
    (Dusk/Deep Voice)"* -- and then returned `deep_voice` at confidence 1.00.
    It picked the wrong id because that id was on the candidate list and
    `dusk`, the person it had just named, was not: the list is built from the
    speakers of neighbouring lines, and here every neighbour is narration.

    So the audit's finding is used twice. It proves `deep_voice` is absent from
    the chapter, which takes it off the list; and the scene text names the
    people who are present, which puts them on it.
    """

    @pytest.fixture
    def prologue(self) -> ScriptChapter:
        return ScriptChapter(
            chapter_number=21,
            chapter_title="Chapter 21",
            lines=[
                _line("ch21_0163", "narrator", "Sak had landed on Vathi's shoulder. Dusk frowned."),
                _line("ch21_0169", "narrator", "He'd traveled the darkness with her."),
                _line("ch21_0173", "deep_voice", '"What?"', 1.0),
                _line("ch21_0174", "narrator", "he asked, his voice hoarse."),
                _line("ch21_0175", "vathi", '"We found instructions in the machine,"', 0.98),
            ],
        )

    @pytest.fixture
    def registry(self):
        from shared.constants import Gender
        from shared.models import Character, CharacterRegistry

        def char(cid, name, gender, aliases=()):
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
                "dusk": char("dusk", "Dusk", Gender.MALE),
                "vathi": char("vathi", "Vathi", Gender.FEMALE),
                "deep_voice": char("deep_voice", "Deep Voice", Gender.MALE, ["The Friend"]),
            }
        )

    def _turn(self, prologue, registry):
        issue = _issue("ch21_0173")
        issue["speaker"] = "deep_voice"
        return turns_from_audit_issues([prologue], [issue], registry=registry)[0]

    def test_the_absent_speaker_is_excluded(self, prologue, registry) -> None:
        assert self._turn(prologue, registry).excluded_speakers == ["deep_voice"]

    def test_the_people_the_scene_names_become_candidates(self, prologue, registry) -> None:
        """`dusk` is in the prose but speaks on none of the neighbouring lines."""
        candidates = self._turn(prologue, registry).extra_candidates
        assert "dusk" in candidates
        assert "deep_voice" not in candidates, "the excluded speaker must not come back this way"

    def test_only_the_absent_character_finding_constrains_the_ballot(self, prologue, registry) -> None:
        """A `named_tag` finding is about the tag, not about who is in the chapter."""
        issue = _issue("ch21_0173", kind="named_tag", expected_speaker="dusk")
        turn = turns_from_audit_issues([prologue], [issue], registry=registry)[0]
        assert turn.excluded_speakers == []
        assert turn.extra_candidates == []

    def test_without_a_registry_nothing_is_added(self, prologue) -> None:
        turn = turns_from_audit_issues([prologue], [_issue("ch21_0173")])[0]
        assert turn.excluded_speakers == ["deep_voice"]
        assert turn.extra_candidates == []

    def test_a_detector_turn_carries_no_constraint(self, prologue) -> None:
        """Every other turn must reach the adjudicator exactly as before."""
        from brain.director.attribution_detector import build_turn_window

        turn = build_turn_window(prologue, 2, reason="low confidence", pattern="low_confidence")
        assert turn.excluded_speakers == []
        assert turn.extra_candidates == []


class TestAnAuditFlaggedLineClearsAHigherBar:
    """The bridge must not turn a loud failure into a silent one.

    The stored speaker on these lines is absent from the chapter, so the audit
    re-flags it on every run and the line stays blocking. Replace it with the
    *wrong* speaker who happens to be present and the audit goes quiet -- the
    line is still wrong, and nothing will ever say so again.

    Measured over the ten Emberdark lines on 2026-09-12. The three the model
    got right came back at 1.00, 0.98 and 1.00; the two it got wrong came back
    at 0.95 -- `ch28_0117` -> `starling`, who is the person being thanked, and
    `ch49_0039` -> `insect_god`, which also split a two-line utterance across
    two speakers. Below the bar the answer is recorded as a proposal instead of
    written as an edit.
    """

    def test_the_floor_is_above_the_ordinary_auto_accept(self) -> None:
        from brain.validators.tiered_adjudicator import AUDIT_ISSUE_AUTO_ACCEPT

        assert AUDIT_ISSUE_AUTO_ACCEPT > 0.95, "0.95 is where both wrong answers landed"

    def test_the_correct_answers_clear_it_and_the_wrong_ones_do_not(self) -> None:
        from brain.validators.tiered_adjudicator import AUDIT_ISSUE_AUTO_ACCEPT

        correct = {"ch21_0173": 1.00, "ch23_0093": 0.98, "ch23_0095": 1.00}
        wrong = {"ch28_0117": 0.95, "ch49_0039": 0.95}
        for line_id, confidence in correct.items():
            assert confidence >= AUDIT_ISSUE_AUTO_ACCEPT, line_id
        for line_id, confidence in wrong.items():
            assert confidence < AUDIT_ISSUE_AUTO_ACCEPT, line_id

    def test_only_audit_turns_are_held_to_it(self) -> None:
        """Detector findings keep the threshold they were measured against."""
        from brain.director.attribution_detector import build_turn_window

        chapter = ScriptChapter(
            chapter_number=1,
            chapter_title="One",
            lines=[_line("ch01_0001", "dusk", '"Quote."', 0.96)],
        )
        turn = build_turn_window(chapter, 0, reason="low confidence", pattern="low_confidence")
        assert turn.detection_pattern != "audit_blocking_issue"
