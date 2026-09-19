"""A published book is not re-scripted without someone saying so.

`_run_exclusive` re-scripts a whole book when its chapter fingerprints stop
matching their dependencies. That is right while a project is in progress -- a
changed dependency usually does mean the cached script is wrong.

It is wrong once audio has shipped. On 2026-09-11 it fired on
`the-finest-edge-of-twilight` with 8 chapters generated, 5 mastered and Part 01
published. Nothing in the scripts had changed: the repair scripts had rewritten
`characters.json` (an alias prune), and the chapter fingerprint covers every
dependency character's alias list, so all 32 chapters read as stale. It reset
the voice-review approval and had started dropping chapters out of
`generated_chapters` before it was stopped by hand.

The cheap recovery -- `scripts/refresh_script_fingerprints.py`, when a repair
has already brought the scripts forward -- is only reachable if the run stops
first. So it stops, and says which two ways out there are.
"""

from __future__ import annotations

import json

import pytest

from brain.orchestrator.pipeline import Pipeline


@pytest.fixture
def project(tmp_path):
    d = tmp_path / "a-book"
    (d / "deliveries").mkdir(parents=True)
    return d


def _index(project, *statuses):
    (project / "deliveries" / "index.json").write_text(
        json.dumps({"deliveries": [{"id": f"part-{i:03d}", "status": s} for i, s in enumerate(statuses, 1)]}),
        encoding="utf-8",
    )


class TestPublishedDeliveryCount:
    def test_counts_only_published_parts(self, project) -> None:
        _index(project, "published", "published", "draft")
        assert Pipeline._published_delivery_count(None, project, {}) == 2

    def test_a_project_with_nothing_published_is_zero(self, project) -> None:
        _index(project, "draft")
        assert Pipeline._published_delivery_count(None, project, {}) == 0

    def test_no_index_falls_back_to_the_job_state(self, project) -> None:
        assert Pipeline._published_delivery_count(None, project, {"published_delivery_count": 3}) == 3

    def test_an_unreadable_index_falls_back_rather_than_raising(self, project) -> None:
        (project / "deliveries" / "index.json").write_text("{not json", encoding="utf-8")
        assert Pipeline._published_delivery_count(None, project, {"published_delivery_count": 1}) == 1

    def test_a_nonsense_state_value_is_not_fatal(self, project) -> None:
        assert Pipeline._published_delivery_count(None, project, {"published_delivery_count": "many"}) == 0

    def test_missing_everything_is_zero(self, project) -> None:
        assert Pipeline._published_delivery_count(None, project, {}) == 0


class TestTheGuardWording:
    """The message has to name both ways out, or it is just a wall."""

    def test_it_names_the_re_stamp_and_the_override(self) -> None:
        import inspect

        source = inspect.getsource(Pipeline._run_exclusive)
        assert "refresh_script_fingerprints.py" in source
        assert "allow_script_refresh_after_publish" in source

    def test_the_override_flag_is_read_from_state(self) -> None:
        import inspect

        source = inspect.getsource(Pipeline._run_exclusive)
        assert 'state.get("allow_script_refresh_after_publish", False)' in source
