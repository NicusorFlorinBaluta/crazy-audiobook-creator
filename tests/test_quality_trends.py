from brain.orchestrator.quality_trends import build_long_form_quality_report
from shared.models import ScriptChapter, ScriptLine


def _script(chapter: int) -> ScriptChapter:
    return ScriptChapter(
        chapter_number=chapter,
        chapter_title=str(chapter),
        lines=[
            ScriptLine(
                line_id=f"ch{chapter:02d}_{index:04d}",
                speaker="mara",
                voice_id="mara",
                text="A line.",
            )
            for index in range(5)
        ],
    )


def _logs(chapter: int, similarity: float, pitch: float, monotone: bool = False):
    return [
        {
            "line_id": f"ch{chapter:02d}_{index:04d}",
            "chapter_number": chapter,
            "attempt": 1,
            "details": {
                "selected": True,
                "speaker_similarity": similarity,
                "pitch_median": pitch,
                "duration_seconds": 2.0,
                "monotone_warning": monotone,
            },
        }
        for index in range(5)
    ]


def test_long_form_report_flags_correlated_identity_drift_and_prosody():
    report = build_long_form_quality_report(
        _logs(1, 0.86, 140.0) + _logs(2, 0.58, 185.0, monotone=True),
        [_script(1), _script(2)],
    )
    kinds = {warning["kind"] for warning in report["warnings"]}
    assert "cross_chapter_voice_drift" in kinds
    assert "sustained_monotone_delivery" in kinds


def test_pitch_expression_alone_does_not_claim_identity_drift():
    report = build_long_form_quality_report(
        _logs(1, 0.86, 140.0) + _logs(2, 0.86, 190.0),
        [_script(1), _script(2)],
    )
    assert not any(
        warning["kind"] == "cross_chapter_voice_drift"
        for warning in report["warnings"]
    )
