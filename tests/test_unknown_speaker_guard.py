"""A repair may not assign a speaker the cast has never heard of.

`_apply_deterministic_attribution_repairs` takes `issue.exact_speaker` straight
from a parsed speech tag. The gender branch beside it confines itself to
`allowed_speakers`; this branch did not, so a proper noun the tag happened to
contain became the speaker whether or not anyone by that name existed.

Two lines of `isles-of-the-emberdark` chapter 8 shipped that way:

    ch08_0315  speaker='drominadian'  voice_id=None  conf=0.99
    ch08_0317  speaker='drominadian'  voice_id=None  conf=0.99

`drominadian` is in no cast entry, so those lines had no voice at all. The audit
catches it afterwards as `unknown_speaker`, but by then it is a release-gate
failure rather than a repair declined -- and `provision_generic_speakers` cannot
rescue it either, because that only mints ids from a fixed archetype list and
this was a name out of the prose.
"""

from __future__ import annotations

import pytest

from brain.director.script_generator import AttributionIssue, ScriptGenerator
from shared.constants import Gender
from shared.models import Character, CharacterRegistry


@pytest.fixture
def registry() -> CharacterRegistry:
    def char(cid: str, name: str) -> Character:
        return Character(
            id=cid,
            name=name,
            gender=Gender.MALE,
            age_range="adult",
            voice_description=f"{name} voice",
            aliases=[],
        )

    return CharacterRegistry(
        characters={
            "narrator": char("narrator", "Narrator"),
            "armored_alien": char("armored_alien", "Armored Alien"),
        }
    )


def _raw() -> dict:
    return {"lines": [{"id": 7, "speaker": "vathi", "dialogue_kind": "spoken"}]}


def _issue(speaker: str) -> AttributionIssue:
    return AttributionIssue(
        kind="named_tag",
        fragment_index=7,
        fragment_id=7,
        submitted_speaker="vathi",
        message=f"Attached speech tag identifies '{speaker}'",
        exact_speaker=speaker,
    )


def test_a_speaker_with_no_cast_entry_is_declined(registry) -> None:
    raw = _raw()
    repairs = ScriptGenerator._apply_deterministic_attribution_repairs(raw, [_issue("drominadian")], registry=registry)
    assert repairs == 0
    assert raw["lines"][0]["speaker"] == "vathi", "the line keeps the speaker it had"


def test_a_registered_speaker_is_still_applied(registry) -> None:
    """The guard must not cost the repairs it exists alongside."""
    raw = _raw()
    repairs = ScriptGenerator._apply_deterministic_attribution_repairs(
        raw, [_issue("armored_alien")], registry=registry
    )
    assert repairs == 1
    assert raw["lines"][0]["speaker"] == "armored_alien"
    assert raw["lines"][0]["attribution_review_required"] is False


def test_narrator_is_always_allowed(registry) -> None:
    raw = _raw()
    ScriptGenerator._apply_deterministic_attribution_repairs(raw, [_issue("narrator")], registry=registry)
    assert raw["lines"][0]["speaker"] == "narrator"


def test_allowed_speakers_narrows_further_than_the_registry(registry) -> None:
    """When the caller supplies a scope, that scope wins."""
    raw = _raw()
    repairs = ScriptGenerator._apply_deterministic_attribution_repairs(
        raw, [_issue("armored_alien")], registry=registry, allowed_speakers={"narrator"}
    )
    assert repairs == 0
    assert raw["lines"][0]["speaker"] == "vathi"


def test_without_a_registry_the_repair_is_unchanged(registry) -> None:
    """No cast to check against is not the same as a cast that lacks the name."""
    raw = _raw()
    repairs = ScriptGenerator._apply_deterministic_attribution_repairs(raw, [_issue("drominadian")])
    assert repairs == 1
    assert raw["lines"][0]["speaker"] == "drominadian"
