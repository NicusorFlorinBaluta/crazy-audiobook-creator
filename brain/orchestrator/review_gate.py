"""Typed review aggregation and pre-master release gating."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from shared.pronunciation import build_pronunciation_inventory

RESOLVED_SEGMENT_DISPOSITIONS = {"acceptable", "regenerate"}
RESOLVED_EXTRACTION_DISPOSITIONS = {"include", "exclude", "reference"}
RESOLVED_ATTRIBUTION_DISPOSITIONS = {"accepted", "overridden", "approved", "fixed", "acceptable"}


@dataclass(frozen=True)
class ReviewItem:
    category: str
    item_id: str
    title: str
    reason: str
    confidence: float | None = None
    disposition: str = "unreviewed"
    blocking: bool = False
    chapter_number: int | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReviewGate:
    items: tuple[ReviewItem, ...]

    @property
    def blocking_items(self) -> tuple[ReviewItem, ...]:
        return tuple(item for item in self.items if item.blocking)

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.category] = counts.get(item.category, 0) + 1
        return {
            "items": [item.to_dict() for item in self.items],
            "total_count": len(self.items),
            "blocking_count": len(self.blocking_items),
            "counts": counts,
            "release_ready": not self.blocking_items,
        }


from datetime import UTC

from shared.cache import cache_service


def _cap_decision_trail(
    trail: list[Any] | None,
    max_entries: int = 10,
    max_str_len: int = 500,
) -> list[dict[str, Any]]:
    """Cap decision trail to the last N entries and truncate verbose strings."""
    if not trail or not isinstance(trail, list):
        return []
    sliced = trail[-max_entries:]
    result: list[dict[str, Any]] = []
    for entry in sliced:
        if isinstance(entry, dict):
            cleaned: dict[str, Any] = {}
            for k, v in entry.items():
                if isinstance(v, str) and len(v) > max_str_len:
                    cleaned[k] = v[:max_str_len] + "..."
                else:
                    cleaned[k] = v
            result.append(cleaned)
        elif isinstance(entry, str):
            result.append({"reason": entry[:max_str_len]})
    return result


def _get_script_review_data(project_dir: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    script_dir = project_dir / "script"
    if not script_dir.is_dir():
        return {}, []

    try:
        file_mtimes = {
            p.name: p.stat().st_mtime
            for p in script_dir.glob("chapter_*.json")
            if re.fullmatch(r"chapter_\d{3,}\.json", p.name)
        }
    except OSError:
        file_mtimes = {}

    cache_key = f"script_review:{project_dir.resolve()}"
    cached = cache_service.get(cache_key)
    if cached and isinstance(cached, dict) and cached.get("file_mtimes") == file_mtimes:
        return cached.get("lines", {}), cached.get("attributions", [])

    script_lines_by_id: dict[str, dict[str, Any]] = {}
    attribution_lines: list[dict[str, Any]] = []

    for chapter_path in sorted(script_dir.glob("chapter_*.json")):
        if not re.fullmatch(r"chapter_\d{3,}\.json", chapter_path.name):
            continue
        try:
            chapter = json.loads(chapter_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        chapter_number = _int_or_none(chapter.get("chapter_number"))
        for line in chapter.get("lines", []):
            line_id = str(line.get("line_id") or line.get("id") or "unknown")
            line_data = {**line, "chapter_number": chapter_number}
            script_lines_by_id[line_id] = line_data
            if line.get("attribution_review_required"):
                attribution_lines.append(line_data)

    cache_service.set(
        cache_key,
        {
            "file_mtimes": file_mtimes,
            "lines": script_lines_by_id,
            "attributions": attribution_lines,
        },
        ttl_seconds=1800,
    )
    return script_lines_by_id, attribution_lines


def collect_review_gate(project_id: str, project_dir: Path, job_queue: Any) -> ReviewGate:
    """Collect all known review work without exposing source text by default."""
    items: list[ReviewItem] = []
    try:
        state = job_queue.get_job(project_id)
        if "generated_chapters" in state or "mastered_chapters" in state:
            current_audio_chapters: set[int] | None = set(state.get("generated_chapters", [])).union(
                state.get("mastered_chapters", [])
            )
        else:
            current_audio_chapters = None
    except (AttributeError, KeyError, TypeError):
        current_audio_chapters = None
    review_rows = job_queue.get_review_items(project_id)
    persisted = {(row["item_type"], row["item_id"]): row for row in review_rows}
    active_audio_review_ids: set[str] = set()
    if current_audio_chapters is not None:
        stage = str(state.get("active_stage") or state.get("status") or "")
        if stage == "waiting_for_review":
            blocking = {str(item_id) for item_id in state.get("review_blocking_item_ids", [])}
            persisted_segments = {str(row.get("item_id")) for row in review_rows if row.get("item_type") == "segment"}
            active_audio_review_ids = blocking.intersection(persisted_segments)

    extraction_path = project_dir / "extraction_audit.json"
    if extraction_path.is_file():
        try:
            extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
            for section in extraction.get("sections", []):
                if not section.get("review_required"):
                    continue
                item_id = str(section.get("item_id") or "unknown")
                review = persisted.get(("extraction", item_id), {})
                disposition = str(review.get("disposition", "unreviewed"))
                items.append(
                    ReviewItem(
                        category="extraction",
                        item_id=item_id,
                        title=f"Book section: {section.get('title') or section.get('href') or item_id}",
                        reason=str(section.get("reason") or "Section classification is ambiguous."),
                        confidence=_float_or_none(section.get("confidence")),
                        disposition=disposition,
                        blocking=disposition not in RESOLVED_EXTRACTION_DISPOSITIONS,
                        details={
                            "decision": section.get("decision"),
                            "word_count": section.get("word_count", 0),
                            "href": section.get("href", ""),
                            "semantics": section.get("semantics", []),
                            "decision_trail": section.get("decision_trail", []),
                        },
                    )
                )
        except (OSError, ValueError, TypeError):
            items.append(
                ReviewItem(
                    category="extraction",
                    item_id="audit-invalid",
                    title="Extraction audit unavailable",
                    reason="The extraction audit could not be read safely.",
                    blocking=True,
                )
            )

    character_audit_path = project_dir / "character_augmentation_audit.json"
    if character_audit_path.is_file():
        try:
            character_audit = json.loads(character_audit_path.read_text(encoding="utf-8"))
            for candidate in character_audit.get("review", []):
                character_id = str(candidate.get("character_id") or "unknown")
                items.append(
                    ReviewItem(
                        category="character",
                        item_id=character_id,
                        title=f"Character profile: {character_id}",
                        reason=str(candidate.get("reason") or "External character enrichment abstained."),
                        confidence=_float_or_none(candidate.get("confidence")),
                        blocking=False,
                        details={
                            "provider": candidate.get("provider", ""),
                            "model": candidate.get("model", ""),
                            "grounded": bool(candidate.get("grounded", False)),
                            "gender_conflict": bool(candidate.get("gender_conflict", False)),
                            "manual_action": ("Review or edit this profile in Voice Review before approval."),
                        },
                    )
                )
        except (OSError, ValueError, TypeError):
            items.append(
                ReviewItem(
                    category="character",
                    item_id="audit-invalid",
                    title="Character augmentation audit unavailable",
                    reason="The character augmentation audit could not be read safely.",
                    blocking=False,
                )
            )

    script_lines_by_id, attribution_lines = _get_script_review_data(project_dir)
    for line in attribution_lines:
        line_id = str(line.get("line_id") or line.get("id") or "unknown")
        chapter_number = line.get("chapter_number")
        review = persisted.get(("attribution", line_id), {})
        disposition = str(review.get("disposition", "unreviewed"))
        items.append(
            ReviewItem(
                category="attribution",
                item_id=line_id,
                title=f"Speaker attribution {line_id}",
                reason=str(
                    line.get("attribution_review_reason")
                    or line.get("speaker_evidence")
                    or "Speaker could not be resolved safely."
                ),
                confidence=_float_or_none(line.get("speaker_confidence")),
                disposition=disposition,
                blocking=disposition not in RESOLVED_ATTRIBUTION_DISPOSITIONS,
                chapter_number=chapter_number,
                details={
                    "speaker": line.get("speaker"),
                    "source_excerpt": line.get("text"),
                    "decision_trail": _cap_decision_trail(line.get("attribution_confidence_history")),
                },
            )
        )

    quality_by_line: dict[str, dict[str, Any]] = {}
    for row in job_queue.get_quality_report(project_id):
        if (
            current_audio_chapters is not None
            and row.get("chapter_number") not in current_audio_chapters
            and str(row.get("line_id")) not in active_audio_review_ids
        ):
            continue
        if row.get("details", {}).get("selected"):
            quality_by_line[row["line_id"]] = row
    for (item_type, item_id), review in persisted.items():
        if item_type != "segment":
            continue
        quality = quality_by_line.get(item_id, {})
        if current_audio_chapters is not None and not quality:
            continue
        details = quality.get("details", {})
        disposition = review.get("disposition", "unreviewed")
        script_line = script_lines_by_id.get(item_id, {})
        # Only segments that failed hard deterministic audio gates or were flagged are blocking.
        # Soft warnings and accepted audio with advisory notes are non-blocking.
        is_hard_failure = not bool(details.get("passed_hard_gates", True)) or quality.get("status") in {
            "fail",
            "flagged",
        }
        blocking = is_hard_failure and (disposition not in RESOLVED_SEGMENT_DISPOSITIONS)

        items.append(
            ReviewItem(
                category="audio",
                item_id=item_id,
                title=f"Audio segment {item_id}",
                reason=str(
                    details.get("manual_review_reason") or review.get("note") or "Automatic validators abstained."
                ),
                confidence=_float_or_none(details.get("validation_confidence")),
                disposition=disposition,
                blocking=blocking,
                chapter_number=_int_or_none(quality.get("chapter_number")) or script_line.get("chapter_number"),
                details={
                    "provider": details.get("external_validation_provider", ""),
                    "model": details.get("external_validation_model", ""),
                    "decision": details.get("external_validation_decision", ""),
                    "decision_trail": details.get("external_validation_history", []),
                    "audio_url": f"api/projects/{project_id}/segments/{item_id}/audio",
                    "text": script_line.get("text", ""),
                    "speaker": script_line.get("speaker", ""),
                },
            )
        )

    trend_path = project_dir / "long_form_audio_quality.json"
    if trend_path.is_file():
        try:
            trend_report = json.loads(trend_path.read_text(encoding="utf-8"))
            for index, warning in enumerate(trend_report.get("warnings", [])):
                if current_audio_chapters is not None and warning.get("chapter_number") not in current_audio_chapters:
                    continue
                kind = str(warning.get("kind") or "audio_consistency")
                items.append(
                    ReviewItem(
                        category="audio_trend",
                        item_id=(
                            f"{kind}:{warning.get('voice_id', 'unknown')}:{warning.get('chapter_number', index + 1)}"
                        ),
                        title=(
                            "Cross-chapter voice consistency"
                            if kind == "cross_chapter_voice_drift"
                            else "Sustained chapter prosody"
                        ),
                        reason=(
                            "This voice differs materially from its book-wide "
                            "identity baseline. Listen before final release."
                            if kind == "cross_chapter_voice_drift"
                            else "A sustained share of this voice's lines were "
                            "flagged as monotone. Listen before final release."
                        ),
                        blocking=False,
                        chapter_number=_int_or_none(warning.get("chapter_number")),
                        details=warning,
                    )
                )
        except (OSError, ValueError, TypeError):
            pass

    existing_attribution_ids = {item.item_id for item in items if item.category == "attribution"}
    audit_path = project_dir / "attribution_audit.json"
    if audit_path.is_file():
        try:
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            for index, issue in enumerate(audit.get("issues", [])):
                item_id = str(issue.get("line_id") or f"audit-{index + 1}")
                if item_id in existing_attribution_ids:
                    continue
                matched_line = script_lines_by_id.get(item_id, {})
                conf = _float_or_none(issue.get("speaker_confidence"))
                if conf is None:
                    conf = _float_or_none(matched_line.get("speaker_confidence"))
                review = persisted.get(("attribution", item_id), {})
                disposition = str(review.get("disposition", "unreviewed"))
                items.append(
                    ReviewItem(
                        category="attribution",
                        item_id=item_id,
                        title=f"Speaker attribution {item_id}",
                        reason=str(issue.get("message") or "Source-grounded attribution audit failed."),
                        confidence=conf,
                        disposition=disposition,
                        blocking=disposition not in RESOLVED_ATTRIBUTION_DISPOSITIONS,
                        chapter_number=_int_or_none(issue.get("chapter_number") or matched_line.get("chapter_number")),
                        details={
                            "audit_kind": issue.get("kind", "unknown"),
                            "speaker": issue.get("speaker") or matched_line.get("speaker"),
                            "source_excerpt": issue.get("source_excerpt") or matched_line.get("text"),
                            "decision_trail": _cap_decision_trail(matched_line.get("attribution_confidence_history")),
                        },
                    )
                )
                existing_attribution_ids.add(item_id)
        except (OSError, ValueError, TypeError):
            pass

    try:
        inv_path = project_dir / "pronunciation_inventory.json"
        if inv_path.is_file():
            inventory = json.loads(inv_path.read_text(encoding="utf-8"))
        else:
            inventory = build_pronunciation_inventory(project_dir)
        for candidate in inventory.get("candidates", []):
            if candidate.get("status") != "review_required":
                continue
            term = str(candidate.get("term", "unknown"))
            items.append(
                ReviewItem(
                    category="pronunciation",
                    item_id=term,
                    title=f"Pronunciation: {term}",
                    reason="Recurring name or term has no verified pronunciation mapping.",
                    blocking=False,
                    details={
                        "term": term,
                        "occurrences": candidate.get("occurrences", 0),
                        "chapters": candidate.get("chapters", []),
                    },
                )
            )
    except (OSError, ValueError, TypeError):
        pass

    items.sort(key=lambda item: (not item.blocking, item.category, item.chapter_number or 0, item.item_id))
    return ReviewGate(tuple(items))


def write_release_report(project_id: str, project_dir: Path, job_queue: Any) -> dict[str, Any]:
    """Persist the exact pre-master decision for audit and UI display."""
    from datetime import datetime

    from shared.artifacts import atomic_write_json

    report = collect_review_gate(project_id, project_dir, job_queue).to_dict()
    report["generated_at"] = datetime.now(UTC).isoformat()
    report["schema"] = 1
    atomic_write_json(project_dir / "pre_master_release.json", report)
    return report


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
