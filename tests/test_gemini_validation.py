from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from brain.orchestrator.pipeline import Pipeline
from brain.validators.gemini_validation import (
    ExtractionBatch,
    GeminiApiClient,
    GeminiValidationService,
    _extract_json,
    _gemini_response_schema,
)
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
            "character_augmentation": {"enabled": True},
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
    def test_web_json_parser_accepts_trailing_explanation(self) -> None:
        self.assertEqual(
            _extract_json('{"decisions": []}\nDone.'),
            {"decisions": []},
        )

    def test_gemini_schema_inlines_pydantic_definitions(self) -> None:
        schema = _gemini_response_schema(ExtractionBatch.model_json_schema())
        encoded = str(schema)
        self.assertNotIn("$defs", encoded)
        self.assertNotIn("$ref", encoded)
        self.assertEqual(
            schema["properties"]["decisions"]["items"]["type"],
            "object",
        )

    def test_extraction_uses_high_confidence_api_result_and_bounded_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = _service(root)
            service.api = _FakeApi(
                [
                    {
                        "decisions": [
                            {
                                "item_id": "appendix-1",
                                "decision": "include",
                                "confidence": 0.96,
                                "reason": "The appendix is continuous narrative.",
                            }
                        ]
                    }
                ]
            )
            service.web = _FakeWeb()
            result = service.resolve_extraction_sections(
                project_dir=root,
                sections=[
                    {
                        "item_id": "appendix-1",
                        "href": "appendix.xhtml",
                        "title": "Appendix: The Trial",
                        "word_count": 2400,
                        "semantics": ["appendix"],
                        "decision": "exclude",
                        "confidence": 0.7,
                        "classifier_excerpt": "x" * 900,
                    }
                ],
            )
            self.assertEqual(result["decisions"]["appendix-1"]["decision"], "include")
            prompt = service.api.calls[0]["prompt"]
            self.assertLess(prompt.count("x"), 500)
            self.assertEqual(service.web.calls, [])

    def test_extraction_browser_fallback_reuses_extraction_conversation_purpose(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = _service(root)
            abstain = {
                "decisions": [
                    {
                        "item_id": "section-1",
                        "decision": "abstain",
                        "confidence": 0.7,
                        "reason": "Unclear.",
                    }
                ]
            }
            service.api = _FakeApi([abstain, abstain])
            service.web = _FakeWeb(
                [
                    {
                        "decisions": [
                            {
                                "item_id": "section-1",
                                "decision": "reference",
                                "confidence": 0.95,
                                "reason": "Glossary-like reference material.",
                            }
                        ]
                    }
                ]
            )
            result = service.resolve_extraction_sections(
                project_dir=root,
                sections=[{"item_id": "section-1", "title": "Names", "word_count": 800}],
            )
            self.assertEqual(result["decisions"]["section-1"]["decision"], "reference")
            self.assertEqual(service.web.calls[0][1], "extraction_v1")

    def test_high_confidence_api_decision_resolves_and_records_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = _service(root)
            service.api = _FakeApi(
                [
                    {
                        "decisions": [
                            {
                                "item_id": "ch01_0001",
                                "decision": "resolved",
                                "speaker_id": "alice",
                                "confidence": 0.97,
                                "reason": "The attached speech tag names Alice.",
                                "evidence": "Alice said",
                            }
                        ]
                    }
                ]
            )
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

    def test_named_identity_cannot_be_auto_mapped_to_generic_speaker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = {
                "decisions": [
                    {
                        "item_id": "ch01_0001",
                        "decision": "resolved",
                        "speaker_id": "minor_female",
                        "confidence": 1.0,
                        "reason": "The line is attributed to Tuka.",
                        "evidence": '"Wait," Tuka said.',
                    }
                ]
            }
            service = _service(root)
            service.api = _FakeApi([invalid, invalid])
            service.web = _FakeWeb([invalid])
            line = ScriptLine(
                line_id="ch01_0001",
                speaker="minor_female",
                speaker_confidence=0.3,
                attribution_review_required=True,
                attribution_review_reason="Missing named candidate",
                text='"Wait," Tuka said.',
            )

            summary = service.resolve_attributions(
                project_dir=root,
                chapters=[
                    ScriptChapter(
                        chapter_number=1,
                        chapter_title="One",
                        lines=[line],
                    )
                ],
                character_ids={"narrator", "minor_female"},
                character_context={
                    "narrator": {"id": "narrator", "name": "Narrator"},
                    "minor_female": {
                        "id": "minor_female",
                        "name": "Unnamed Woman",
                    },
                },
            )

            self.assertEqual(summary["resolved"], 0)
            self.assertEqual(summary["manual_review"], 1)
            self.assertTrue(line.attribution_review_required)
            self.assertEqual(line.speaker, "minor_female")
            self.assertIn(
                "different or missing character",
                line.attribution_review_reason,
            )
            self.assertTrue(all("validation_error" in entry for entry in line.attribution_confidence_history[1:]))

    def test_inconclusive_api_stages_use_persistent_web_purpose_then_stay_manual(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            low = lambda confidence: {
                "decisions": [
                    {
                        "item_id": "ch01_0001",
                        "decision": "abstain",
                        "speaker_id": None,
                        "confidence": confidence,
                        "reason": "Insufficient source evidence.",
                    }
                ]
            }
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
            self.assertEqual(service.web.calls[0][1], "attribution_v2")

    def test_attribution_batches_and_candidates_stay_chapter_local(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = _service(root)
            service.api = _FakeApi(
                [
                    {
                        "decisions": [
                            {
                                "item_id": "ch01_0001",
                                "decision": "resolved",
                                "speaker_id": "alice",
                                "confidence": 0.96,
                                "reason": "Alice is named.",
                                "evidence": "Alice said",
                            }
                        ]
                    },
                    {
                        "decisions": [
                            {
                                "item_id": "ch02_0001",
                                "decision": "resolved",
                                "speaker_id": "bob",
                                "confidence": 0.96,
                                "reason": "Bob is named.",
                                "evidence": "Bob said",
                            }
                        ]
                    },
                ]
            )
            service.web = _FakeWeb()
            chapters = [
                ScriptChapter(
                    chapter_number=1,
                    chapter_title="One",
                    lines=[
                        ScriptLine(
                            line_id="ch01_0001",
                            speaker="narrator",
                            attribution_review_required=True,
                            text='"Wait," Alice said.',
                        )
                    ],
                ),
                ScriptChapter(
                    chapter_number=2,
                    chapter_title="Two",
                    lines=[
                        ScriptLine(
                            line_id="ch02_0001",
                            speaker="narrator",
                            attribution_review_required=True,
                            text='"Go," Bob said.',
                        )
                    ],
                ),
            ]
            result = service.resolve_attributions(
                project_dir=root,
                chapters=chapters,
                character_ids={"narrator", "alice", "bob"},
                character_context={
                    "alice": {"id": "alice", "name": "Alice", "aliases": []},
                    "bob": {"id": "bob", "name": "Bob", "aliases": []},
                    "narrator": {"id": "narrator", "name": "Narrator", "aliases": []},
                },
            )
            self.assertEqual(result["resolved"], 2)
            self.assertEqual(len(service.api.calls), 2)
            self.assertIn('"alice"', service.api.calls[0]["prompt"])
            self.assertNotIn('"bob"', service.api.calls[0]["prompt"])
            self.assertIn('"bob"', service.api.calls[1]["prompt"])
            self.assertNotIn('"alice"', service.api.calls[1]["prompt"])

    def test_existing_chapter_speaker_remains_an_allowed_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = _service(root)
            service.api = _FakeApi(
                [
                    {
                        "decisions": [
                            {
                                "item_id": "ch01_0002",
                                "decision": "resolved",
                                "speaker_id": "alice",
                                "confidence": 0.97,
                                "reason": "The turn alternates back to Alice.",
                                "evidence": "Alice owns the preceding side of the exchange.",
                            }
                        ]
                    }
                ]
            )
            service.web = _FakeWeb()
            chapter = ScriptChapter(
                chapter_number=1,
                chapter_title="One",
                lines=[
                    ScriptLine(
                        line_id="ch01_0001",
                        speaker="alice",
                        text='"I understand."',
                    ),
                    ScriptLine(
                        line_id="ch01_0002",
                        speaker="minor_female",
                        speaker_confidence=0.4,
                        attribution_review_required=True,
                        text='"Then we agree."',
                    ),
                    ScriptLine(
                        line_id="ch01_0003",
                        speaker="narrator",
                        text="Bob left the room.",
                    ),
                ],
            )

            result = service.resolve_attributions(
                project_dir=root,
                chapters=[chapter],
                character_ids={"narrator", "alice", "bob", "minor_female"},
                character_context={
                    "alice": {"id": "alice", "name": "Alice", "aliases": []},
                    "bob": {"id": "bob", "name": "Bob", "aliases": []},
                    "narrator": {"id": "narrator", "name": "Narrator", "aliases": []},
                    "minor_female": {
                        "id": "minor_female",
                        "name": "Unnamed Woman",
                        "aliases": [],
                    },
                },
            )

            self.assertEqual(result["resolved"], 1)
            self.assertEqual(chapter.lines[1].speaker, "alice")
            self.assertIn('"alice"', service.api.calls[0]["prompt"])

    def test_character_augmentation_requires_verbatim_grounding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = _service(root)
            ungrounded = {
                "decisions": [
                    {
                        "character_id": "alice",
                        "decision": "update",
                        "confidence": 0.99,
                        "reason": "Inferred",
                        "evidence": ["This sentence is not in the source."],
                        "gender": "female",
                        "voice_description": "bright precise voice",
                    }
                ]
            }
            grounded = {
                "decisions": [
                    {
                        "character_id": "alice",
                        "decision": "update",
                        "confidence": 0.96,
                        "reason": "Grounded",
                        "evidence": ["Alice answered in a clear, deliberate voice."],
                        "gender": "female",
                        "voice_description": "bright precise voice",
                    }
                ]
            }
            service.api = _FakeApi([ungrounded, grounded])
            service.web = _FakeWeb()
            result = service.augment_characters(
                project_dir=root,
                dossier={
                    "alice": {
                        "current_gender": "other",
                        "evidence_snippets": ["Alice answered in a clear, deliberate voice."],
                    }
                },
            )
            self.assertIn("alice", result["accepted"])
            self.assertEqual(result["review"], [])
            self.assertTrue((root / "character_augmentation_audit.json").is_file())


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
            service.api = _FakeApi(
                [
                    {
                        "item_id": "line",
                        "decision": "accept",
                        "confidence": 0.96,
                        "reason": "Speech is clear and natural.",
                        "defects": [],
                    }
                ]
            )
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


class GeminiApiClientPacingTests(unittest.TestCase):
    def test_client_respects_request_interval(self) -> None:
        from unittest.mock import MagicMock, patch

        with tempfile.TemporaryDirectory() as directory:
            client = GeminiApiClient(
                {
                    "enabled": True,
                    "request_interval_seconds": 0.05,
                    "max_attempts": 2,
                },
                Path(directory),
            )
            client.api_key = "test_key"

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"candidates": [{"content": {"parts": [{"text": '{"decision": "accept"}'}]}}]}

            with patch("httpx.post", return_value=mock_resp) as mock_post, patch("time.sleep") as mock_sleep:
                client._last_request_time = 100.0
                with patch("time.monotonic", side_effect=[100.01, 100.05, 100.1, 100.1]):
                    res = client.generate_json(
                        model="gemini-3.5-flash-lite",
                        prompt="test prompt",
                        schema={"type": "object"},
                    )
                    self.assertEqual(res, {"decision": "accept"})
                    mock_sleep.assert_called_once()
                    self.assertAlmostEqual(mock_sleep.call_args[0][0], 0.04, places=2)

    def test_client_honors_retry_after_on_429(self) -> None:
        from unittest.mock import MagicMock, patch

        with tempfile.TemporaryDirectory() as directory:
            client = GeminiApiClient(
                {
                    "enabled": True,
                    "request_interval_seconds": 0.0,
                    "max_attempts": 3,
                },
                Path(directory),
            )
            client.api_key = "test_key"

            resp_429 = MagicMock()
            resp_429.status_code = 429
            resp_429.headers = {"Retry-After": "5"}

            resp_200 = MagicMock()
            resp_200.status_code = 200
            resp_200.json.return_value = {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]}

            with patch("httpx.post", side_effect=[resp_429, resp_200]), patch("time.sleep") as mock_sleep:
                res = client.generate_json(
                    model="gemini-3.5-flash-lite",
                    prompt="test prompt",
                    schema={"type": "object"},
                )
                self.assertEqual(res, {"ok": True})
                mock_sleep.assert_called_once_with(5.0)


if __name__ == "__main__":
    unittest.main()
