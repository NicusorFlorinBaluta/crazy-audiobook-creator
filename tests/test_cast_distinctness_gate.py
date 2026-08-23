from brain.dashboard.api.main import (
    _cast_distinctness_review,
    _mark_cast_distinctness_stale,
)


def test_required_similar_pair_needs_review_but_unused_candidate_does_not():
    cast = {
        "quality": {
            "distinctness_status": "current",
            "cast_pair_diagnostics": [
                {
                    "left_voice_id": "narrator",
                    "right_voice_id": "mara",
                    "status": "similar",
                    "warning_suppressed": False,
                },
                {
                    "left_voice_id": "mara",
                    "right_voice_id": "unused_candidate",
                    "status": "similar",
                    "warning_suppressed": False,
                },
            ],
        }
    }
    pairs, stale = _cast_distinctness_review(cast, {"narrator", "mara"})
    assert len(pairs) == 1
    assert not stale


def test_voice_change_removes_stale_pair_evidence_and_requires_review():
    cast = {
        "quality": {
            "distinctness_status": "current",
            "cast_pair_diagnostics": [
                {
                    "left_voice_id": "narrator",
                    "right_voice_id": "mara",
                    "status": "distinct",
                }
            ],
        }
    }
    _mark_cast_distinctness_stale(cast, "mara")
    pairs, stale = _cast_distinctness_review(cast, {"narrator", "mara"})
    assert pairs == []
    assert stale
    assert cast["quality"]["cast_pair_diagnostics"] == []
