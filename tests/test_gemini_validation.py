from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from brain.orchestrator.pipeline import Pipeline
from brain.validators.gemini_validation import GeminiValidationService
from shared.constants import ValidationStatus
from shared.models import QualityResult, ScriptChapter, ScriptLine


class _FakeApi:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class _FakeWeb:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def generate_json(self, project_dir, purpose, prompt):
        self.calls.append((project_dir, purpose, prompt))
        return self.responses.pop(0)


def _service(root: Path) -> GeminiValidationService:
    return GeminiValidationService(
        {
            "enabled": True,
            "auto_accept_confidence": 0.9,
            "manual_review_confidence": 0.75,
            "api": {
                "enabled": True,
                "triage_model": "lite",
                "adjudication_model": "flash",
            },
            "browser": {"enabled": True},
        },
        root,
    )


class GeminiAttributionValidationTests(unittest.TestCase):
    def test_high_confidence_api_decision_resolves_and_records_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = _service(root)
            service.api = _FakeApi([{
                "decisions": [{
                    "item_id": "ch01_0001",
                    "decision": "resolved",
                    "speaker_id": "alice",
                    "confidence": 0.97,
                    "reason": "The attached speech tag names Alice.",
                    "evidence": "Alice said",
                }]
            }])
            service.web = _FakeWeb()
            line = ScriptLine(
                line_id="ch01_0001",
                speaker="narrator",
                speaker_confidence=0.3,
                attribution_review_required=True,
                attribution_review_reason="Ambiguous local turn",
                text='"Wait," Alice said.',
            )
            chapter = ScriptChapter(chapter_number=1, chapter_title="One", lines=[line])

            summary = service.resolve_attributions(
                project_dir=root,
                chapters=[chapter],
                character_ids={"narrator", "alice"},
            )

            self.assertEqual(summary, {"attempted": 1, "resolved": 1, "manual_review": 0})
            self.assertEqual(line.speaker, "alice")
            self.assertEqual(line.attribution_resolver, "gemini_api_triage")
            self.assertAlmostEqual(line.speaker_confidence, 0.97)
            self.assertFalse(line.attribution_review_required)
            self.assertEqual(line.attribution_confidence_history[-1]["confidence"], 0.97)
            self.assertTrue((root / "external_validation" / "attribution.json").is_file())

    def test_inconclusive_api_stages_use_persistent_web_purpose_then_stay_manual(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            low = lambda confidence: {"decisions": [{
                "item_id": "ch01_0001",
                "decision": "abstain",
                "speaker_id": None,
                "confidence": confidence,
                "reason": "Insufficient source evidence.",
            }]}
            service = _service(root)
            service.api = _FakeApi([low(0.55), low(0.7)])
            service.web = _FakeWeb([low(0.72)])
            line = ScriptLine(
                line_id="ch01_0001",
                speaker="narrator",
                attribution_review_required=True,
                text='"Wait."',
            )

            summary = service.resolve_attributions(
                project_dir=root,
                chapters=[ScriptChapter(chapter_number=1, chapter_title="One", lines=[line])],
                character_ids={"narrator", "alice"},
            )

            self.assertEqual(summary["manual_review"], 1)
            self.assertTrue(line.attribution_review_required)
            self.assertEqual(len(line.attribution_confidence_history), 4)
            self.assertEqual(line.attribution_confidence_history[0]["resolver"], "local")
            self.assertEqual(service.web.calls[0][1], "attribution")


class GeminiAudioValidationTests(unittest.TestCase):
    def test_hard_gate_cannot_be_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "line.wav"
            audio.write_bytes(b"RIFF")
            service = _service(root)
            service.api = _FakeApi([])
            service.web = _FakeWeb()
            result = QualityResult(
                line_id="line",
                status=ValidationStatus.FAIL,
                wer=0.5,
                quality_score=0.2,
                passed_hard_gates=False,
            )

            service.validate_audio(project_dir=root, audio_path=audio, line_text="Text", result=result)

            self.assertTrue(result.manual_review_required)
            self.assertIn("hard gate", result.manual_review_reason)
            self.assertEqual(result.validation_confidence, 1.0)
            self.assertEqual(result.external_validation_history[0]["decision"], "reject")
            self.assertEqual(service.api.calls, [])

    def test_high_confidence_audio_acceptance_clears_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "line.wav"
            audio.write_bytes(b"RIFF")
            service = _service(root)
            service.api = _FakeApi([{
                "item_id": "line",
                "decision": "accept",
                "confidence": 0.96,
                "reason": "Speech is clear and natural.",
                "defects": [],
            }])
            service.web = _FakeWeb()
            result = QualityResult(
                line_id="line",
                status=ValidationStatus.ACCEPTED_WITH_WARNING,
                wer=0.0,
                quality_score=0.72,
                warnings=["monotone"],
            )

            service.validate_audio(project_dir=root, audio_path=audio, line_text="Text", result=result)

            self.assertFalse(result.manual_review_required)
            self.assertEqual(result.external_validation_decision, "accept")
            self.assertAlmostEqual(result.validation_confidence, 0.96)
            self.assertEqual(len(result.external_validation_history), 2)

    def test_pipeline_routes_confident_rejection_to_automatic_regeneration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "line.wav"
            audio.write_bytes(b"RIFF")
            result = QualityResult(
                line_id="line",
                status=ValidationStatus.ACCEPTED_WITH_WARNING,
                wer=0.0,
                quality_score=0.8,
                selected=True,
                warnings=["prosody"],
            )

            class _Validator:
                auto_accept = 0.9

                @staticmethod
                def validate_audio(**kwargs):
                    quality = kwargs["result"]
                    quality.external_validation_decision = "reject"
                    quality.external_validation_confidence = 0.98
                    quality.manual_review_required = True
                    quality.manual_review_reason = "Audible glitch"

            class _Queue:
                @staticmethod
                def get_review_items(*_args):
                    return []

            pipeline = Pipeline.__new__(Pipeline)
            pipeline.external_validator = _Validator()
            pipeline.job_queue = _Queue()
            manual, regenerate = pipeline._apply_external_audio_validation(
                project_id="book",
                project_dir=root,
                request_lines=[ScriptLine(line_id="line", speaker="narrator", text="Text")],
                response=SimpleNamespace(
                    segment_files_dir=str(root),
                    quality_results=[result],
                ),
            )

            self.assertEqual(regenerate, {"line"})
            self.assertEqual(manual, {"line"})


if __name__ == "__main__":
    unittest.main()
