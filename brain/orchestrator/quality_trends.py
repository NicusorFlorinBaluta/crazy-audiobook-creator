"""Book-level audio consistency diagnostics built from existing validators.

The analyzer deliberately performs no model inference.  It aggregates the
selected per-segment measurements already paid for during generation, so it is
safe to update after every completed chapter.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from statistics import median
from typing import Any, Iterable

from shared.models import ScriptChapter


def build_long_form_quality_report(
    quality_logs: Iterable[dict[str, Any]],
    scripts: Iterable[ScriptChapter],
) -> dict[str, Any]:
    """Aggregate voice drift and sustained prosody warnings across chapters."""
    line_owner: dict[str, tuple[str, str]] = {}
    for script in scripts:
        for line in script.lines:
            line_owner[line.line_id] = (line.voice_id or line.speaker, line.speaker)

    grouped_attempts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in quality_logs:
        grouped_attempts[str(row.get("line_id"))].append(row)
    selected: list[dict[str, Any]] = []
    for attempts in grouped_attempts.values():
        explicit = [row for row in attempts if row.get("details", {}).get("selected")]
        selected.append(max(explicit or attempts, key=lambda row: int(row.get("attempt") or 1)))

    buckets: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in selected:
        owner = line_owner.get(str(row.get("line_id")))
        if not owner:
            continue
        details = row.get("details") or {}
        buckets[(owner[0], int(row.get("chapter_number") or 0))].append(details)

    chapter_rows: list[dict[str, Any]] = []
    by_voice: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (voice_id, chapter_number), details_rows in sorted(buckets.items()):
        similarities = [
            float(row["speaker_similarity"])
            for row in details_rows
            if row.get("speaker_similarity") is not None
        ]
        pitches = [
            float(row["pitch_median"])
            for row in details_rows
            if float(row.get("pitch_median") or 0) > 0
        ]
        eligible_prosody = [
            row for row in details_rows if float(row.get("duration_seconds") or 0) >= 1.0
        ]
        item = {
            "voice_id": voice_id,
            "chapter_number": chapter_number,
            "segments": len(details_rows),
            "speaker_similarity_median": median(similarities) if similarities else None,
            "pitch_median_hz": median(pitches) if pitches else None,
            "prosody_eligible_segments": len(eligible_prosody),
            "monotone_fraction": (
                sum(bool(row.get("monotone_warning")) for row in eligible_prosody)
                / len(eligible_prosody)
                if eligible_prosody else None
            ),
            "warnings": [],
        }
        chapter_rows.append(item)
        if len(details_rows) >= 3:
            by_voice[voice_id].append(item)

    warnings: list[dict[str, Any]] = []
    for voice_id, rows in by_voice.items():
        similarity_baseline_values = [
            row["speaker_similarity_median"]
            for row in rows
            if row["speaker_similarity_median"] is not None
        ]
        pitch_baseline_values = [
            row["pitch_median_hz"]
            for row in rows
            if row["pitch_median_hz"] is not None
        ]
        similarity_baseline = (
            median(similarity_baseline_values) if similarity_baseline_values else None
        )
        pitch_baseline = median(pitch_baseline_values) if pitch_baseline_values else None
        for row in rows:
            similarity = row["speaker_similarity_median"]
            pitch = row["pitch_median_hz"]
            similarity_drop = (
                similarity_baseline - similarity
                if similarity is not None and similarity_baseline is not None
                else 0.0
            )
            pitch_deviation = (
                abs(pitch - pitch_baseline) / pitch_baseline
                if pitch and pitch_baseline
                else 0.0
            )
            # Pitch alone is expressive, not identity drift.  Escalate only
            # sustained pitch movement accompanied by weaker voice identity.
            if similarity is not None and (
                similarity < 0.62
                or similarity_drop >= 0.10
                or (pitch_deviation >= 0.25 and similarity_drop >= 0.05)
            ):
                warning = {
                    "kind": "cross_chapter_voice_drift",
                    "voice_id": voice_id,
                    "chapter_number": row["chapter_number"],
                    "speaker_similarity_median": similarity,
                    "similarity_baseline": similarity_baseline,
                    "pitch_deviation_ratio": pitch_deviation,
                }
                row["warnings"].append("cross_chapter_voice_drift")
                warnings.append(warning)
            monotone_fraction = row["monotone_fraction"]
            if row["prosody_eligible_segments"] >= 5 and monotone_fraction is not None and monotone_fraction >= 0.35:
                row["warnings"].append("sustained_monotone_delivery")
                warnings.append({
                    "kind": "sustained_monotone_delivery",
                    "voice_id": voice_id,
                    "chapter_number": row["chapter_number"],
                    "monotone_fraction": monotone_fraction,
                    "segments": row["prosody_eligible_segments"],
                })

    return {
        "schema": "long-form-audio-quality-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "selected_segments": len(selected),
        "chapter_voice_metrics": chapter_rows,
        "warnings": warnings,
        "warning_count": len(warnings),
        "policy": {
            "minimum_segments_per_voice_chapter": 3,
            "identity_similarity_floor": 0.62,
            "similarity_drop_threshold": 0.10,
            "pitch_requires_identity_corroboration": True,
            "monotone_fraction_threshold": 0.35,
        },
    }
