"""Tiered dialogue attribution adjudicator.

Orchestrates multi-tier attribution resolution:
- Tier 1: Local Qwen 27B micro-prompt on isolated conversation window
  with anti-overconfidence guardrails (fuzzy quote verification, gender/pronoun
  consistency, canonical alias resolution, reciprocal turn consistency).
- Tier 2: Escalation to Gemini API (flash-lite / flash / web pro) via existing
  GeminiValidationService for borderline/failed cases.
- Tier 3: Review Inbox routing for remaining human edge cases.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from brain.director.attribution_detector import SuspiciousTurn
from brain.director.ollama_client import OllamaClient
from brain.director.script_generator import _GENERIC_ROLE_DESCRIPTORS, ScriptGenerator
from brain.validators.gemini_validation import GeminiValidationService
from shared.artifacts import atomic_write_json
from shared.constants import DEFAULT_OLLAMA_MODEL
from shared.models import CharacterRegistry, ScriptChapter, ScriptLine

logger = logging.getLogger(__name__)

_MALE_PRONOUN_TAGS = re.compile(
    r"\b(?:he\s+(?:said|replied|asked|whispered|muttered|demanded|called|shouted|murmured|answered|groaned|laughed|sighed|continued|repeated)|his\s+voice|said\s+he|asked\s+he)\b",
    re.IGNORECASE,
)
_FEMALE_PRONOUN_TAGS = re.compile(
    r"\b(?:she\s+(?:said|replied|asked|whispered|muttered|demanded|called|shouted|murmured|answered|groaned|laughed|sighed|continued|repeated)|her\s+voice|said\s+she|asked\s+she)\b",
    re.IGNORECASE,
)

_EXPLICIT_SPEECH_TAG_PATTERN = re.compile(
    r"\b(?:(?:he|she|they|[a-z]{3,})\s+(?:said|asked|replied|whispered|muttered|demanded|screamed|called|shouted|murmured|answered|groaned|laughed|sighed|continued)|"
    r"(?:said|asked|replied|whispered|muttered|demanded|screamed|called|shouted|murmured|answered|groaned|laughed|sighed)\s+(?:he|she|they|[a-z]{3,}))\b",
    re.IGNORECASE,
)


def _has_speech_tag(reason: str, evidence: str) -> bool:
    combined = f"{evidence}\n{reason}"
    return bool(_EXPLICIT_SPEECH_TAG_PATTERN.search(combined))


def _normalize_text(s: str) -> str:
    s = re.sub(r'["\'\u201c\u201d\u2018\u2019\u00ab\u00bb`]', '"', s)
    s = re.sub(r"[\u2014\u2013\u2212]", "-", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip().casefold()


def _fuzzy_quote_in_context(evidence_quote: str, scene_text: str) -> tuple[bool, str]:
    """Verify whether evidence quote appears in the scene context (exact or fuzzy)."""
    if not evidence_quote or not evidence_quote.strip():
        return False, "missing_evidence_quote"
    norm_quote = _normalize_text(evidence_quote)
    norm_scene = _normalize_text(scene_text)
    if not norm_quote:
        return False, "empty_evidence_quote"
    if norm_quote in norm_scene:
        return True, "exact_match"

    # Sliding window fuzzy match for quotes >= 10 chars
    q_len = len(norm_quote)
    best_ratio = 0.0
    if q_len >= 10 and len(norm_scene) >= q_len:
        step = max(1, q_len // 4)
        for start in range(0, max(1, len(norm_scene) - q_len + 1), step):
            sub = norm_scene[start : start + q_len + step]
            ratio = difflib.SequenceMatcher(None, norm_quote, sub).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
            if best_ratio >= 0.85:
                return True, f"fuzzy_match (ratio={best_ratio:.2f})"
        if best_ratio >= 0.85:
            return True, f"fuzzy_match (ratio={best_ratio:.2f})"

    direct_ratio = difflib.SequenceMatcher(None, norm_quote, norm_scene).ratio()
    if direct_ratio >= 0.85:
        return True, f"fuzzy_match (ratio={direct_ratio:.2f})"

    return False, f"evidence_not_found (best_ratio={max(best_ratio, direct_ratio):.2f})"


def _check_gender_pronoun_consistency(
    speaker_id: str,
    evidence_and_reason: str,
    registry: CharacterRegistry,
) -> tuple[bool, str]:
    """Reject candidate if cited speech tags contain opposite-gender pronouns."""
    char = registry.characters.get(speaker_id)
    if not char or not char.gender:
        return True, "unknown_or_unregistered_gender"

    gender_val = char.gender.value.lower() if hasattr(char.gender, "value") else str(char.gender).lower()
    if gender_val not in ("male", "female"):
        return True, "neutral_or_other_gender"

    has_male_tag = bool(_MALE_PRONOUN_TAGS.search(evidence_and_reason))
    has_female_tag = bool(_FEMALE_PRONOUN_TAGS.search(evidence_and_reason))

    if gender_val == "male" and has_female_tag and not has_male_tag:
        return False, "Male speaker contradicts cited female speech tag"
    if gender_val == "female" and has_male_tag and not has_female_tag:
        return False, "Female speaker contradicts cited male speech tag"

    return True, "gender_consistent"


def _resolve_speaker_alias(raw_speaker: str, registry: CharacterRegistry) -> tuple[str | None, str]:
    """Map nicknames or aliases to canonical registered character IDs."""
    if not raw_speaker:
        return None, "empty_speaker"
    clean = raw_speaker.strip().casefold()
    clean_id = clean.replace(" ", "_")

    if clean_id in registry.characters:
        return clean_id, "exact_id"
    if clean in registry.characters:
        return clean, "exact_id"

    matches: set[str] = set()
    for cid, c in registry.characters.items():
        if cid == "narrator":
            continue
        c_name = str(c.name or "").strip().casefold()
        if clean == c_name or clean == c_name.replace(" ", "_"):
            matches.add(cid)
            continue
        for alias in c.aliases or []:
            a_clean = str(alias).strip().casefold()
            if len(a_clean.split()) == 1 and a_clean in _GENERIC_ROLE_DESCRIPTORS:
                continue
            if clean == a_clean or clean == a_clean.replace(" ", "_"):
                matches.add(cid)
                break
        if len(clean) >= 4 and clean not in _GENERIC_ROLE_DESCRIPTORS:
            cid_parts = [
                p.casefold() for p in cid.split("_") if len(p) >= 4 and p.casefold() not in _GENERIC_ROLE_DESCRIPTORS
            ]
            if clean in cid_parts:
                matches.add(cid)

    if len(matches) == 1:
        return next(iter(matches)), "alias_resolved"
    if len(matches) > 1:
        return None, f"ambiguous_alias (matched: {sorted(matches)})"
    return None, f"unresolved_speaker '{raw_speaker}'"


def _extract_json(text: str) -> dict[str, Any]:
    """Extract JSON object handling code fences, preamble, and raw JSON."""
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start : end + 1]
    start = candidate.find("{")
    if start < 0:
        raise ValueError("Response did not contain a JSON object")
    value, _ = json.JSONDecoder().raw_decode(candidate[start:])
    if not isinstance(value, dict):
        raise TypeError("Response was not a JSON object")
    return value


@dataclass
class AdjudicationResult:
    line_id: str
    chapter_number: int
    text: str
    original_speaker: str
    resolved_speaker: str | None
    resolver_tier: str
    confidence: float
    reason: str
    evidence_quote: str
    guardrail_results: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AdjudicationReport:
    results: list[AdjudicationResult]
    summary: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "results": [r.to_dict() for r in self.results],
        }


class TieredAttributionAdjudicator:
    """Multi-tiered adjudicator for dialogue speaker attributions."""

    def __init__(
        self,
        ollama: OllamaClient,
        external_validator: GeminiValidationService,
        registry: CharacterRegistry,
        *,
        local_auto_accept: float = 0.95,
        gemini_auto_accept: float = 0.90,
        ollama_temperature: float = 0.1,
    ):
        self.ollama = ollama
        self.external_validator = external_validator
        self.registry = registry
        self.local_auto_accept = local_auto_accept
        self.gemini_auto_accept = gemini_auto_accept
        self.ollama_temperature = ollama_temperature

    def adjudicate(
        self,
        suspicious_turns: list[SuspiciousTurn],
        project_dir: Path,
        chapters: list[ScriptChapter],
        *,
        dry_run: bool = False,
    ) -> AdjudicationReport:
        """Run Tier 1 micro-adjudication with guardrails on all suspicious turns.

        Lines meeting all guardrails and confidence >= local_auto_accept are resolved.
        Lines failing any check are marked for Tier 2 escalation.
        """
        lines_by_id: dict[str, ScriptLine] = {line.line_id: line for chapter in chapters for line in chapter.lines}
        chapter_map: dict[int, ScriptChapter] = {c.chapter_number: c for c in chapters}

        results: list[AdjudicationResult] = []

        # -------------------------------------------------------------
        # Phase 1: Tier 1 Local Qwen Micro-Adjudication
        # -------------------------------------------------------------
        for turn in suspicious_turns:
            result = self._adjudicate_turn_tier1(turn, chapter_map.get(turn.chapter_number))
            results.append(result)

        # -------------------------------------------------------------
        # Phase 2: Guardrail 4 — Reciprocal Turn Consistency Check
        # -------------------------------------------------------------
        self._apply_reciprocal_turn_guardrail(results, chapter_map)

        # -------------------------------------------------------------
        # Phase 3: Apply Decisions (unless dry_run)
        # -------------------------------------------------------------
        local_resolved_count = 0
        escalated_count = 0

        for res in results:
            line = lines_by_id.get(res.line_id)
            if line is None:
                continue

            if res.resolver_tier == "local_qwen" and res.resolved_speaker:
                local_resolved_count += 1
                if not dry_run:
                    prev_speaker = line.speaker
                    line.speaker = res.resolved_speaker
                    line.speaker_confidence = res.confidence
                    line.speaker_evidence = (
                        f"Tier 1 Qwen 27B micro-adjudication: {res.evidence_quote} ({res.reason})"
                    )[:4000]
                    line.attribution_resolver = "local_qwen_micro"
                    line.attribution_review_required = False
                    line.attribution_review_reason = ""
                    line.attribution_confidence_history.append(
                        {
                            "resolver": "local_qwen_micro",
                            "model": getattr(self.ollama, "model", DEFAULT_OLLAMA_MODEL),
                            "decision": "resolved",
                            "speaker_id": res.resolved_speaker,
                            "confidence": res.confidence,
                            "reason": res.reason,
                            "evidence": res.evidence_quote,
                        }
                    )
                    logger.info(
                        "[TieredAttribution] Repaired %s (%s -> %s, conf=%.2f): %s",
                        res.line_id,
                        prev_speaker,
                        res.resolved_speaker,
                        res.confidence,
                        res.reason,
                    )
            else:
                escalated_count += 1
                if not dry_run:
                    line.attribution_review_required = True
                    line.speaker_confidence = min(float(line.speaker_confidence or 0.0), 0.54)
                    line.attribution_review_reason = (f"Tier 1 micro-adjudication escalated: {res.reason}")[:4000]
                    line.attribution_confidence_history.append(
                        {
                            "resolver": "local_qwen_micro",
                            "model": getattr(self.ollama, "model", DEFAULT_OLLAMA_MODEL),
                            "decision": "abstain",
                            "speaker_id": res.resolved_speaker,
                            "confidence": res.confidence,
                            "reason": res.reason,
                            "evidence": res.evidence_quote,
                        }
                    )

        if not dry_run and local_resolved_count > 0:
            ScriptGenerator.sync_dialogue_counts(chapters, self.registry)

        summary = {
            "total_suspicious": len(suspicious_turns),
            "local_resolved": local_resolved_count,
            "escalated_to_tier2": escalated_count,
            "dry_run": dry_run,
        }

        report = AdjudicationReport(results=results, summary=summary)

        # Write preview / report
        preview_path = (
            project_dir
            / "external_validation"
            / ("tiered_attribution_preview.json" if dry_run else "tiered_attribution_report.json")
        )
        try:
            preview_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(preview_path, report.to_dict())
        except Exception as exc:
            logger.warning("[TieredAttribution] Failed writing report to %s: %s", preview_path, exc)

        return report

    def _adjudicate_turn_tier1(
        self,
        turn: SuspiciousTurn,
        chapter: ScriptChapter | None,
    ) -> AdjudicationResult:
        """Run single-turn Qwen micro-prompt and apply guardrails 1-3."""
        # Build scene character context
        active_ids = {
            neighbor["speaker"]
            for neighbor in turn.surrounding_lines
            if neighbor.get("speaker") and neighbor.get("speaker") != "narrator"
        }
        active_ids.add(turn.current_speaker)
        active_ids.discard("narrator")

        scene_characters: list[dict[str, Any]] = []
        for cid in sorted(active_ids):
            char = self.registry.characters.get(cid)
            if char:
                gender_str = char.gender.value if hasattr(char.gender, "value") else str(char.gender)
                scene_characters.append(
                    {
                        "id": cid,
                        "name": char.name,
                        "gender": gender_str,
                        "aliases": char.aliases or [],
                    }
                )

        # Format surrounding lines
        context_lines_formatted = []
        for neighbor in turn.surrounding_lines:
            prefix = ">>> [TARGET]" if neighbor.get("is_target") else "   "
            context_lines_formatted.append(
                f"{prefix} [{neighbor['line_id']}] {neighbor['speaker']}: {neighbor['text']}"
            )
        formatted_context = "\n".join(context_lines_formatted)

        prompt = (
            "You are an audiobook dialogue attribution expert. Determine the correct speaker for "
            "the TARGET line based on the surrounding conversation context.\n\n"
            "CHARACTERS IN THIS SCENE:\n"
            f"{json.dumps(scene_characters, ensure_ascii=False, indent=2)}\n\n"
            "SURROUNDING CONTEXT (the TARGET line is marked with >>>):\n"
            f"{formatted_context}\n\n"
            "TARGET LINE:\n"
            f"  Line ID: {turn.line_id}\n"
            f"  Text: {turn.text}\n"
            f"  Current Assigned Speaker: {turn.current_speaker}\n\n"
            "RULES:\n"
            "1. In two-party dialogue without explicit speech tags, turns strictly ALTERNATE between speakers.\n"
            "2. If a line addresses someone by name (e.g., '..., Dusk'), the SPEAKER is the OTHER character talking TO that person.\n"
            "3. Match pronouns in action beats: 'He frowned. \"Quote\"' means a MALE character speaks.\n"
            "4. Do NOT assume consecutive quotes are a monologue unless there is explicit evidence of continuation (e.g., 'he continued', 'she went on').\n"
            "5. Return ONLY a JSON object with this exact schema:\n"
            '{"speaker_id": "string", "confidence": 0.0-1.0, "reason": "brief explanation", "evidence_quote": "verbatim quote or phrase from context proving attribution"}'
        )

        try:
            raw_response = self.ollama.generate(
                prompt,
                temperature=self.ollama_temperature,
                format="json",
            )
            parsed = _extract_json(raw_response)
        except Exception as exc:
            logger.warning("[TieredAttribution] Qwen failed on %s: %s", turn.line_id, exc)
            return AdjudicationResult(
                line_id=turn.line_id,
                chapter_number=turn.chapter_number,
                text=turn.text,
                original_speaker=turn.current_speaker,
                resolved_speaker=None,
                resolver_tier="gemini_api",
                confidence=0.0,
                reason=f"Tier 1 LLM generation/parsing error: {exc}",
                evidence_quote="",
                guardrail_results={"llm_call": False},
            )

        raw_speaker = str(parsed.get("speaker_id") or "")
        confidence = float(parsed.get("confidence") or 0.0)
        reason = str(parsed.get("reason") or "")
        evidence_quote = str(parsed.get("evidence_quote") or "")

        # Guardrail 3: Canonical Alias Resolution
        resolved_speaker, alias_detail = _resolve_speaker_alias(raw_speaker, self.registry)
        alias_passed = bool(resolved_speaker is not None)

        # Guardrail 1: Fuzzy Quote in Context
        quote_passed, quote_detail = _fuzzy_quote_in_context(evidence_quote, turn.scene_text)
        if not quote_passed and confidence > 0.80:
            # Downgrade confidence if quote is absent/fabricated
            confidence = max(0.0, confidence - 0.15)

        # Guardrail 2: Gender/Pronoun Consistency
        gender_passed, gender_detail = (
            _check_gender_pronoun_consistency(
                resolved_speaker or raw_speaker,
                f"{reason} {evidence_quote}",
                self.registry,
            )
            if alias_passed
            else (True, "skipped_due_to_unresolved_alias")
        )

        guardrail_status = {
            "alias_resolution": {"passed": alias_passed, "detail": alias_detail},
            "quote_in_context": {"passed": quote_passed, "detail": quote_detail},
            "gender_pronoun": {"passed": gender_passed, "detail": gender_detail},
        }

        all_passed = alias_passed and gender_passed and confidence >= self.local_auto_accept

        if all_passed and resolved_speaker:
            return AdjudicationResult(
                line_id=turn.line_id,
                chapter_number=turn.chapter_number,
                text=turn.text,
                original_speaker=turn.current_speaker,
                resolved_speaker=resolved_speaker,
                resolver_tier="local_qwen",
                confidence=confidence,
                reason=reason,
                evidence_quote=evidence_quote,
                guardrail_results=guardrail_status,
            )
        escalate_reasons = []
        if not alias_passed:
            escalate_reasons.append(alias_detail)
        if not gender_passed:
            escalate_reasons.append(gender_detail)
        if confidence < self.local_auto_accept:
            escalate_reasons.append(f"Confidence {confidence:.2f} < threshold {self.local_auto_accept:.2f}")
        full_reason = "; ".join(escalate_reasons) or reason

        return AdjudicationResult(
            line_id=turn.line_id,
            chapter_number=turn.chapter_number,
            text=turn.text,
            original_speaker=turn.current_speaker,
            resolved_speaker=resolved_speaker,
            resolver_tier="gemini_api",
            confidence=confidence,
            reason=full_reason,
            evidence_quote=evidence_quote,
            guardrail_results=guardrail_status,
        )

    def _apply_reciprocal_turn_guardrail(
        self,
        results: list[AdjudicationResult],
        chapter_map: dict[int, ScriptChapter],
    ) -> None:
        """Guardrail 4: Revert consecutive question/answer pairs resolved to the same speaker when untagged."""
        res_by_id = {r.line_id: r for r in results}

        for chapter_number, chapter in chapter_map.items():
            dialogue_lines = [
                line
                for line in chapter.lines
                if line.speaker != "narrator"
                and (line.dialogue_kind == "spoken" or line.text.strip().startswith(('"', "“", "‘", "'", "—", "–")))
            ]

            for i in range(len(dialogue_lines) - 1):
                cur = dialogue_lines[i]
                nxt = dialogue_lines[i + 1]

                # Current speakers considering Tier 1 resolution
                cur_res = res_by_id.get(cur.line_id)
                nxt_res = res_by_id.get(nxt.line_id)

                cur_speaker = (
                    cur_res.resolved_speaker
                    if (cur_res and cur_res.resolver_tier == "local_qwen" and cur_res.resolved_speaker)
                    else cur.speaker
                )
                nxt_speaker = (
                    nxt_res.resolved_speaker
                    if (nxt_res and nxt_res.resolver_tier == "local_qwen" and nxt_res.resolved_speaker)
                    else nxt.speaker
                )

                if cur_speaker == nxt_speaker and cur_speaker != "narrator":
                    # Check if one is a question
                    if "?" in cur.text or "?" in nxt.text:
                        # If either turn has an explicit speech tag or continuation evidence,
                        # the same speaker is legitimately speaking consecutive sentences
                        cur_has_tag = _has_speech_tag(
                            getattr(cur_res, "reason", "") or "",
                            getattr(cur_res, "evidence_quote", "") or "",
                        )
                        nxt_has_tag = _has_speech_tag(
                            getattr(nxt_res, "reason", "") or "",
                            getattr(nxt_res, "evidence_quote", "") or "",
                        )
                        if cur_has_tag or nxt_has_tag:
                            continue

                        # If both are untagged, invalidate local_qwen and escalate to Gemini!
                        if cur_res and cur_res.resolver_tier == "local_qwen":
                            cur_res.resolver_tier = "gemini_api"
                            cur_res.reason += (
                                " [Rejected by Guardrail 4: Untagged reciprocal Q&A same-speaker conflict]"
                            )
                            cur_res.guardrail_results["reciprocal_turn"] = {
                                "passed": False,
                                "detail": f"Untagged same-speaker conflict with {nxt.line_id}",
                            }
                            logger.warning(
                                "[TieredAttribution] Guardrail 4 rejected local resolution on %s",
                                cur.line_id,
                            )
                        if nxt_res and nxt_res.resolver_tier == "local_qwen":
                            nxt_res.resolver_tier = "gemini_api"
                            nxt_res.reason += (
                                " [Rejected by Guardrail 4: Untagged reciprocal Q&A same-speaker conflict]"
                            )
                            nxt_res.guardrail_results["reciprocal_turn"] = {
                                "passed": False,
                                "detail": f"Untagged same-speaker conflict with {cur.line_id}",
                            }
                            logger.warning(
                                "[TieredAttribution] Guardrail 4 rejected local resolution on %s",
                                nxt.line_id,
                            )
