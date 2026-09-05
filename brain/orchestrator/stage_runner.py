"""Typed resume planning for the durable pipeline coordinator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from shared.constants import PipelineStage


@dataclass(frozen=True)
class PipelineResumePlan:
    stage: PipelineStage
    reason: str

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> PipelineResumePlan:
        reset_target = state.get("reset_target_stage")
        if reset_target:
            return cls(PipelineStage(reset_target), "explicit stage reset")
        if state.get("bootstrapping_completed", False):
            return cls(PipelineStage.GENERATING, "voice bootstrap is complete")
        if state.get("script_completed", False):
            return cls(PipelineStage.BOOTSTRAPPING, "script is complete")
        if state.get("scripted_chapters"):
            return cls(PipelineStage.SCRIPTING, "partial script checkpoint exists")
        return cls(PipelineStage.CREATED, "no completed stage checkpoint")
