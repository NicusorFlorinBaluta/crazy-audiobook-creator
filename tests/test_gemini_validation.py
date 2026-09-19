from __future__ import annotations

import json
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
                def is_critical_risk_segment(_result):
                    return True

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


class GeminiResponseSchemaTests(unittest.TestCase):
    """What `generateContent` will and will not accept in a responseSchema.

    Measured against gemini-3.5-flash and -flash-lite on 2026-09-04: any
    schema carrying `maxItems` is rejected with a bare `400 INVALID_ARGUMENT`,
    as an integer or as a string, even though the key appears in the published
    subset. Found by bisecting a failing schema one key at a time after a cast
    adjudication call 400ed in a live run.

    The damage was not limited to the new cast passes. `CharacterAugmentationBatch`
    carries `maxItems` too and predates them, so its API tier had been failing
    and silently escalating for as long as it has existed.
    """

    def _keys(self, node, found=None):
        found = set() if found is None else found
        if isinstance(node, dict):
            for key, value in node.items():
                found.add(key)
                self._keys(value, found)
        elif isinstance(node, list):
            for value in node:
                self._keys(value, found)
        return found

    def test_no_shipped_schema_reaches_the_api_with_maxitems(self) -> None:
        from brain.validators import gemini_validation as module

        offenders = []
        for name in dir(module):
            model = getattr(module, name)
            if not isinstance(model, type) or not hasattr(model, "model_json_schema"):
                continue
            if not name.endswith(("Batch", "Decision")):
                continue
            converted = module._gemini_response_schema(model.model_json_schema())
            present = self._keys(converted) & {"maxItems", "minItems"}
            # A property literally named maxItems would be a false positive;
            # none exists, and this keeps the failure message honest.
            if present:
                offenders.append(f"{name}: {sorted(present)}")
        self.assertEqual(offenders, [], f"these would be rejected with 400: {offenders}")

    def test_the_converter_strips_the_bounds_but_keeps_the_shape(self) -> None:
        from brain.validators.gemini_validation import _gemini_response_schema

        converted = _gemini_response_schema(
            {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "maxItems": 60,
                        "minItems": 1,
                        "items": {"$ref": "#/$defs/Thing"},
                    }
                },
                "required": ["items"],
                "$defs": {
                    "Thing": {
                        "type": "object",
                        "title": "Thing",
                        "properties": {"name": {"type": "string", "maxLength": 600}},
                    }
                },
            }
        )
        self.assertNotIn("maxItems", converted["properties"]["items"])
        self.assertNotIn("minItems", converted["properties"]["items"])
        # The reference is still inlined and the rest survives: maxLength is
        # accepted by the API, and dropping constraints wholesale would be a
        # different and worse change.
        thing = converted["properties"]["items"]["items"]
        self.assertEqual(thing["properties"]["name"]["maxLength"], 600)
        self.assertNotIn("title", thing)
        self.assertEqual(converted["required"], ["items"])

    def test_the_bounds_are_still_enforced_where_it_counts(self) -> None:
        """Stripping the hint must not stop the model from being validated.

        The API never guaranteed the bound anyway. Pydantic does, on the way
        back in, which is the only place it was ever load-bearing.
        """
        from pydantic import ValidationError

        from brain.validators.gemini_validation import CastRosterBatch

        too_many = {"proposals": [{"left_id": "a", "right_id": "b", "confidence": 0.9, "reason": "x"}] * 61}
        with self.assertRaises(ValidationError):
            CastRosterBatch.model_validate(too_many)


class EscalationFailureReportingTests(unittest.TestCase):
    """A failure has to say enough to act on."""

    def test_the_summary_keeps_the_response_body(self) -> None:
        from brain.validators.gemini_validation import _failure_summary

        exc = Exception(
            "Client error '400 Bad Request' for url 'https://x'\n"
            "For more information check: https://mdn\n"
            '; response={ "error": { "status": "INVALID_ARGUMENT" } }'
        )
        summary = _failure_summary(exc)
        self.assertIn("INVALID_ARGUMENT", summary, "the body is the part worth keeping")
        self.assertNotIn("\n", summary, "must stay one log line")

    def test_a_rejected_request_is_not_reported_as_an_outage(self) -> None:
        from brain.validators.gemini_validation import _is_malformed_request

        self.assertTrue(_is_malformed_request("400 Bad Request INVALID_ARGUMENT"))
        self.assertTrue(_is_malformed_request("404 Not Found"))
        # A rate limit is exactly when escalating is correct, so it is not a bug.
        self.assertFalse(_is_malformed_request("429 Too Many Requests"))
        self.assertFalse(_is_malformed_request("503 Service Unavailable"))


class AudioValidationFastExitAndFallbackTests(unittest.TestCase):
    def test_usage_budget_is_exhausted(self) -> None:
        from brain.validators.gemini_validation import ExternalValidationError, _UsageBudget

        with tempfile.TemporaryDirectory() as directory:
            budget_path = Path(directory) / "usage.json"
            budget = _UsageBudget(budget_path, {"test-model": 2})

            self.assertFalse(budget.is_exhausted("test-model"))
            budget.reserve("test-model")
            self.assertFalse(budget.is_exhausted("test-model"))
            budget.reserve("test-model")
            self.assertTrue(budget.is_exhausted("test-model"))
            with self.assertRaises(ExternalValidationError):
                budget.reserve("test-model")

    def test_gemini_api_429_quota_fast_exit(self) -> None:
        from unittest.mock import MagicMock, patch

        from brain.validators.gemini_validation import GeminiApiClient, QuotaExhaustedError

        with tempfile.TemporaryDirectory() as directory:
            client = GeminiApiClient(
                {
                    "enabled": True,
                    "api_key_env": "TEST_GEMINI_KEY",
                    "max_attempts": 4,
                    "daily_request_budgets": {"test-model": 10},
                },
                Path(directory),
            )
            client.api_key = "test-key"

            # A per-DAY quota cannot recover inside this call: fail on attempt 1.
            per_day = MagicMock()
            per_day.status_code = 429
            per_day.text = (
                '{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": ['
                '{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{'
                '"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}'
            )
            with patch("httpx.post", return_value=per_day) as mock_post:
                with self.assertRaises(QuotaExhaustedError) as ctx:
                    client.generate_json(model="test-model", prompt="test", schema={})
                self.assertIn("quota exhausted", str(ctx.exception).lower())
                # Must exit on first attempt, NOT loop 4 times with backoff
                self.assertEqual(mock_post.call_count, 1)

    def test_gemini_api_429_per_minute_rate_limit_retries(self) -> None:
        """A per-MINUTE 429 clears on its own; it must not be read as exhaustion.

        Google returns RESOURCE_EXHAUSTED and the word "quota" for both kinds,
        so only the quotaId separates them. Treating a rate limit as exhaustion
        opened the provider circuit for an hour over something that clears in
        seconds, and every remaining critical segment was then accepted locally
        with no external check at all.
        """
        from unittest.mock import MagicMock, patch

        from brain.validators.gemini_validation import (
            ExternalValidationError,
            GeminiApiClient,
            QuotaExhaustedError,
        )

        with tempfile.TemporaryDirectory() as directory:
            client = GeminiApiClient(
                {
                    "enabled": True,
                    "api_key_env": "TEST_GEMINI_KEY",
                    "max_attempts": 3,
                    "request_interval_seconds": 0,
                    "daily_request_budgets": {"test-model": 100},
                },
                Path(directory),
            )
            client.api_key = "test-key"

            per_minute = MagicMock()
            per_minute.status_code = 429
            per_minute.headers = {}
            per_minute.text = (
                '{"error": {"code": 429, "message": "You exceeded your current quota.", '
                '"status": "RESOURCE_EXHAUSTED", "details": ['
                '{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{'
                '"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},'
                '{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "0s"}]}}'
            )
            with patch("httpx.post", return_value=per_minute) as mock_post:
                with self.assertRaises(ExternalValidationError) as ctx:
                    client.generate_json(model="test-model", prompt="test", schema={})
                # Retried, and NOT reported as exhaustion -- so the circuit stays shut.
                self.assertEqual(mock_post.call_count, 3)
                self.assertNotIsInstance(ctx.exception, QuotaExhaustedError)
                self.assertIn("rate limited", str(ctx.exception).lower())

            # Every attempt is charged to the local safety budget, not just the first.
            self.assertEqual(
                json.loads((Path(directory) / ".gemini_api_usage.json").read_text(encoding="utf-8"))["models"][
                    "test-model"
                ],
                3,
            )

    def test_quota_cooldown_lasts_until_the_quota_returns(self) -> None:
        """A spent daily quota is held until Pacific midnight, not for a fixed hour.

        A fixed hour is wrong in both directions: exhaust the budget at 10:00 PT
        and an hourly circuit wakes up to fail thirteen more times before the
        quota is back; exhaust it at 23:30 PT and the circuit stays shut for
        half an hour after it returned.
        """
        from brain.validators.gemini_validation import (
            _ProviderHealth,
            next_daily_quota_reset_epoch,
        )

        with tempfile.TemporaryDirectory() as directory:
            health = _ProviderHealth(Path(directory) / "health.json", threshold=3, cooldown_seconds=60)
            health.record(
                "gemini_api_triage",
                success=False,
                latency_ms=5,
                error="Local daily safety budget exhausted for lite (450/450)",
                quota_exhausted=True,
            )
            state = json.loads((Path(directory) / "health.json").read_text(encoding="utf-8"))
            entry = state["gemini_api_triage"]
            self.assertEqual(entry["open_reason"], "daily_quota_exhausted")
            # Held to the reset boundary, whenever that happens to be.
            self.assertAlmostEqual(entry["open_until_epoch"], next_daily_quota_reset_epoch(), delta=2)
            self.assertTrue(health.snapshot()["gemini_api_triage"]["circuit_open"])

    def test_quota_circuit_reports_when_the_quota_comes_back(self) -> None:
        """`before()` must say the quota is spent, not that something failed."""
        from brain.validators.gemini_validation import QuotaExhaustedError, _ProviderHealth

        with tempfile.TemporaryDirectory() as directory:
            health = _ProviderHealth(Path(directory) / "health.json", threshold=3, cooldown_seconds=60)
            health.record(
                "gemini_api_triage",
                success=False,
                latency_ms=5,
                error="Local daily safety budget exhausted",
                quota_exhausted=True,
            )
            with self.assertRaises(QuotaExhaustedError) as ctx:
                health.before("gemini_api_triage")
            self.assertIn("daily quota is spent", str(ctx.exception))
            self.assertIn("resets at", str(ctx.exception))

    def test_a_success_clears_the_quota_circuit(self) -> None:
        from brain.validators.gemini_validation import _ProviderHealth

        with tempfile.TemporaryDirectory() as directory:
            health = _ProviderHealth(Path(directory) / "health.json", threshold=3, cooldown_seconds=60)
            health.record("gemini_api_triage", success=False, latency_ms=5, error="x", quota_exhausted=True)
            health.record("gemini_api_triage", success=True, latency_ms=5)
            snapshot = health.snapshot()["gemini_api_triage"]
            self.assertFalse(snapshot["circuit_open"])
            self.assertEqual(snapshot["open_reason"], "")
            health.before("gemini_api_triage")  # must not raise

    def test_validate_audio_clean_local_acceptance_when_external_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = _service(root)
            # Both API and Web disabled / unavailable
            service.api.config["enabled"] = False
            service.web.config["enabled"] = False

            q_result = QualityResult(
                line_id="ch01_0001",
                chapter_number=1,
                character_id="narrator",
                status=ValidationStatus.ACCEPTED_WITH_WARNING,
                passed_hard_gates=True,
                warnings=["minor pacing difference"],
                wer=0.08,
                quality_score=0.88,
            )
            validated = service.validate_audio(
                project_dir=root,
                audio_path=root / "dummy.wav",
                line_text="Hello world",
                result=q_result,
            )
            self.assertFalse(validated.manual_review_required)
            self.assertEqual(validated.manual_review_reason, "")

    def test_validate_audio_hard_gate_failure_is_not_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = _service(root)
            service.api.config["enabled"] = False
            service.web.config["enabled"] = False

            q_result = QualityResult(
                line_id="ch01_0002",
                chapter_number=1,
                character_id="narrator",
                status=ValidationStatus.FAIL,
                passed_hard_gates=False,
                warnings=["audio clipping detected"],
                wer=0.45,
                quality_score=0.4,
            )
            validated = service.validate_audio(
                project_dir=root,
                audio_path=root / "dummy.wav",
                line_text="Hello world",
                result=q_result,
            )

    def test_validate_audio_benign_soft_warnings_auto_accepted_without_api_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = _service(root)
            service.api = _FakeApi([])
            service.web = _FakeWeb()

            # Segment has high quality score, low text error, and soft warnings only
            q_result = QualityResult(
                line_id="ch01_0003",
                chapter_number=1,
                character_id="narrator",
                status=ValidationStatus.ACCEPTED_WITH_WARNING,
                passed_hard_gates=True,
                warnings=["minor speech rate variance (pacing_anomaly)", "monotone_warning"],
                wer=0.04,
                effective_text_error=0.04,
                quality_score=0.86,
                clipping_detected=False,
                has_long_silence=False,
            )
            validated = service.validate_audio(
                project_dir=root,
                audio_path=root / "dummy.wav",
                line_text="The journey began at dawn.",
                result=q_result,
            )
            self.assertFalse(validated.manual_review_required)
            self.assertEqual(validated.manual_review_reason, "")
            self.assertGreaterEqual(validated.validation_confidence, 0.85)
            # Must NOT call external API or burn quota for benign soft warnings
            self.assertEqual(len(service.api.calls), 0)

    def test_validate_audio_critical_risk_escalates_to_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = _service(root)
            service.api = _FakeApi(
                [
                    {
                        "item_id": "ch01_0004",
                        "decision": "reject",
                        "confidence": 0.95,
                        "reason": "Severe hallucinated speech detected.",
                        "defects": ["hallucination"],
                    }
                ]
            )
            service.web = _FakeWeb()

            # Segment has critical text error (hallucination)
            q_result = QualityResult(
                line_id="ch01_0004",
                chapter_number=1,
                character_id="narrator",
                status=ValidationStatus.FAIL,
                passed_hard_gates=True,
                warnings=["severe text mismatch"],
                wer=0.35,
                effective_text_error=0.35,
                quality_score=0.55,
                clipping_detected=False,
                has_long_silence=False,
            )
            validated = service.validate_audio(
                project_dir=root,
                audio_path=root / "dummy.wav",
                line_text="The journey began at dawn.",
                result=q_result,
            )
            self.assertTrue(validated.manual_review_required)
            self.assertIn("rejected this segment", validated.manual_review_reason)
            # Must call external API for critical risks
            self.assertEqual(len(service.api.calls), 1)

    def test_a_self_pair_proposal_costs_no_grounding_call(self) -> None:
        """The roster stage really does return ("starling", "starling").

        Observed live on 2026-09-10 from gemini-3.5-flash-lite. `merge_veto`
        refuses a self-merge, but that runs in the caller, so without a cheap
        pre-filter the pair reaches the grounding stage and spends a second
        API call to be told nothing.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = _service(root)
            service.cast_adjudication_enabled = True
            service.api = _FakeApi(
                [
                    {
                        "proposals": [
                            {
                                "left_id": "starling",
                                "right_id": "starling",
                                "confidence": 0.9,
                                "reason": "same name",
                            }
                        ]
                    }
                ]
            )
            service.web = _FakeWeb()

            roster = {
                "starling": {"name": "Starling", "aliases": [], "gender": "female", "dialogue_count": 486},
                "dusk": {"name": "Dusk", "aliases": [], "gender": "male", "dialogue_count": 300},
            }
            result = service.adjudicate_cast(
                project_dir=root,
                roster=roster,
                evidence_for=lambda a, b: ["should never be asked for"],
            )

            self.assertEqual(result["merges"], [])
            # One roster call and nothing else.
            self.assertEqual(len(service.api.calls), 1)
            self.assertEqual(
                [t.get("reason") for t in result["trace"]],
                ["proposal names the same character twice"],
            )

    def test_a_low_confidence_reject_is_not_erased_by_a_later_accept(self) -> None:
        """An earlier stage's rejection must survive a later stage's acceptance.

        `external_validation_decision` holds only the LAST stage that answered.
        Reading that scalar meant a low-confidence "reject" from triage was
        overwritten by a low-confidence "accept" from a later stage, and the
        segment was then auto-accepted with `manual_review_required = False`.
        Only critical-risk segments reach this loop at all, so it was the worst
        possible place to drop a rejection. The full history is scanned instead.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = _service(root)
            # Both below auto_accept (0.9), so neither returns early and the
            # loop falls through to the end-of-stages decision.
            service.api = _FakeApi(
                [
                    {
                        "item_id": "ch01_0009",
                        "decision": "reject",
                        "confidence": 0.60,
                        "reason": "Possible clipped consonant.",
                        "defects": ["clipping"],
                    },
                    {
                        "item_id": "ch01_0009",
                        "decision": "accept",
                        "confidence": 0.62,
                        "reason": "Sounds fine to me.",
                        "defects": [],
                    },
                ]
            )
            service.web = _FakeWeb(
                [
                    {
                        "item_id": "ch01_0009",
                        "decision": "accept",
                        "confidence": 0.55,
                        "reason": "No audible defect.",
                        "defects": [],
                    }
                ]
            )

            q_result = QualityResult(
                line_id="ch01_0009",
                chapter_number=1,
                character_id="narrator",
                status=ValidationStatus.ACCEPTED_WITH_WARNING,
                passed_hard_gates=True,
                warnings=["soft warning"],
                wer=0.05,
                effective_text_error=0.05,
                quality_score=0.60,
                clipping_detected=False,
                has_long_silence=False,
            )
            validated = service.validate_audio(
                project_dir=root,
                audio_path=root / "dummy.wav",
                line_text="The journey began at dawn.",
                result=q_result,
            )

            decisions = [entry["decision"] for entry in validated.external_validation_history]
            self.assertIn("reject", decisions, "the rejection must be on the record")
            self.assertEqual(validated.external_validation_decision, "accept", "last writer still wins the scalar")
            self.assertTrue(
                validated.manual_review_required,
                "a segment any stage rejected must reach a human, not be auto-accepted",
            )


if __name__ == "__main__":
    unittest.main()
