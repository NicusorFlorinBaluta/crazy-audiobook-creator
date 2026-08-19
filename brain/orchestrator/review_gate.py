"""Typed review aggregation and pre-master release gating."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from shared.pronunciation import build_pronunciation_inventory


RESOLVED_SEGMENT_DISPOSITIONS = {"acceptable", "regenerate"}
RESOLVED_EXTRACTION_DISPOSITIONS = {"include", "exclude", "reference"}


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


def collect_review_gate(project_id: str, project_dir: Path, job_queue: Any) -> ReviewGate:
    """Collect all known review work without exposing source text by default."""
    items: list[ReviewItem] = []
    persisted = {
        (row["item_type"], row["item_id"]): row
        for row in job_queue.get_review_items(project_id)
    }

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
                items.append(ReviewItem(
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
                ))
        except (OSError, ValueError, TypeError):
            items.append(ReviewItem(
                category="extraction",
                item_id="audit-invalid",
                title="Extraction audit unavailable",
                reason="The extraction audit could not be read safely.",
                blocking=True,
            ))

    script_lines_by_id: dict[str, dict[str, Any]] = {}
    for chapter_path in sorted((project_dir / "script").glob("chapter_*.json")):
        try:
            chapter = json.loads(chapter_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        chapter_number = _int_or_none(chapter.get("chapter_number"))
        for line in chapter.get("lines", []):
            line_id = str(line.get("line_id") or line.get("id") or "unknown")
            script_lines_by_id[line_id] = {**line, "chapter_number": chapter_number}
            if not line.get("attribution_review_required"):
                continue
            items.append(ReviewItem(
                category="attribution",
                item_id=line_id,
                title=f"Speaker attribution {line_id}",
                reason=str(line.get("attribution_review_reason") or line.get("speaker_evidence") or "Speaker could not be resolved safely."),
                confidence=_float_or_none(line.get("speaker_confidence")),
                blocking=True,
                chapter_number=chapter_number,
                details={
                    "speaker": line.get("speaker"),
                    "source_excerpt": line.get("text"),
                    "decision_trail": line.get("attribution_confidence_history", []),
                },
            ))

    quality_by_line: dict[str, dict[str, Any]] = {}
    for row in job_queue.get_quality_report(project_id):
        if row.get("details", {}).get("selected"):
            quality_by_line[row["line_id"]] = row
    for (item_type, item_id), review in persisted.items():
        if item_type != "segment":
            continue
        quality = quality_by_line.get(item_id, {})
        details = quality.get("details", {})
        disposition = review.get("disposition", "unreviewed")
        script_line = script_lines_by_id.get(item_id, {})
        items.append(ReviewItem(
            category="audio",
            item_id=item_id,
            title=f"Audio segment {item_id}",
            reason=str(details.get("manual_review_reason") or review.get("note") or "Automatic validators abstained."),
            confidence=_float_or_none(details.get("validation_confidence")),
            disposition=disposition,
            blocking=disposition not in RESOLVED_SEGMENT_DISPOSITIONS,
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
        ))

    existing_attribution_ids = {
        item.item_id for item in items if item.category == "attribution"
    }
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
                items.append(ReviewItem(
                    category="attribution",
                    item_id=item_id,
                    title=f"Speaker attribution {item_id}",
                    reason=str(issue.get("message") or "Source-grounded attribution audit failed."),
                    confidence=conf,
                    blocking=True,
                    chapter_number=_int_or_none(issue.get("chapter_number") or matched_line.get("chapter_number")),
                    details={
                        "audit_kind": issue.get("kind", "unknown"),
                        "speaker": issue.get("speaker") or matched_line.get("speaker"),
                        "source_excerpt": issue.get("source_excerpt") or matched_line.get("text"),
                        "decision_trail": matched_line.get("attribution_confidence_history", []),
                    },
                ))
                existing_attribution_ids.add(item_id)
        except (OSError, ValueError, TypeError):
            pass

    try:
        inventory = build_pronunciation_inventory(project_dir)
        for candidate in inventory.get("candidates", []):
            if candidate.get("status") != "review_required":
                continue
            term = str(candidate.get("term", "unknown"))
            items.append(ReviewItem(
                category="pronunciation",
                item_id=term,
                title=f"Pronunciation: {term}",
                reason="Recurring name or term has no verified pronunciation mapping.",
                blocking=False,
                details={
                    "occurrences": candidate.get("occurrences", 0),
                    "chapters": candidate.get("chapters", []),
                },
            ))
    except (OSError, ValueError, TypeError):
        pass

    items.sort(key=lambda item: (not item.blocking, item.category, item.chapter_number or 0, item.item_id))
    return ReviewGate(tuple(items))


def write_release_report(project_id: str, project_dir: Path, job_queue: Any) -> dict[str, Any]:
    """Persist the exact pre-master decision for audit and UI display."""
    from datetime import datetime, timezone
    from shared.artifacts import atomic_write_json

    report = collect_review_gate(project_id, project_dir, job_queue).to_dict()
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
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
