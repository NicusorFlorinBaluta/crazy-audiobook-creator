"""Deterministic refutations, and what a model is allowed to say about them.

Extracted from `test_block_adjudication.py` when block adjudication was removed
on 2026-09-10. The subject was never block adjudication -- these guard the
independent post-hoc possessive check, the layer that refuses to let a model
restate a speaker the text has already ruled out, and the resolver that acts
when the scene leaves exactly one alternative. All three outlived the feature
whose failure motivated them.
"""

from __future__ import annotations

from types import SimpleNamespace

from brain.director.attribution_audit import (
    detect_possessive_contradictions,
    resolve_refuted_by_unique_candidate,
)
from brain.validators.gemini_validation import (
    DETERMINISTIC_REVIEW_PREFIX,
    _is_deterministic_contradiction,
)
from shared.constants import Gender
from shared.models import Character, CharacterRegistry, ScriptChapter, ScriptLine


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
            lines=[ScriptLine(line_id=lid, speaker=speaker, text=text) for lid, speaker, text in rows],
        )

    def test_the_ch11_tower_contradiction_is_caught(self) -> None:
        chapter = self._chapter(
            11,
            [
                ("ch11_0147", "effron", '"And I have even done you small favors, as you mention."'),
                ("ch11_0148", "effron", '"You have never invited me to be a guest in your tower."'),
                ("ch11_0149", "effron", '"You will never be invited into my tower, mother,"'),
            ],
        )
        found = detect_possessive_contradictions([chapter])
        assert len(found) == 1
        assert found[0]["speaker"] == "effron"
        assert found[0]["noun"] == "tower"
        assert found[0]["claimed_line_id"] == "ch11_0149"
        assert found[0]["disclaimed_line_id"] == "ch11_0148"

    def test_a_contrast_inside_one_line_is_not_a_contradiction(self) -> None:
        """ "Your tower is grander than my tower" is one speaker, two towers."""
        chapter = self._chapter(
            1,
            [
                ("ch01_0001", "effron", '"Your tower is grander than my tower, mother."'),
                ("ch01_0002", "effron", '"That has always been true."'),
            ],
        )
        assert detect_possessive_contradictions([chapter]) == []

    def test_two_speakers_may_disagree_about_ownership(self) -> None:
        """The check is about ONE speaker contradicting themselves."""
        chapter = self._chapter(
            1,
            [
                ("ch01_0001", "dahlia", '"You have never invited me into your tower."'),
                ("ch01_0002", "effron", '"You will never be invited into my tower."'),
            ],
        )
        assert detect_possessive_contradictions([chapter]) == []

    def test_a_narrator_line_does_not_break_the_run(self) -> None:
        """Speech tags sit between turns; the run is the speaker's, not the text's."""
        chapter = self._chapter(
            11,
            [
                ("ch11_0148", "effron", '"...a guest in your tower."'),
                ("ch11_0149", "effron", '"You will never enter my tower."'),
            ],
        )
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
        chapter = self._chapter(
            25,
            [
                ("ch25_0101", "dusk", '"Setting off without knowing your way home is stupid."'),
                ("ch25_0103", "dusk", "\"No, I don't know how we'll find our way back,\""),
            ],
        )
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


class TestUniqueCandidateResolver:
    """Resolve a refuted line without anyone reading the book.

    Flagging for review is the obvious response to a refutation and the wrong
    one here: reviewing an attribution means reading the passage, and the
    operator has not read the book. The 2026-09-04 record makes the same
    argument about cast merges -- approving one "means reading the verbatim
    excerpts that justify it, which is a plot summary of a book the operator
    has not read yet". So anything resolvable without a human reading should be.

    A refutation says who did *not* speak. Where the scene leaves exactly one
    other candidate it allows, that is an answer. Where it leaves two, or none,
    the rule declines -- it cannot guess and does not try.
    """

    @staticmethod
    def _registry(**genders: str) -> CharacterRegistry:
        return CharacterRegistry(
            characters={
                cid: Character(
                    id=cid,
                    name=cid.title(),
                    gender=Gender(g),
                    age_range="adult",
                    voice_description="x",
                    aliases=[],
                )
                for cid, g in genders.items()
            }
        )

    @staticmethod
    def _chapter(rows):
        return ScriptChapter(
            chapter_number=11,
            chapter_title="Eleven",
            lines=[ScriptLine(line_id=i, speaker=s, text=t) for i, s, t in rows],
        )

    def test_the_ch11_cascade_resolves_to_dahlia(self) -> None:
        """The case block adjudication was built for and never fixed."""
        chapter = self._chapter(
            [
                ("ch11_0145", "dahlia", '"Of course!"'),
                ("ch11_0147", "effron", '"And I have done you small favors."'),
                ("ch11_0148", "effron", '"You have never invited me to be a guest in your tower."'),
                ("ch11_0149", "effron", '"You will never be invited into my tower, mother,"'),
            ]
        )
        registry = self._registry(effron="male", dahlia="female")
        proposals = resolve_refuted_by_unique_candidate([chapter], registry)
        assert len(proposals) == 1
        assert proposals[0]["line_id"] == "ch11_0148"
        assert proposals[0]["from"] == "effron"
        assert proposals[0]["to"] == "dahlia"
        assert proposals[0]["source"] == "possessive_contradiction"

    def test_a_gendering_tag_resolves_when_one_candidate_fits(self) -> None:
        chapter = ScriptChapter(
            chapter_number=62,
            chapter_title="Sixty-Two",
            lines=[
                ScriptLine(line_id="a", speaker="dusk", text='"Look there."'),
                ScriptLine(line_id="b", speaker="vathi", text='"Are you done?"'),
                ScriptLine(line_id="c", speaker="narrator", text="he asked Vathi, leaning closer to her."),
            ],
        )
        registry = self._registry(dusk="male", vathi="female")
        proposals = resolve_refuted_by_unique_candidate([chapter], registry)
        assert [(p["line_id"], p["to"]) for p in proposals] == [("b", "dusk")]

    def test_two_candidates_are_left_alone(self) -> None:
        """It cannot guess between them, so it does not."""
        chapter = ScriptChapter(
            chapter_number=47,
            chapter_title="Forty-Seven",
            lines=[
                ScriptLine(line_id="a", speaker="starling", text='"One."'),
                ScriptLine(line_id="b", speaker="chrysalis", text='"Two."'),
                ScriptLine(line_id="c", speaker="deep_voice", text='"Yes,"'),
                ScriptLine(line_id="d", speaker="narrator", text="she said, muffled from under the table."),
            ],
        )
        registry = self._registry(deep_voice="male", starling="female", chrysalis="female")
        assert resolve_refuted_by_unique_candidate([chapter], registry) == []

    def test_no_candidate_means_no_change(self) -> None:
        """The known false positive: "your way home" against "our way back"."""
        chapter = ScriptChapter(
            chapter_number=25,
            chapter_title="Twenty-Five",
            lines=[
                ScriptLine(line_id="a", speaker="dusk", text='"Knowing your way home matters."'),
                ScriptLine(line_id="b", speaker="dusk", text='"I do not know how we will find our way back."'),
            ],
        )
        assert resolve_refuted_by_unique_candidate([chapter], self._registry(dusk="male")) == []
