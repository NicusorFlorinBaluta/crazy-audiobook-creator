"""A second look, with more context, at only the lines the first look could not settle.

Measured on the 16 sub-threshold lines of the-finest-edge-of-twilight
(`scripts/experiment_attribution_context_ab.py`, three runs each):

        agrees_with_stored  stable_3of3  mean_conf  above_0.85  wall
narrow        16/16            16/16       0.917      13/16     5.3s/line
wide          15/16            16/16       0.954      16/16     6.1s/line

Wide context buys confidence, not stability -- both widths were unanimous on
every line -- and the one disagreement, `ch11_0222`, was wide catching a real
error the narrow window had cut off. So the retry runs as a cascade on the
2.6% of lines that land below the bar, never as a default width.

The property these tests defend is that the cascade is *strictly additive*: it
can turn an escalation into a local resolution and nothing else. A line the
narrow pass already resolved must never be re-asked, and a failed retry must
leave the escalation exactly as it was.
"""

from __future__ import annotations

import json

import pytest

from brain.director.attribution_detector import build_turn_window, rebuild_turn_with_context
from brain.validators.tiered_adjudicator import TieredAttributionAdjudicator
from shared.constants import Gender
from shared.models import Character, CharacterRegistry, ScriptChapter, ScriptLine

EVIDENCE = "the timbre of Dahlia"


@pytest.fixture
def registry() -> CharacterRegistry:
    def char(cid: str, name: str, gender: Gender) -> Character:
        return Character(
            id=cid,
            name=name,
            gender=gender,
            age_range="adult",
            voice_description=f"{name} voice",
            aliases=[],
        )

    return CharacterRegistry(
        characters={
            "narrator": char("narrator", "Narrator", Gender.OTHER),
            "dahlia": char("dahlia", "Dahlia", Gender.FEMALE),
            "effron": char("effron", "Effron", Gender.MALE),
        }
    )


@pytest.fixture
def chapter() -> ScriptChapter:
    """A chapter long enough that a +/-20 window genuinely sees more than +/-5."""
    lines: list[ScriptLine] = []
    for i in range(61):
        speaker = "dahlia" if i % 2 else "effron"
        lines.append(
            ScriptLine(
                line_id=f"ch11_{i:04d}",
                speaker=speaker,
                speaker_confidence=0.8,
                text=f'"Line {i}, and something about {EVIDENCE}."',
                dialogue_kind="spoken",
            )
        )
    return ScriptChapter(chapter_number=11, chapter_title="Eleven", scenes=[], lines=lines)


class _WidthSensitiveOllama:
    """Answers one way on a short prompt and another on a long one.

    A stand-in for the real effect: the narrow window cuts off the sentence
    that settles the line, so the model hedges; with the sentence in view it
    commits. Counts calls, because "was the retry even attempted?" is the thing
    most of these tests are actually asking.
    """

    model = "stub"

    def __init__(self, narrow: tuple[str, float], wide: tuple[str, float], *, threshold: int = 20) -> None:
        self.narrow = narrow
        self.wide = wide
        # Counted in context lines rather than characters: a radius of 5 puts
        # 11 lines in the prompt and a radius of 20 puts 41, which is the
        # distinction being tested. Prompt length also moves with unrelated
        # edits to the instructions.
        self.threshold = threshold
        self.prompts: list[str] = []

    def generate(self, prompt: str, **kwargs: object) -> str:  # noqa: ARG002 - stub
        self.prompts.append(prompt)
        context_lines = prompt.count("] ")
        speaker, confidence = self.wide if context_lines >= self.threshold else self.narrow
        return json.dumps(
            {
                "speaker_id": speaker,
                "confidence": confidence,
                "reason": f"stub answer at prompt length {len(prompt)}",
                "evidence_quote": EVIDENCE,
            }
        )


def _adjudicator(registry: CharacterRegistry, ollama: object, **kwargs: object) -> TieredAttributionAdjudicator:
    return TieredAttributionAdjudicator(
        ollama=ollama,
        external_validator=None,
        registry=registry,
        local_auto_accept=0.85,
        **kwargs,
    )


def _turn(chapter: ScriptChapter, idx: int = 30):
    return build_turn_window(chapter, idx, reason="test", pattern="test")


def test_a_line_the_narrow_window_settles_is_never_re_asked(registry, chapter) -> None:
    """97% of lines are confident already. Paying +15% on those buys nothing."""
    ollama = _WidthSensitiveOllama(narrow=("effron", 0.95), wide=("dahlia", 0.99))
    result = _adjudicator(registry, ollama)._adjudicate_turn_tier1(_turn(chapter), chapter)

    assert result.resolver_tier == "local_qwen"
    assert result.resolved_speaker == "effron"
    assert len(ollama.prompts) == 1, "a confident line must cost exactly one call"


def test_a_sub_threshold_line_is_retried_and_settled_locally(registry, chapter) -> None:
    ollama = _WidthSensitiveOllama(narrow=("effron", 0.80), wide=("dahlia", 0.98))
    result = _adjudicator(registry, ollama)._adjudicate_turn_tier1(_turn(chapter), chapter)

    assert result.resolver_tier == "local_qwen_wide"
    assert result.resolved_speaker == "dahlia"
    assert result.confidence == pytest.approx(0.98)
    assert len(ollama.prompts) == 2
    assert len(ollama.prompts[1]) > len(ollama.prompts[0]), "the retry must actually widen the window"


def test_the_retry_reason_keeps_why_the_narrow_window_gave_up(registry, chapter) -> None:
    """Otherwise the record shows a confident answer and no trace of the doubt."""
    ollama = _WidthSensitiveOllama(narrow=("effron", 0.80), wide=("dahlia", 0.98))
    result = _adjudicator(registry, ollama)._adjudicate_turn_tier1(_turn(chapter), chapter)

    assert "0.80" in result.reason
    assert "context" in result.reason


def test_a_retry_that_also_fails_leaves_the_escalation_untouched(registry, chapter) -> None:
    """The cascade may only ever *add* a resolution -- never worsen one."""
    ollama = _WidthSensitiveOllama(narrow=("effron", 0.80), wide=("dahlia", 0.60))
    result = _adjudicator(registry, ollama)._adjudicate_turn_tier1(_turn(chapter), chapter)

    assert result.resolver_tier == "gemini_api"
    assert result.resolved_speaker == "effron", "the narrow answer is what escalates, not the retry's"
    assert result.confidence == pytest.approx(0.80)
    assert len(ollama.prompts) == 2


def test_a_retry_that_names_nobody_real_leaves_the_escalation_untouched(registry, chapter) -> None:
    """A wider window is no reason to relax guardrail 3."""
    ollama = _WidthSensitiveOllama(narrow=("effron", 0.80), wide=("Some Bystander", 0.99))
    result = _adjudicator(registry, ollama)._adjudicate_turn_tier1(_turn(chapter), chapter)

    assert result.resolver_tier == "gemini_api"
    assert result.resolved_speaker == "effron"


def test_the_retry_runs_at_most_once(registry, chapter) -> None:
    """The recursion guard: a retry that escalates must not retry itself."""
    ollama = _WidthSensitiveOllama(narrow=("effron", 0.80), wide=("dahlia", 0.10))
    _adjudicator(registry, ollama)._adjudicate_turn_tier1(_turn(chapter), chapter)

    assert len(ollama.prompts) == 2


def test_a_model_failure_on_the_retry_is_not_fatal(registry, chapter) -> None:
    class _ExplodesOnTheSecondCall(_WidthSensitiveOllama):
        def generate(self, prompt: str, **kwargs: object) -> str:
            if self.prompts:
                self.prompts.append(prompt)
                raise RuntimeError("GPU fell over")
            return super().generate(prompt, **kwargs)

    ollama = _ExplodesOnTheSecondCall(narrow=("effron", 0.80), wide=("dahlia", 0.98))
    result = _adjudicator(registry, ollama)._adjudicate_turn_tier1(_turn(chapter), chapter)

    assert result.resolver_tier == "gemini_api"
    assert result.confidence == pytest.approx(0.80)


def test_the_cascade_can_be_switched_off(registry, chapter) -> None:
    ollama = _WidthSensitiveOllama(narrow=("effron", 0.80), wide=("dahlia", 0.98))
    result = _adjudicator(registry, ollama, wide_context_retry=False)._adjudicate_turn_tier1(
        _turn(chapter), chapter
    )

    assert result.resolver_tier == "gemini_api"
    assert len(ollama.prompts) == 1


def test_no_chapter_means_no_retry(registry, chapter) -> None:
    """`adjudicate` passes `chapter_map.get(...)`, which can miss."""
    ollama = _WidthSensitiveOllama(narrow=("effron", 0.80), wide=("dahlia", 0.98))
    result = _adjudicator(registry, ollama)._adjudicate_turn_tier1(_turn(chapter), None)

    assert result.resolver_tier == "gemini_api"
    assert len(ollama.prompts) == 1


def test_a_chapter_too_short_to_widen_is_not_re_asked(registry) -> None:
    """If the window already holds the whole chapter, the retry is a duplicate call."""
    lines = [
        ScriptLine(
            line_id=f"ch01_{i:04d}",
            speaker="effron" if i % 2 else "dahlia",
            speaker_confidence=0.8,
            text=f'"Short line {i}."',
            dialogue_kind="spoken",
        )
        for i in range(3)
    ]
    short = ScriptChapter(chapter_number=1, chapter_title="One", scenes=[], lines=lines)
    ollama = _WidthSensitiveOllama(narrow=("effron", 0.80), wide=("dahlia", 0.98))
    result = _adjudicator(registry, ollama)._adjudicate_turn_tier1(
        build_turn_window(short, 1, reason="test", pattern="test"), short
    )

    assert result.resolver_tier == "gemini_api"
    assert len(ollama.prompts) == 1


def test_rebuilding_a_turn_preserves_everything_but_the_window(chapter) -> None:
    """Why a line was flagged does not depend on how much of the scene is shown."""
    narrow = build_turn_window(chapter, 30, reason="low_confidence", pattern="low_conf")
    wide = rebuild_turn_with_context(narrow, chapter, window_radius=20, scene_radius=30)

    assert wide is not None
    assert (wide.line_id, wide.text, wide.current_speaker) == (narrow.line_id, narrow.text, narrow.current_speaker)
    assert (wide.detection_reason, wide.detection_pattern) == ("low_confidence", "low_conf")
    assert len(wide.surrounding_lines) > len(narrow.surrounding_lines)
    assert len(wide.scene_text) > len(narrow.scene_text)
    assert sum(1 for line in wide.surrounding_lines if line["is_target"]) == 1


def test_rebuilding_a_turn_whose_line_is_gone_returns_none(chapter) -> None:
    narrow = build_turn_window(chapter, 30, reason="test", pattern="test")
    narrow.line_id = "ch11_9999"

    assert rebuild_turn_with_context(narrow, chapter, window_radius=20, scene_radius=30) is None
