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
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from brain.director.attribution_detector import SuspiciousTurn, _is_dialogue_line
from brain.director.ollama_client import OllamaClient, OllamaError
from brain.director.script_generator import (
    _GENERIC_ROLE_DESCRIPTORS,
    _HE_SPEECH_TAG,
    _SHE_SPEECH_TAG,
    ScriptGenerator,
)
from brain.validators.gemini_validation import GeminiValidationService
from shared.artifacts import atomic_write_json
from shared.constants import DEFAULT_OLLAMA_MODEL, Gender
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


def _label_support(
    lines: list[dict[str, Any]] | list[ScriptLine],
    index: int,
    registry: CharacterRegistry,
) -> str:
    """Say what backs the speaker label on `lines[index]`, for the prompt.

    A neighbouring label is evidence of wildly varying quality: the book naming
    the speaker outright, or a previous model's guess. Presented flat they look
    identical, and the alternation rule then propagates whichever ones are
    wrong. Only the book's own speech tag counts as confirmation here.
    """
    item = lines[index]
    speaker = str(item.get("speaker") if isinstance(item, dict) else getattr(item, "speaker", "") or "")
    if not speaker or speaker == "narrator":
        return ""
    if index + 1 >= len(lines):
        return "  [unverified]"
    following = lines[index + 1]
    following_speaker = str(
        following.get("speaker") if isinstance(following, dict) else getattr(following, "speaker", "") or ""
    )
    if following_speaker != "narrator":
        return "  [unverified]"
    tag = str(following.get("text") if isinstance(following, dict) else getattr(following, "text", "") or "").strip()
    lead = next((char for char in tag if char.isalpha()), "")
    if not lead or not lead.islower():
        return "  [unverified]"

    named, kind, gender = ScriptGenerator._dialogue_tag_evidence(tag, registry)
    if (
        gender is not None
        and kind == "pronoun_gender"
        and not (_HE_SPEECH_TAG.search(tag) or _SHE_SPEECH_TAG.search(tag))
    ):
        gender = None
    if named:
        if named == speaker:
            return "  [confirmed by the speech tag below]"
        return f"  [CONTRADICTED: the speech tag below names {named}]"
    if gender is not None:
        candidate = registry.characters.get(speaker)
        if candidate and candidate.gender in (Gender.MALE, Gender.FEMALE):
            if candidate.gender == gender:
                return "  [confirmed by the speech tag below]"
            return f"  [CONTRADICTED: the speech tag below is {gender.value}]"
    return "  [unverified]"


def _attached_tag_evidence(
    turn: SuspiciousTurn,
    registry: CharacterRegistry,
) -> tuple[str | None, Gender | None, str]:
    """Read the speech tag the author attached to this line, if there is one.

    Narration right after a quote whose first letter is lowercase is a
    grammatical continuation of it, so it is the author naming the speaker
    outright. Narration starting with a capital is a new sentence and merely a
    reaction ("Dahlia laughed at that."), which says nothing about who just
    spoke; reading those as tags is how a bystander ends up owning the line.

    Parsing is delegated to `_dialogue_tag_evidence`, so a tag is read the same
    way here as everywhere else in the pipeline.
    """
    lines = list(turn.surrounding_lines or [])
    index = next((i for i, line in enumerate(lines) if line.get("is_target")), None)
    if index is None or index + 1 >= len(lines):
        return None, None, ""
    following = lines[index + 1]
    if str(following.get("speaker") or "") != "narrator":
        return None, None, ""
    tag = str(following.get("text") or "").strip()
    lead = next((char for char in tag if char.isalpha()), "")
    if not lead or not lead.islower():
        return None, None, ""
    exact, kind, gender = ScriptGenerator._dialogue_tag_evidence(tag, registry)
    if (
        gender is not None
        and kind == "pronoun_gender"
        and not (_HE_SPEECH_TAG.search(tag) or _SHE_SPEECH_TAG.search(tag))
    ):
        # A lone pronoun elsewhere in the sentence is not the speaker: "the
        # seated halfling said, then ... as she neared" is about the person
        # approaching. Too weak to refuse an attribution on.
        gender = None
    return exact, gender, tag


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


def _has_tag_confirmation(
    line: ScriptLine,
    following_line: ScriptLine | None,
    registry: CharacterRegistry,
) -> bool:
    """Say if following narrator line is an explicit speech tag confirming line's speaker."""
    if following_line is None or following_line.speaker != "narrator":
        return False
    tag = str(following_line.text or "").strip()
    lead = next((char for char in tag if char.isalpha()), "")
    if not lead or not lead.islower():
        return False

    named, kind, gender = ScriptGenerator._dialogue_tag_evidence(tag, registry)
    if (
        gender is not None
        and kind == "pronoun_gender"
        and not (_HE_SPEECH_TAG.search(tag) or _SHE_SPEECH_TAG.search(tag))
    ):
        gender = None
    if named:
        return named == line.speaker
    if gender is not None:
        candidate = registry.characters.get(line.speaker)
        if candidate and candidate.gender in (Gender.MALE, Gender.FEMALE):
            return candidate.gender == gender
    return False


def _find_unconfirmed_run_line_ids(
    chapter: ScriptChapter,
    registry: CharacterRegistry,
) -> set[str]:
    """Find line_ids belonging to same-speaker runs of >=3 dialogue turns with 0 tag confirmations."""
    spoken_indices = [
        i for i, line in enumerate(chapter.lines) if _is_dialogue_line(line) and line.speaker != "narrator"
    ]
    if not spoken_indices:
        return set()

    unconfirmed_ids: set[str] = set()
    cur_run: list[int] = []

    for idx in spoken_indices:
        line = chapter.lines[idx]
        following = chapter.lines[idx + 1] if idx + 1 < len(chapter.lines) else None
        tagged = _has_tag_confirmation(line, following, registry)

        if not tagged:
            if not cur_run or chapter.lines[cur_run[-1]].speaker == line.speaker:
                cur_run.append(idx)
            else:
                if len(cur_run) >= 3:
                    for i in cur_run:
                        unconfirmed_ids.add(chapter.lines[i].line_id)
                cur_run = [idx]
        else:
            if len(cur_run) >= 3:
                for i in cur_run:
                    unconfirmed_ids.add(chapter.lines[i].line_id)
            cur_run = []

    if len(cur_run) >= 3:
        for i in cur_run:
            unconfirmed_ids.add(chapter.lines[i].line_id)

    return unconfirmed_ids


def _extract_block_json(raw_text: str, expected_ids: set[str]) -> dict[str, dict[str, Any]]:
    """Extract block attribution JSON and validate keys match expected target line IDs."""
    parsed = _extract_json(raw_text)
    if not isinstance(parsed, dict):
        raise TypeError("Response was not a JSON object")

    # If the response wrapped the map in a top-level key like "attributions", "lines", or "results"
    for wrapper_key in ("attributions", "lines", "results", "dialogue", "turns"):
        if wrapper_key in parsed and isinstance(parsed[wrapper_key], dict):
            inner = parsed[wrapper_key]
            if any(k in expected_ids for k in inner):
                parsed = inner
                break
        elif wrapper_key in parsed and isinstance(parsed[wrapper_key], list):
            items = parsed[wrapper_key]
            if items and isinstance(items[0], dict) and "line_id" in items[0]:
                parsed = {item["line_id"]: item for item in items if isinstance(item, dict) and "line_id" in item}
                break

    # Check if raw parsed is a list of objects with line_id
    if isinstance(parsed, list):
        parsed = {item["line_id"]: item for item in parsed if isinstance(item, dict) and "line_id" in item}

    result_map: dict[str, dict[str, Any]] = {}
    for k, v in parsed.items():
        if isinstance(v, dict):
            result_map[str(k)] = v
        elif isinstance(v, str):
            result_map[str(k)] = {
                "speaker_id": v,
                "confidence": 0.50,
                "reason": "bare-string block assignment without confidence or evidence",
                "evidence_quote": "",
            }

    missing = expected_ids - set(result_map.keys())
    if missing:
        raise ValueError(f"Block response missing target line_ids: {sorted(missing)}")

    extra = set(result_map.keys()) - expected_ids
    if extra:
        raise ValueError(f"Block response contained unknown/invented line_ids: {sorted(extra)}")

    return result_map


@dataclass
class DialogueBlock:
    chapter_number: int
    start_line_idx: int
    end_line_idx: int
    spoken_line_indices: list[int]
    suspicious_turns: list[SuspiciousTurn]


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
    summary: dict[str, Any]

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
        block_adjudication_enabled: bool = False,
        max_suspicious_per_call: int = 8,
        only_unconfirmed_runs: bool = True,
    ):
        self.ollama = ollama
        self.external_validator = external_validator
        self.registry = registry
        self.local_auto_accept = local_auto_accept
        self.gemini_auto_accept = gemini_auto_accept
        self.ollama_temperature = ollama_temperature
        self.block_adjudication_enabled = block_adjudication_enabled
        # A non-positive cap would make `_split_group` recurse forever: a
        # one-suspicious group can never be split smaller than itself.
        self.max_suspicious_per_call = max(1, int(max_suspicious_per_call))
        self.only_unconfirmed_runs = only_unconfirmed_runs

    def adjudicate(
        self,
        suspicious_turns: list[SuspiciousTurn],
        project_dir: Path,
        chapters: list[ScriptChapter],
        *,
        dry_run: bool = False,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> AdjudicationReport:
        """Run Tier 1 micro-adjudication with guardrails on all suspicious turns.

        Lines meeting all guardrails and confidence >= local_auto_accept are resolved.
        Lines failing any check are marked for Tier 2 escalation.

        `progress_callback(done, total)` is invoked as each turn is adjudicated.
        This loop is one LLM call per turn and can run for over an hour on a
        long book -- 1,089 turns on a 32-chapter one -- so without it the
        dashboard shows whatever Pass 2 last said and the run looks hung.
        """
        lines_by_id: dict[str, ScriptLine] = {line.line_id: line for chapter in chapters for line in chapter.lines}
        chapter_map: dict[int, ScriptChapter] = {c.chapter_number: c for c in chapters}

        results: list[AdjudicationResult] = []

        # -------------------------------------------------------------
        # Phase 1: Tier 1 Local Qwen Micro-Adjudication
        # -------------------------------------------------------------
        total_turns = len(suspicious_turns)
        self._unconfirmed_run_cache: dict[int, set[str]] = {}
        current_done = 0
        blocks_adjudicated_count = 0
        block_fallbacks_count = 0

        if self.block_adjudication_enabled and chapter_map:
            blocks = self._group_suspicious_into_blocks(suspicious_turns, chapter_map)
            processed_line_ids: set[str] = set()
            for block in blocks:
                chapter = chapter_map.get(block.chapter_number)
                if self._is_block_targeted(block, chapter):
                    block_results: list[AdjudicationResult] | None = None
                    try:
                        block_results = self._adjudicate_block_tier1(block.suspicious_turns, chapter)
                    except Exception as exc:  # noqa: BLE001 - any failure must fall back to per-line
                        logger.warning(
                            "[TieredAttribution] Block adjudication failed for block in ch%d (%s): %s; falling back to per-line",
                            block.chapter_number,
                            block.suspicious_turns[0].line_id if block.suspicious_turns else "unknown",
                            exc,
                        )
                        block_results = None

                    if block_results is not None:
                        blocks_adjudicated_count += 1
                        results.extend(block_results)
                        for r in block_results:
                            processed_line_ids.add(r.line_id)
                        current_done += len(block.suspicious_turns)
                        if progress_callback is not None:
                            try:
                                progress_callback(current_done, total_turns)
                            except Exception as exc:  # noqa: BLE001 - reporting must never fail
                                logger.debug("Attribution progress callback raised: %s", exc)
                        continue
                    block_fallbacks_count += 1

                # Fallback / non-targeted turns in this block
                for turn in block.suspicious_turns:
                    res = self._adjudicate_turn_tier1(turn, chapter)
                    results.append(res)
                    processed_line_ids.add(turn.line_id)
                    current_done += 1
                    if progress_callback is not None:
                        try:
                            progress_callback(current_done, total_turns)
                        except Exception as exc:  # noqa: BLE001 - reporting must never fail
                            logger.debug("Attribution progress callback raised: %s", exc)

            # Fallback for ANY turn not captured in blocks (e.g. missing chapter in map, empty spoken indices)
            for turn in suspicious_turns:
                if turn.line_id not in processed_line_ids:
                    res = self._adjudicate_turn_tier1(turn, chapter_map.get(turn.chapter_number))
                    results.append(res)
                    processed_line_ids.add(turn.line_id)
                    current_done += 1
                    if progress_callback is not None:
                        try:
                            progress_callback(current_done, total_turns)
                        except Exception as exc:  # noqa: BLE001 - reporting must never fail
                            logger.debug("Attribution progress callback raised: %s", exc)
        else:
            for index, turn in enumerate(suspicious_turns, start=1):
                result = self._adjudicate_turn_tier1(turn, chapter_map.get(turn.chapter_number))
                results.append(result)
                if progress_callback is not None:
                    try:
                        progress_callback(index, total_turns)
                    except Exception as exc:  # noqa: BLE001 - reporting must never fail the pass
                        logger.debug("Attribution progress callback raised: %s", exc)

        # -------------------------------------------------------------
        # Phase 2: Guardrail 4 — Reciprocal Turn Consistency Check
        # -------------------------------------------------------------
        self._apply_reciprocal_turn_guardrail(results, chapter_map)

        # -------------------------------------------------------------
        # Phase 3: Apply Decisions (unless dry_run)
        # -------------------------------------------------------------
        local_resolved_count = 0
        # A resolution that keeps the speaker is a confirmation, not a repair.
        # Counted apart because the two say opposite things about scripting
        # quality: many confirmations mean the suspicion heuristic is broad,
        # many reattributions mean attribution itself is wrong.
        confirmed_count = 0
        reattributed_count = 0
        escalated_count = 0
        tag_overruled_count = 0

        for res in results:
            line = lines_by_id.get(res.line_id)
            if line is None:
                continue

            if res.resolver_tier == "deterministic_tag" and res.resolved_speaker:
                # Recorded under its own resolver so the trail says the source
                # text decided this, not a model.
                local_resolved_count += 1
                tag_overruled_count += 1
                if not dry_run:
                    prev_speaker = line.speaker
                    line.speaker = res.resolved_speaker
                    line.speaker_confidence = 1.0
                    line.speaker_evidence = f"Attached speech tag: {res.evidence_quote}"[:4000]
                    line.attribution_resolver = "deterministic_attached_tag"
                    line.attribution_review_required = False
                    line.attribution_review_reason = ""
                    line.attribution_confidence_history.append(
                        {
                            "resolver": "deterministic_attached_tag",
                            "model": "source_parser",
                            "decision": "resolved",
                            "speaker_id": res.resolved_speaker,
                            "confidence": 1.0,
                            "reason": res.reason,
                            "evidence": res.evidence_quote,
                        }
                    )
                    if prev_speaker == res.resolved_speaker:
                        confirmed_count += 1
                    else:
                        reattributed_count += 1
                        logger.info(
                            "[TieredAttribution] Speech tag overruled adjudication on %s (%s -> %s): %s",
                            res.line_id,
                            prev_speaker,
                            res.resolved_speaker,
                            res.reason,
                        )
            elif res.resolver_tier in ("local_qwen", "local_qwen_block") and res.resolved_speaker:
                local_resolved_count += 1
                if not dry_run:
                    prev_speaker = line.speaker
                    line.speaker = res.resolved_speaker
                    line.speaker_confidence = res.confidence
                    resolver_name = (
                        "local_qwen_block" if res.resolver_tier == "local_qwen_block" else "local_qwen_micro"
                    )
                    evidence_label = "block" if res.resolver_tier == "local_qwen_block" else "micro"
                    line.speaker_evidence = (
                        f"Tier 1 Qwen 27B {evidence_label}-adjudication: {res.evidence_quote} ({res.reason})"
                    )[:4000]
                    line.attribution_resolver = resolver_name
                    line.attribution_review_required = False
                    line.attribution_review_reason = ""
                    line.attribution_confidence_history.append(
                        {
                            "resolver": resolver_name,
                            "model": getattr(self.ollama, "model", DEFAULT_OLLAMA_MODEL),
                            "decision": "resolved",
                            "speaker_id": res.resolved_speaker,
                            "confidence": res.confidence,
                            "reason": res.reason,
                            "evidence": res.evidence_quote,
                        }
                    )
                    if prev_speaker == res.resolved_speaker:
                        confirmed_count += 1
                        logger.info(
                            "[TieredAttribution] Confirmed %s as %s (conf=%.2f, no longer flagged): %s",
                            res.line_id,
                            res.resolved_speaker,
                            res.confidence,
                            res.reason,
                        )
                    else:
                        reattributed_count += 1
                        logger.info(
                            "[TieredAttribution] Reattributed %s (%s -> %s, conf=%.2f): %s",
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
            # local_resolved == confirmed + reattributed. Kept as the total for
            # existing readers; the split is what answers "was attribution
            # actually wrong, or merely uncertain?"
            "confirmed": confirmed_count,
            "reattributed": reattributed_count,
            "escalated_to_tier2": escalated_count,
            # How often the source text had to overrule the model. A rising
            # number here means adjudication is drifting from the book.
            "tag_overruled": tag_overruled_count,
            "blocks_adjudicated": blocks_adjudicated_count,
            "block_fallbacks": block_fallbacks_count,
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
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("[TieredAttribution] Failed writing report to %s: %s", preview_path, exc)

        return report

    def _group_suspicious_into_blocks(
        self,
        suspicious_turns: list[SuspiciousTurn],
        chapter_map: dict[int, ScriptChapter],
    ) -> list[DialogueBlock]:
        """Group suspicious turns into dialogue blocks capped at max_suspicious_per_call."""
        turns_by_chapter: dict[int, list[SuspiciousTurn]] = {}
        for t in suspicious_turns:
            turns_by_chapter.setdefault(t.chapter_number, []).append(t)

        blocks: list[DialogueBlock] = []
        for ch_num, ch_turns in turns_by_chapter.items():
            chapter = chapter_map.get(ch_num)
            if not chapter:
                continue
            suspicious_by_id = {t.line_id: t for t in ch_turns}
            spoken_indices = [
                i
                for i, line in enumerate(chapter.lines)
                if line.line_id in suspicious_by_id or (_is_dialogue_line(line) and line.speaker != "narrator")
            ]
            if not spoken_indices:
                continue

            # Group consecutive spoken lines separated by <= 2 narrator lines
            raw_groups: list[list[int]] = []
            cur_group = [spoken_indices[0]]
            for idx in spoken_indices[1:]:
                gap = idx - cur_group[-1] - 1
                if gap <= 2:
                    cur_group.append(idx)
                else:
                    raw_groups.append(cur_group)
                    cur_group = [idx]
            if cur_group:
                raw_groups.append(cur_group)

            # Subdivide groups that exceed max_suspicious_per_call at widest narration gap
            def _split_group(
                indices: list[int],
                chapter: ScriptChapter = chapter,
                suspicious_by_id: dict[str, Any] = suspicious_by_id,
            ) -> list[list[int]]:
                susp_indices = [idx for idx in indices if chapter.lines[idx].line_id in suspicious_by_id]
                if len(susp_indices) <= self.max_suspicious_per_call:
                    return [indices]

                total_susp = len(susp_indices)
                best_split = None
                best_gap_score = (-1, -1, -999999)
                susp_so_far = 0

                for i in range(len(indices) - 1):
                    idx_cur = indices[i]
                    idx_nxt = indices[i + 1]
                    if chapter.lines[idx_cur].line_id in suspicious_by_id:
                        susp_so_far += 1

                    if 1 <= susp_so_far < total_susp:
                        gap_lines = idx_nxt - idx_cur - 1
                        gap_chars = sum(len(chapter.lines[k].text.strip()) for k in range(idx_cur + 1, idx_nxt))
                        balance = -abs(susp_so_far - total_susp / 2)
                        gap_score = (gap_lines, gap_chars, balance)
                        if gap_score > best_gap_score:
                            best_gap_score = gap_score
                            best_split = i

                if best_split is None:
                    best_split = len(indices) // 2

                left = indices[: best_split + 1]
                right = indices[best_split + 1 :]
                return _split_group(left, chapter, suspicious_by_id) + _split_group(right, chapter, suspicious_by_id)

            for raw_g in raw_groups:
                for sub_g in _split_group(raw_g):
                    sub_turns = [
                        suspicious_by_id[chapter.lines[idx].line_id]
                        for idx in sub_g
                        if chapter.lines[idx].line_id in suspicious_by_id
                    ]
                    if sub_turns:
                        blocks.append(
                            DialogueBlock(
                                chapter_number=ch_num,
                                start_line_idx=sub_g[0],
                                end_line_idx=sub_g[-1],
                                spoken_line_indices=sub_g,
                                suspicious_turns=sub_turns,
                            )
                        )
        return blocks

    def _is_block_targeted(
        self,
        block: DialogueBlock,
        chapter: ScriptChapter | None,
    ) -> bool:
        """Check if a dialogue block qualifies for block-level adjudication."""
        if not self.only_unconfirmed_runs:
            return True
        if not chapter:
            return False
        unconfirmed_ids = self._unconfirmed_run_ids_for(chapter)
        return any((chapter.lines[idx].line_id in unconfirmed_ids) for idx in block.spoken_line_indices)

    def _unconfirmed_run_ids_for(self, chapter: ScriptChapter) -> set[str]:
        """Per-chapter cache: the scan is whole-chapter, the callers are per-block."""
        cache = getattr(self, "_unconfirmed_run_cache", None)
        if cache is None:
            cache = {}
            self._unconfirmed_run_cache = cache
        key = id(chapter)
        if key not in cache:
            cache[key] = _find_unconfirmed_run_line_ids(chapter, self.registry)
        return cache[key]

    def _adjudicate_block_tier1(
        self,
        turns: list[SuspiciousTurn],
        chapter: ScriptChapter | None,
    ) -> list[AdjudicationResult]:
        """Run multi-turn joint Qwen micro-prompt for a conversational block."""
        if not chapter or not turns:
            raise ValueError("Chapter and turns must be provided for block adjudication")

        expected_ids = {t.line_id for t in turns}
        lines = chapter.lines
        line_idx_by_id = {l.line_id: i for i, l in enumerate(lines)}

        turn_indices = [line_idx_by_id[t.line_id] for t in turns if t.line_id in line_idx_by_id]
        if not turn_indices:
            raise ValueError("None of the turns were found in chapter")

        start_idx = min(turn_indices)
        end_idx = max(turn_indices)
        w_start = max(0, start_idx - 2)
        w_end = min(len(lines), end_idx + 3)

        block_lines = lines[w_start:w_end]

        active_ids: set[str] = set()
        for bline in block_lines:
            if bline.speaker and bline.speaker != "narrator":
                active_ids.add(bline.speaker)
        for t in turns:
            if t.current_speaker and t.current_speaker != "narrator":
                active_ids.add(t.current_speaker)

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

        fixed_anchors: list[str] = []
        tag_constraints: dict[str, tuple[str | None, Gender | None, str]] = {}
        for pos in range(w_start, w_end):
            cur_line = lines[pos]
            if pos + 1 < len(lines):
                nxt_line = lines[pos + 1]
                if nxt_line.speaker == "narrator":
                    tag_text = str(nxt_line.text or "").strip()
                    lead = next((c for c in tag_text if c.isalpha()), "")
                    if lead and lead.islower():
                        exact, kind, gender = ScriptGenerator._dialogue_tag_evidence(tag_text, self.registry)
                        if (
                            gender is not None
                            and kind == "pronoun_gender"
                            and not (_HE_SPEECH_TAG.search(tag_text) or _SHE_SPEECH_TAG.search(tag_text))
                        ):
                            gender = None
                        if exact or gender:
                            tag_constraints[cur_line.line_id] = (exact, gender, tag_text)
                            if exact:
                                fixed_anchors.append(
                                    f"- Line [{cur_line.line_id}]: speaker must be '{exact}' (speech tag: '{tag_text}')"
                                )
                            elif gender:
                                fixed_anchors.append(
                                    f"- Line [{cur_line.line_id}]: speaker must be {gender.value} (speech tag: '{tag_text}')"
                                )

        anchors_text = "\n".join(fixed_anchors) if fixed_anchors else "- None in this block."

        formatted_lines: list[str] = []
        for pos in range(w_start, w_end):
            cur_line = lines[pos]
            if cur_line.line_id in expected_ids:
                prefix = f">>> [TARGET: {cur_line.line_id}]"
                formatted_lines.append(f"{prefix} [{cur_line.line_id}] {cur_line.speaker}: {cur_line.text}")
            else:
                prefix = "   "
                support = _label_support(lines, pos, self.registry)
                formatted_lines.append(f"{prefix} [{cur_line.line_id}] {cur_line.speaker}: {cur_line.text}{support}")
        formatted_context = "\n".join(formatted_lines)

        target_lines_desc = "\n".join(f"- [{t.line_id}] (assigned to {t.current_speaker}): {t.text}" for t in turns)

        prompt = (
            "You are an audiobook dialogue attribution expert. Determine the correct speaker for "
            "all TARGET lines in this conversational block based on the surrounding dialogue exchange.\n\n"
            "CHARACTERS IN THIS SCENE:\n"
            f"{json.dumps(scene_characters, ensure_ascii=False, indent=2)}\n\n"
            "FIXED ANCHORS (established facts from author speech tags; MUST NOT be contradicted):\n"
            f"{anchors_text}\n\n"
            "TARGET LINES TO RESOLVE (you MUST provide a decision for EVERY line below):\n"
            f"{target_lines_desc}\n\n"
            "CONVERSATION BLOCK (TARGET lines to resolve are marked with >>> [TARGET: line_id]):\n"
            f"{formatted_context}\n\n"
            "RULES:\n"
            "1. Maintain narrative and conversational coherence across the entire block: who owns what, who is speaking to whom, and question-and-answer flow.\n"
            "2. In two-party dialogue without explicit speech tags, turns usually ALTERNATE between speakers -- but a label marked [unverified] is a previous guess, not evidence. Never flip a line merely to preserve alternation against [unverified] neighbours. A label marked [confirmed by the speech tag] is the book stating who spoke, and outranks every other consideration.\n"
            "3. Respect all FIXED ANCHORS absolutely. Any proposed attribution that contradicts a fixed anchor tag or its gender is strictly invalid.\n"
            "4. If a line addresses someone by name (e.g., '..., Dusk'), the SPEAKER is the OTHER character talking TO that person.\n"
            "5. Match pronouns in action beats: 'He frowned. \"Quote\"' means a MALE character speaks.\n"
            "6. Return ONLY a JSON object mapping each target line_id to an object with this exact schema:\n"
            "{\n"
            '  "<line_id>": {\n'
            '    "speaker_id": "string",\n'
            '    "confidence": 0.0-1.0,\n'
            '    "reason": "brief explanation",\n'
            '    "evidence_quote": "verbatim quote or phrase from context proving attribution"\n'
            "  }\n"
            "}\n"
            f"Required keys: {sorted(expected_ids)}\n"
            "Do not include any lines other than the specified TARGET lines. Every TARGET line must be present."
        )

        token_budget = max(getattr(self.ollama, "max_output_tokens", 8192), 350 * len(turns) + 500)

        parsed_map: dict[str, dict[str, Any]] | None = None
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            generate_kwargs: dict[str, Any] = {
                "temperature": self.ollama_temperature,
                "format": "json",
            }
            if hasattr(self.ollama, "max_output_tokens"):
                generate_kwargs["max_output_tokens"] = token_budget

            try:
                raw_response = self.ollama.generate(prompt, **generate_kwargs)
            except TypeError:
                generate_kwargs.pop("max_output_tokens", None)
                raw_response = self.ollama.generate(prompt, **generate_kwargs)

            try:
                parsed = _extract_block_json(raw_response, expected_ids)
            except ValueError as ve:
                if attempt < max_attempts and "missing target line_ids" in str(ve):
                    logger.warning(
                        "[TieredAttribution] Block response missing target lines on attempt %d: %s. Retrying...",
                        attempt,
                        ve,
                    )
                    prompt = (
                        f"{prompt}\n\n"
                        f"CRITICAL CORRECTION REQUIRED:\n"
                        f"Your previous response omitted required TARGET lines:\n"
                        f"{ve}\n"
                        f"You MUST include an entry for EVERY line ID in: {sorted(expected_ids)}. "
                        f"Return the complete JSON mapping for all TARGET lines."
                    )
                    continue
                raise

            # Check hard constraints
            violations: list[str] = []
            for t in turns:
                entry = parsed[t.line_id]
                raw_spk = str(entry.get("speaker_id") or "")
                cid, _ = _resolve_speaker_alias(raw_spk, self.registry)
                if t.line_id in tag_constraints:
                    req_named, req_gender, req_tag = tag_constraints[t.line_id]
                    if req_named and cid and cid != req_named:
                        violations.append(
                            f"Line {t.line_id} assigned to '{cid}' contradicts tag naming '{req_named}' ('{req_tag}')"
                        )
                    elif req_gender and cid:
                        cand = self.registry.characters.get(cid)
                        if cand and cand.gender in (Gender.MALE, Gender.FEMALE) and cand.gender != req_gender:
                            violations.append(
                                f"Line {t.line_id} assigned to '{cid}' ({cand.gender.value}) contradicts tag gender {req_gender.value} ('{req_tag}')"
                            )

            if violations:
                if attempt < max_attempts:
                    logger.warning(
                        "[TieredAttribution] Block response violated speech tag constraint: %s. Retrying...",
                        violations[0],
                    )
                    prompt = (
                        f"{prompt}\n\n"
                        f"CRITICAL CORRECTION REQUIRED:\n"
                        f"Your previous response violated the following fixed speech tag constraint:\n"
                        f"- {violations[0]}\n"
                        f"You MUST assign that line to the speaker or gender required by the author's tag. "
                        f"Return the complete JSON mapping for all TARGET lines."
                    )
                    continue
                raise ValueError(f"Block adjudication violated hard constraint after retry: {violations[0]}")

            parsed_map = parsed
            break

        if parsed_map is None:
            raise ValueError("Block adjudication failed to produce a valid response")

        results: list[AdjudicationResult] = []
        for turn in turns:
            entry = parsed_map[turn.line_id]
            raw_speaker = str(entry.get("speaker_id") or "")
            confidence = float(entry.get("confidence") or 0.0)
            reason = str(entry.get("reason") or "")
            evidence_quote = str(entry.get("evidence_quote") or "")

            resolved_speaker, alias_detail = _resolve_speaker_alias(raw_speaker, self.registry)
            alias_passed = bool(resolved_speaker is not None)

            quote_passed, quote_detail = _fuzzy_quote_in_context(evidence_quote, turn.scene_text)
            if not quote_passed and confidence > 0.80:
                confidence = max(0.0, confidence - 0.15)

            gender_passed, gender_detail = (
                _check_gender_pronoun_consistency(
                    resolved_speaker or raw_speaker,
                    f"{reason} {evidence_quote}",
                    self.registry,
                )
                if alias_passed
                else (True, "skipped_due_to_unresolved_alias")
            )

            tag_named, tag_gender, tag_text = _attached_tag_evidence(turn, self.registry)
            tag_status: dict[str, Any] = {
                "passed": True,
                "detail": "tag_consistent" if tag_text else "no_attached_tag",
            }
            if alias_passed and resolved_speaker and tag_text and tag_named is None and tag_gender is not None:
                candidate = self.registry.characters.get(resolved_speaker)
                if candidate and candidate.gender in (Gender.MALE, Gender.FEMALE) and candidate.gender != tag_gender:
                    gender_passed = False
                    gender_detail = (
                        f"Attached speech tag identifies a {tag_gender.value} speaker; "
                        f"'{resolved_speaker}' is {candidate.gender.value}"
                    )
                    tag_status = {"passed": False, "detail": gender_detail}

            guardrail_status = {
                "alias_resolution": {"passed": alias_passed, "detail": alias_detail},
                "quote_in_context": {"passed": quote_passed, "detail": quote_detail},
                "gender_pronoun": {"passed": gender_passed, "detail": gender_detail},
                "attached_tag": tag_status,
            }

            if alias_passed and resolved_speaker and tag_named and tag_named != resolved_speaker:
                guardrail_status["attached_tag"] = {
                    "passed": False,
                    "detail": f"tag names '{tag_named}', adjudication said '{resolved_speaker}'",
                }
                results.append(
                    AdjudicationResult(
                        line_id=turn.line_id,
                        chapter_number=turn.chapter_number,
                        text=turn.text,
                        original_speaker=turn.current_speaker,
                        resolved_speaker=tag_named,
                        resolver_tier="deterministic_tag",
                        confidence=1.0,
                        reason=(
                            f"Attached speech tag names '{tag_named}'; block-adjudication "
                            f"proposed '{resolved_speaker}' and was overruled"
                        ),
                        evidence_quote=tag_text,
                        guardrail_results=guardrail_status,
                    )
                )
                continue

            all_passed = alias_passed and gender_passed and confidence >= self.local_auto_accept
            if all_passed and resolved_speaker:
                results.append(
                    AdjudicationResult(
                        line_id=turn.line_id,
                        chapter_number=turn.chapter_number,
                        text=turn.text,
                        original_speaker=turn.current_speaker,
                        resolved_speaker=resolved_speaker,
                        resolver_tier="local_qwen_block",
                        confidence=confidence,
                        reason=reason,
                        evidence_quote=evidence_quote,
                        guardrail_results=guardrail_status,
                    )
                )
            else:
                escalate_reasons = []
                if not alias_passed:
                    escalate_reasons.append(alias_detail)
                if not gender_passed:
                    escalate_reasons.append(gender_detail)
                if confidence < self.local_auto_accept:
                    escalate_reasons.append(f"Confidence {confidence:.2f} < threshold {self.local_auto_accept:.2f}")
                full_reason = "; ".join(escalate_reasons) or reason
                results.append(
                    AdjudicationResult(
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
                )

        return results

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

        # Format surrounding lines, each marked with what backs its label. Sent
        # flat, a neighbour's guess is indistinguishable from the book stating
        # the speaker, and Rule 1 below then propagates the guesses.
        context_lines_formatted = []
        window = list(turn.surrounding_lines or [])
        for position, neighbor in enumerate(window):
            prefix = ">>> [TARGET]" if neighbor.get("is_target") else "   "
            support = "" if neighbor.get("is_target") else _label_support(window, position, self.registry)
            context_lines_formatted.append(
                f"{prefix} [{neighbor['line_id']}] {neighbor['speaker']}: {neighbor['text']}{support}"
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
            "1. In two-party dialogue without explicit speech tags, turns usually ALTERNATE "
            "between speakers -- but a label marked [unverified] is a previous guess, not "
            "evidence. Never flip the TARGET merely to preserve alternation against "
            "[unverified] neighbours; if they are the only reason to change it, they are the "
            "more likely thing to be wrong. A label marked [confirmed by the speech tag] is "
            "the book stating who spoke, and outranks every other consideration.\n"
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
        except (OllamaError, OSError, ValueError, TypeError, KeyError) as exc:
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

        # Guardrail 2b: the book's own speech tag. The check above reads the
        # model's prose, which is the wrong text -- see this module's history on
        # ch11_0149. This one reads the narration the author attached.
        tag_named, tag_gender, tag_text = _attached_tag_evidence(turn, self.registry)
        tag_status: dict[str, Any] = {
            "passed": True,
            "detail": "tag_consistent" if tag_text else "no_attached_tag",
        }
        if alias_passed and resolved_speaker and tag_text and tag_named is None and tag_gender is not None:
            candidate = self.registry.characters.get(resolved_speaker)
            if candidate and candidate.gender in (Gender.MALE, Gender.FEMALE) and candidate.gender != tag_gender:
                # The tag cannot say who spoke, but it is decisive about who did
                # not. Refuse rather than guess.
                gender_passed = False
                gender_detail = (
                    f"Attached speech tag identifies a {tag_gender.value} speaker; "
                    f"'{resolved_speaker}' is {candidate.gender.value}"
                )
                tag_status = {"passed": False, "detail": gender_detail}

        guardrail_status = {
            "alias_resolution": {"passed": alias_passed, "detail": alias_detail},
            "quote_in_context": {"passed": quote_passed, "detail": quote_detail},
            "gender_pronoun": {"passed": gender_passed, "detail": gender_detail},
            "attached_tag": tag_status,
        }

        if alias_passed and resolved_speaker and tag_named and tag_named != resolved_speaker:
            # The author named the speaker. That is not evidence to weigh, it is
            # the answer, and no confidence score outranks it.
            guardrail_status["attached_tag"] = {
                "passed": False,
                "detail": f"tag names '{tag_named}', adjudication said '{resolved_speaker}'",
            }
            return AdjudicationResult(
                line_id=turn.line_id,
                chapter_number=turn.chapter_number,
                text=turn.text,
                original_speaker=turn.current_speaker,
                resolved_speaker=tag_named,
                resolver_tier="deterministic_tag",
                confidence=1.0,
                reason=(
                    f"Attached speech tag names '{tag_named}'; micro-adjudication "
                    f"proposed '{resolved_speaker}' and was overruled"
                ),
                evidence_quote=tag_text,
                guardrail_results=guardrail_status,
            )

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
                    if (
                        cur_res
                        and cur_res.resolver_tier in ("local_qwen", "local_qwen_block")
                        and cur_res.resolved_speaker
                    )
                    else cur.speaker
                )
                nxt_speaker = (
                    nxt_res.resolved_speaker
                    if (
                        nxt_res
                        and nxt_res.resolver_tier in ("local_qwen", "local_qwen_block")
                        and nxt_res.resolved_speaker
                    )
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
                        if cur_res and cur_res.resolver_tier in ("local_qwen", "local_qwen_block"):
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
                        if nxt_res and nxt_res.resolver_tier in ("local_qwen", "local_qwen_block"):
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
