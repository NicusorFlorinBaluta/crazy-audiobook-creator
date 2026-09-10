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

from brain.director.attribution_detector import SuspiciousTurn, rebuild_turn_with_context
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


# Verbs that report speech. A narrator sentence built on one of these is a
# speech tag; one built on a reaction verb ("Dahlia laughed at that.") is not,
# and reading a reaction as a tag is how a bystander ends up owning the line.
_SPEECH_TAG_VERBS = (
    "said|says|asked|asks|replied|replies|answered|answers|stated|states|added|adds|"
    "muttered|mutters|growled|growls|whispered|whispers|shouted|shouts|called|calls|"
    "snarled|snarls|breathed|breathes|offered|offers|insisted|insists|countered|counters|"
    # `continued on` is walking, not speaking: "Dusk continued on, remaining
    # methodical." was read as a tag and pulled a name out of the narration
    # three sentences later. `went on` stays, because that idiom *is* speech.
    "agreed|agrees|admitted|admits|observed|observes|remarked|remarks|"
    r"continued(?!\s+on)|continues(?!\s+on)|"
    "interrupted|interrupts|corrected|corrects|protested|protests|explained|explains|"
    "murmured|murmurs|repeated|repeats|announced|announces|declared|declares|"
    "returned|returns|finished|finishes|went on|goes on"
)

# "Dahlia said no more and let him go." is the shape of a *refusal* to speak.
# It matches "<Name> said" and is not a tag for the quote above it.
_TAG_NEGATION = re.compile(
    rf"\b(?:{_SPEECH_TAG_VERBS})\s+(?:no|nothing|not|never|none)\b",
    re.IGNORECASE,
)

# "<Name> <optional adverb> <speech verb>" at the very start of the sentence.
_CAPITAL_LED_TAG = re.compile(
    rf"^[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*)?\s+(?:\w+ly\s+)?(?:{_SPEECH_TAG_VERBS})\b"
)

#: Tiers that mean "the local model settled this line". Every consumer that
#: asks "was this resolved locally?" must accept all of them, so the set lives
#: here rather than as a literal at each of the five call sites.
_LOCAL_RESOLVER_TIERS = ("local_qwen", "local_qwen_wide")

#: What each of those writes to `line.attribution_resolver`, so a line's stored
#: provenance says which attempt actually settled it. `local_qwen_block` is
#: still read from scripts written before 2026-09-10; nothing writes it now.
_RESOLVER_PROVENANCE = {
    "local_qwen": "local_qwen_micro",
    "local_qwen_wide": "local_qwen_wide",
}
_EVIDENCE_LABEL = {
    "local_qwen": "micro",
    "local_qwen_wide": "wide-context",
}

#: Radii for the wide-context retry, in lines either side of the target.
#: Roughly four times the detector's default. Prefill is cheap relative to
#: decode, so the extra context is not what makes a retried line slower -- the
#: second call is. See `_retry_with_wide_context` for the measured cost.
WIDE_RETRY_WINDOW_RADIUS = 20
WIDE_RETRY_SCENE_RADIUS = 30


def _reads_as_attached_tag(tag: str) -> bool:
    """Is this narrator line the author naming who just spoke?

    Two shapes qualify.

    A **lower-case** first letter means the line grammatically continues the
    quoted sentence -- `"Get out," he snarled.` -- so it is a tag by
    construction, whatever its verb.

    A **capital** first letter starts a new sentence, which may be a reaction
    rather than a tag. Those qualify only when the sentence opens with a name
    and a speech verb. Measured across two books (2026-09-10,
    `scripts/audit_capital_led_speech_tags.py`), that distinction holds:

    ```
                        speech-verb   reaction-verb
      trailing              391             6
      leading                 6            12      <- names the NEXT speaker
      both-same             986             4
      unparsed              194           415
    ```

    Of 1,390 capital-led speech-verb tags that parse to a name, 6 name the
    following speaker rather than the preceding one -- 0.4%. Reaction verbs are
    90-96% unparseable and the few that parse lean the wrong way, so they stay
    excluded.

    Before this, only the lower-case shape counted, which saw 490 of roughly
    1,064 tags in `the-finest-edge-of-twilight-book` -- slightly under half.
    """
    if not tag:
        return False
    lead = next((char for char in tag if char.isalpha()), "")
    if not lead:
        return False
    if lead.islower():
        return True
    if _TAG_NEGATION.search(tag):
        return False
    return bool(_CAPITAL_LED_TAG.match(tag.strip()))


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
    if not _reads_as_attached_tag(tag):
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
    if not _reads_as_attached_tag(tag):
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
        wide_context_retry: bool = True,
        wide_window_radius: int = WIDE_RETRY_WINDOW_RADIUS,
        wide_scene_radius: int = WIDE_RETRY_SCENE_RADIUS,
    ):
        self.ollama = ollama
        self.external_validator = external_validator
        self.registry = registry
        self.local_auto_accept = local_auto_accept
        self.gemini_auto_accept = gemini_auto_accept
        self.ollama_temperature = ollama_temperature
        self.wide_context_retry = wide_context_retry
        self.wide_window_radius = max(1, int(wide_window_radius))
        self.wide_scene_radius = max(1, int(wide_scene_radius))

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
            elif res.resolver_tier in _LOCAL_RESOLVER_TIERS and res.resolved_speaker:
                local_resolved_count += 1
                if not dry_run:
                    prev_speaker = line.speaker
                    line.speaker = res.resolved_speaker
                    line.speaker_confidence = res.confidence
                    resolver_name = _RESOLVER_PROVENANCE.get(res.resolver_tier, "local_qwen_micro")
                    evidence_label = _EVIDENCE_LABEL.get(res.resolver_tier, "micro")
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
            # Lines the narrow window escalated and the wide retry settled --
            # each one is a Gemini call not made. Watch it against
            # `escalated_to_tier2`: if the cascade stops paying, this falls
            # toward zero while escalations hold steady.
            "wide_context_resolved": sum(1 for r in results if r.resolver_tier == "local_qwen_wide"),
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

    def _adjudicate_turn_tier1(
        self,
        turn: SuspiciousTurn,
        chapter: ScriptChapter | None,
        *,
        allow_wide_retry: bool = True,
    ) -> AdjudicationResult:
        """Run single-turn Qwen micro-prompt and apply guardrails 1-3.

        `allow_wide_retry=False` is the recursion guard for the wide-context
        second attempt; see `_retry_with_wide_context`.
        """
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

        escalation = AdjudicationResult(
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
        if allow_wide_retry and self.wide_context_retry and chapter is not None:
            widened = self._retry_with_wide_context(turn, chapter, escalation)
            if widened is not None:
                return widened
        return escalation

    def _retry_with_wide_context(
        self,
        turn: SuspiciousTurn,
        chapter: ScriptChapter,
        narrow: AdjudicationResult,
    ) -> AdjudicationResult | None:
        """Second look at a line the narrow window could not settle.

        Only lines already bound for Gemini get here, so the choice is not
        "one local call or none" but "one local call or a paid remote one".

        Measured on the 16 sub-threshold lines of `the-finest-edge-of-twilight`
        (`scripts/experiment_attribution_context_ab.py`, three runs each):

        ```
                agrees_with_stored  stable_3of3  mean_conf  above_0.85  wall
        narrow        16/16            16/16       0.917      13/16     5.3s
        wide          15/16            16/16       0.954      16/16     6.1s
        ```

        Two things that reading settles. Context buys **confidence, not
        stability** -- both widths were unanimous across all three runs on
        every line, so the stability the constrained-choice tier gained came
        from closing the question, not from the wider window. And the single
        disagreement, `ch11_0222`, was wide catching a real error: the narrator
        line just after it reads "That had Effron's hair on the back of his
        neck standing up. Something about the timbre of Dahlia...", which the
        narrow window cut off.

        So this runs as a **cascade, not a default width**, and the cost of
        that is small. Over an unbiased 150-line sample of the same book's
        suspicious turns, run through this code path:

        ```
        retried (narrow escalated)  4 (2.7%)   settled by the retry  4
        mean 5.0s with no retry, 12.9s when retried   pass wall +3.4%
        ```

        Note what the +3.4% is made of. The extra context is nearly free --
        prefill is cheap next to decode -- so what a retried line pays for is
        the *second call*, not its size. Widening every line instead would cost
        the same per call and buy nothing on the 97% that are already confident.

        A single run, deliberately: both widths were 3-of-3 stable, so repeats
        measure nothing here. Unanimity is worth paying for where the model is
        known to waver (the constrained-choice tier), not here.

        Returns `None` when the wide attempt also fails, and the caller keeps
        the narrow escalation untouched. The retry can only ever turn an
        escalation into a local resolution; it can never change a line the
        narrow pass already decided, nor make an escalation worse.
        """
        wide_turn = rebuild_turn_with_context(
            turn,
            chapter,
            window_radius=self.wide_window_radius,
            scene_radius=self.wide_scene_radius,
        )
        if wide_turn is None or len(wide_turn.surrounding_lines) <= len(turn.surrounding_lines):
            # Nothing more to show it -- a short chapter, or the line is gone.
            return None

        try:
            result = self._adjudicate_turn_tier1(wide_turn, chapter, allow_wide_retry=False)
        except Exception as exc:  # noqa: BLE001 - a retry must never break the pass
            logger.warning("[TieredAttribution] Wide-context retry failed on %s: %s", turn.line_id, exc)
            return None

        if result.resolver_tier not in _LOCAL_RESOLVER_TIERS or not result.resolved_speaker:
            logger.debug(
                "[TieredAttribution] Wide-context retry did not settle %s either (%s)",
                turn.line_id,
                result.reason,
            )
            return None

        logger.info(
            "[TieredAttribution] Wide context settled %s as %s (conf %.2f -> %.2f): %s",
            turn.line_id,
            result.resolved_speaker,
            narrow.confidence,
            result.confidence,
            result.reason,
        )
        return AdjudicationResult(
            line_id=result.line_id,
            chapter_number=result.chapter_number,
            text=result.text,
            original_speaker=result.original_speaker,
            resolved_speaker=result.resolved_speaker,
            resolver_tier="local_qwen_wide",
            confidence=result.confidence,
            reason=(
                f"{result.reason} [resolved on retry with +/-{self.wide_window_radius} lines of "
                f"context, after the narrow window escalated: {narrow.reason}]"
            ),
            evidence_quote=result.evidence_quote,
            guardrail_results=result.guardrail_results,
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
                        and cur_res.resolver_tier in _LOCAL_RESOLVER_TIERS
                        and cur_res.resolved_speaker
                    )
                    else cur.speaker
                )
                nxt_speaker = (
                    nxt_res.resolved_speaker
                    if (
                        nxt_res
                        and nxt_res.resolver_tier in _LOCAL_RESOLVER_TIERS
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
                        if cur_res and cur_res.resolver_tier in _LOCAL_RESOLVER_TIERS:
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
                        if nxt_res and nxt_res.resolver_tier in _LOCAL_RESOLVER_TIERS:
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
