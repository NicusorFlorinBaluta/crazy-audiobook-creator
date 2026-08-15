from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from brain.orchestrator.audio_candidates import candidate_score, list_candidates, preserve_candidate
from brain.orchestrator.job_queue import JobQueue
from brain.orchestrator.review_gate import collect_review_gate, write_release_report
from brain.orchestrator.stage_runner import PipelineResumePlan
from shared.constants import PipelineStage
from shared.models import QualityResult, ValidationStatus


class ReviewWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "demo"
        (self.project / "script").mkdir(parents=True)
        self.queue = JobQueue(str(self.root / "state.db"))
        self.queue.create_job("demo", {"status": "waiting_for_review", "bootstrapping_completed": True})

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_gate_release_and_human_resolution(self) -> None:
        (self.project / "script" / "chapter_001.json").write_text(json.dumps({
            "chapter_number": 1,
            "lines": [{"line_id": "line-1", "speaker": "narrator",
                       "speaker_confidence": .4, "attribution_review_required": True,
                       "attribution_review_reason": "ambiguous"}],
        }), encoding="utf-8")
        self.queue.set_review_item("demo", "segment", "audio-1", "unreviewed", "uncertain")
        gate = collect_review_gate("demo", self.project, self.queue)
        self.assertEqual(len(gate.blocking_items), 2)
        self.queue.set_review_item("demo", "segment", "audio-1", "acceptable")
        payload = json.loads((self.project / "script" / "chapter_001.json").read_text(encoding="utf-8"))
        payload["lines"][0]["attribution_review_required"] = False
        (self.project / "script" / "chapter_001.json").write_text(json.dumps(payload), encoding="utf-8")
        report = write_release_report("demo", self.project, self.queue)
        self.assertTrue(report["release_ready"])
        self.assertTrue((self.project / "pre_master_release.json").is_file())

    def test_ledger_calibration_is_advisory(self) -> None:
        for index in range(25):
            self.queue.log_external_validation(
                "demo", "segment", f"line-{index}", "gemini_api_triage", "test",
                "accept", .95, "clear", 10,
            )
            self.queue.reconcile_external_validation(
                "demo", "segment", f"line-{index}", "acceptable"
            )
        calibration = self.queue.external_validation_calibration("demo")
        self.assertTrue(calibration["ready"])
        self.assertEqual(calibration["recommended_auto_accept_threshold"], .95)
        self.assertFalse(calibration["applied_automatically"])

    def test_candidate_ranking_and_retention(self) -> None:
        audio = self.root / "source.wav"
        audio.write_bytes(b"RIFF-test")
        weak = QualityResult(line_id="line-1", status=ValidationStatus.FLAGGED,
                             wer=.2, quality_score=.3, attempt=1, selected=True,
                             external_validation_decision="reject",
                             external_validation_confidence=.9)
        strong = QualityResult(line_id="line-1", status=ValidationStatus.PASS,
                               wer=.01, quality_score=.95, attempt=2, selected=True,
                               external_validation_decision="accept",
                               external_validation_confidence=.95)
        self.assertGreater(candidate_score(strong), candidate_score(weak))
        preserve_candidate(self.project, audio, weak, retain=2)
        preserve_candidate(self.project, audio, strong, retain=2)
        rows = list_candidates(self.project, "line-1")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["quality"]["attempt"], 2)

    def test_typed_resume_plan(self) -> None:
        plan = PipelineResumePlan.from_state({"bootstrapping_completed": True})
        self.assertEqual(plan.stage, PipelineStage.GENERATING)


if __name__ == "__main__":
    unittest.main()
