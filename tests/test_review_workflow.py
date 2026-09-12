from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import brain.dashboard.api.main as dashboard_main
from brain.dashboard.api.main import (
    _automatic_extraction_review_pending,
    _automatic_pipeline_review_pending,
)
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
        (self.project / "script" / "chapter_001.json").write_text(
            json.dumps(
                {
                    "chapter_number": 1,
                    "lines": [
                        {
                            "line_id": "line-1",
                            "speaker": "narrator",
                            "speaker_confidence": 0.4,
                            "attribution_review_required": True,
                            "attribution_review_reason": "ambiguous",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.queue.update_job("demo", {"generated_chapters": [1]})
        self.queue.log_quality(
            "demo",
            "audio-1",
            1,
            1,
            0.05,
            0.9,
            "flagged",
            details={"selected": True, "manual_review_reason": "uncertain"},
        )
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

    def test_stale_audio_reviews_do_not_block_a_reconciled_generation(self) -> None:
        self.queue.update_job("demo", {"generated_chapters": [], "mastered_chapters": []})
        self.queue.log_quality(
            "demo",
            "old-line",
            1,
            1,
            0.2,
            0.5,
            "flagged",
            details={"selected": True, "manual_review_reason": "old result"},
        )
        self.queue.set_review_item("demo", "segment", "old-line", "unreviewed", "old result")

        gate = collect_review_gate("demo", self.project, self.queue)

        self.assertFalse(gate.blocking_items)
        self.assertFalse(any(item.item_id == "old-line" for item in gate.items))

    def test_active_incomplete_chapter_audio_review_remains_blocking(self) -> None:
        self.queue.update_job(
            "demo",
            {
                "status": "waiting_for_review",
                "active_stage": "waiting_for_review",
                "generated_chapters": [],
                "mastered_chapters": [],
                "review_blocking_item_ids": ["current-line"],
            },
        )
        self.queue.log_quality(
            "demo",
            "current-line",
            2,
            1,
            0.2,
            0.5,
            "flagged",
            details={"selected": True, "manual_review_reason": "listen"},
        )
        self.queue.set_review_item("demo", "segment", "current-line", "unreviewed", "listen")

        gate = collect_review_gate("demo", self.project, self.queue)

        self.assertEqual([item.item_id for item in gate.blocking_items], ["current-line"])

    def test_ambiguous_extraction_blocks_until_include_exclude_or_reference(self) -> None:
        (self.project / "extraction_audit.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "sections": [
                        {
                            "item_id": "appendix-1",
                            "title": "Appendix: The Trial",
                            "href": "appendix.xhtml",
                            "decision": "exclude",
                            "confidence": 0.64,
                            "word_count": 1800,
                            "reason": "Narrative appendix is ambiguous",
                            "review_required": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        gate = collect_review_gate("demo", self.project, self.queue)
        self.assertEqual([item.category for item in gate.blocking_items], ["extraction"])
        self.assertNotIn("excerpt", gate.items[0].details)
        self.queue.set_review_item("demo", "extraction", "appendix-1", "include")
        resolved = collect_review_gate("demo", self.project, self.queue)
        self.assertFalse(resolved.blocking_items)
        self.assertEqual(resolved.items[0].disposition, "include")

    def test_never_attempted_extraction_can_enter_automatic_resolver_once(self) -> None:
        audit_path = self.project / "extraction_audit.json"
        payload = {
            "sections": [
                {
                    "item_id": "unknown-1",
                    "title": "Unknown",
                    "decision": "include",
                    "confidence": 0.6,
                    "review_required": True,
                }
            ]
        }
        audit_path.write_text(json.dumps(payload), encoding="utf-8")
        gate = collect_review_gate("demo", self.project, self.queue)
        original = dashboard_main._project_dir
        dashboard_main._project_dir = lambda _project_id: self.project
        try:
            self.assertTrue(_automatic_extraction_review_pending("demo", gate.blocking_items))
            payload["sections"][0]["external_validation_attempted"] = True
            audit_path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertFalse(_automatic_extraction_review_pending("demo", gate.blocking_items))
        finally:
            dashboard_main._project_dir = original

    def test_incomplete_scripting_can_resume_into_automatic_attribution_validation(self) -> None:
        (self.project / "script" / "chapter_001.json").write_text(
            json.dumps(
                {
                    "chapter_number": 1,
                    "lines": [
                        {
                            "line_id": "line-1",
                            "speaker": "narrator",
                            "speaker_confidence": 0.25,
                            "attribution_review_required": True,
                            "attribution_review_reason": "ambiguous",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        gate = collect_review_gate("demo", self.project, self.queue)

        self.assertTrue(
            _automatic_pipeline_review_pending(
                "demo",
                gate.blocking_items,
                {"script_completed": False},
                PipelineStage.SCRIPTING,
            )
        )
        self.assertFalse(
            _automatic_pipeline_review_pending(
                "demo",
                gate.blocking_items,
                {"script_completed": True},
                PipelineStage.BOOTSTRAPPING,
            )
        )

    def test_ledger_calibration_is_advisory(self) -> None:
        for index in range(25):
            self.queue.log_external_validation(
                "demo",
                "segment",
                f"line-{index}",
                "gemini_api_triage",
                "test",
                "accept",
                0.95,
                "clear",
                10,
            )
            self.queue.reconcile_external_validation("demo", "segment", f"line-{index}", "acceptable")
        calibration = self.queue.external_validation_calibration("demo")
        self.assertTrue(calibration["ready"])
        self.assertEqual(calibration["recommended_auto_accept_threshold"], 0.95)
        self.assertFalse(calibration["applied_automatically"])
        self.assertEqual(calibration["pooling_policy"], "provider_model_purpose_revision")
        self.assertEqual(calibration["pooled_groups"][0]["purpose"], "segment")
        self.assertAlmostEqual(calibration["brier_score"], 0.0025)

    def test_calibration_does_not_pool_different_models_or_purposes(self) -> None:
        self.queue.create_job("other", {"status": "waiting_for_review"})
        for project, item_type, provider, model, revision in (
            ("demo", "segment", "gemini", "flash", "audio-v2"),
            ("other", "segment", "gemini", "flash", "audio-v2"),
            ("other", "attribution", "gemini", "flash", "speaker-v3"),
            ("other", "segment", "gemini", "pro", "audio-v2"),
        ):
            self.queue.log_external_validation(
                project,
                item_type,
                f"{project}-{item_type}-{model}",
                provider,
                model,
                "accept",
                0.9,
                "clear",
                10,
                {"purpose_version": revision},
            )
            self.queue.reconcile_external_validation(
                project,
                item_type,
                f"{project}-{item_type}-{model}",
                "acceptable",
            )

        calibration = self.queue.external_validation_calibration("demo")
        groups = calibration["pooled_groups"]
        self.assertEqual(len(groups), 3)
        audio_flash = next(group for group in groups if group["model"] == "flash" and group["purpose"] == "segment")
        self.assertEqual(audio_flash["sample_count"], 2)

    def test_candidate_ranking_and_retention(self) -> None:
        audio = self.root / "source.wav"
        audio.write_bytes(b"RIFF-test")
        weak = QualityResult(
            line_id="line-1",
            status=ValidationStatus.FLAGGED,
            wer=0.2,
            quality_score=0.3,
            attempt=1,
            selected=True,
            external_validation_decision="reject",
            external_validation_confidence=0.9,
        )
        strong = QualityResult(
            line_id="line-1",
            status=ValidationStatus.PASS,
            wer=0.01,
            quality_score=0.95,
            attempt=2,
            selected=True,
            external_validation_decision="accept",
            external_validation_confidence=0.95,
        )
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
