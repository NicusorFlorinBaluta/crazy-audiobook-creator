"""Book-level audio consistency diagnostics built from existing validators.

The analyzer deliberately performs no model inference.  It aggregates the
selected per-segment measurements already paid for during generation, so it is
safe to update after every completed chapter.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from statistics import median, stdev
from typing import Any

from shared.models import ScriptChapter

# Within-chapter consistency thresholds. These are diagnostic only: they never
# block a release, and they were chosen to be quiet on the current corpus so
# that a *change* in variance is the signal rather than an absolute level.
WITHIN_CHAPTER_MIN_SEGMENTS = 6
PITCH_VARIATION_WARN_RATIO = 0.22        # stdev / median of pitch_median
RATE_VARIATION_WARN_RATIO = 0.30         # stdev / median of chars-per-second
PITCH_JUMP_WARN_RATIO = 0.45             # largest adjacent-line pitch jump


def _relative_spread(values: list[float]) -> float | None:
    """Return stdev/median, a scale-free measure of spread."""
    usable = [value for value in values if value > 0]
    if len(usable) < 2:
        return None
    centre = median(usable)
    if centre <= 0:
        return None
    return round(stdev(usable) / centre, 6)


def _within_chapter_consistency(
    details_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Measure line-to-line delivery variation for one voice in one chapter.

    Every existing audio gate is per-segment (WER, clipping, duration, pitch
    CV, speaker similarity) or cross-chapter (`cross_chapter_voice_drift`).
    Nothing measured whether adjacent lines in the same chapter actually match
    each other, which is the artifact a listener notices first: the engine
    samples with `do_sample: true` at `temperature: 0.9`, so each line is an
    independent draw, and a single regenerated line lands beside neighbours
    drawn separately.

    This is computed entirely from measurements already paid for during
    validation, so it adds no inference cost. It exists to give a sampling or
    seeding change something to be evaluated against.
    """
    ordered = sorted(details_rows, key=lambda row: str(row.get("line_id", "")))
    pitches = [float(row.get("pitch_median") or 0.0) for row in ordered]
    rates: list[float] = []
    for row in ordered:
        duration = float(row.get("duration_seconds") or 0.0)
        characters = int(row.get("text_characters") or 0)
        if duration > 0 and characters > 0:
            rates.append(characters / duration)

    voiced_pitches = [value for value in pitches if value > 0]
    largest_jump: float | None = None
    if len(voiced_pitches) >= 2:
        centre = median(voiced_pitches)
        if centre > 0:
            jumps = [
                abs(later - earlier) / centre
                for earlier, later in zip(voiced_pitches, voiced_pitches[1:])
            ]
            largest_jump = round(max(jumps), 6)

    pitch_spread = _relative_spread(pitches)
    rate_spread = _relative_spread(rates)

    warnings: list[str] = []
    if len(ordered) >= WITHIN_CHAPTER_MIN_SEGMENTS:
        if pitch_spread is not None and pitch_spread >= PITCH_VARIATION_WARN_RATIO:
            warnings.append("within_chapter_pitch_variation")
        if rate_spread is not None and rate_spread >= RATE_VARIATION_WARN_RATIO:
            warnings.append("within_chapter_rate_variation")
        if largest_jump is not None and largest_jump >= PITCH_JUMP_WARN_RATIO:
            warnings.append("within_chapter_pitch_jump")

    return {
        "segments": len(ordered),
        "measured_for_warnings": len(ordered) >= WITHIN_CHAPTER_MIN_SEGMENTS,
        "pitch_relative_spread": pitch_spread,
        "speaking_rate_relative_spread": rate_spread,
        "largest_adjacent_pitch_jump_ratio": largest_jump,
        "warnings": warnings,
    }


def build_long_form_quality_report(
    quality_logs: Iterable[dict[str, Any]],
    scripts: Iterable[ScriptChapter],
) -> dict[str, Any]:
    """Aggregate voice drift and sustained prosody warnings across chapters."""
    line_owner: dict[str, tuple[str, str]] = {}
    line_characters: dict[str, int] = {}
    for script in scripts:
        for line in script.lines:
            line_owner[line.line_id] = (line.voice_id or line.speaker, line.speaker)
            line_characters[line.line_id] = len(line.text or "")

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
        details = dict(row.get("details") or {})
        line_id = str(row.get("line_id"))
        details.setdefault("line_id", line_id)
        details.setdefault("text_characters", line_characters.get(line_id, 0))
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
        item["within_chapter_consistency"] = _within_chapter_consistency(
            details_rows
        )
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
            for consistency_warning in row["within_chapter_consistency"]["warnings"]:
                if consistency_warning not in row["warnings"]:
                    row["warnings"].append(consistency_warning)
                warnings.append({
                    "kind": consistency_warning,
                    "voice_id": voice_id,
                    "chapter_number": row["chapter_number"],
                    **{
                        key: value
                        for key, value in row["within_chapter_consistency"].items()
                        if key != "warnings"
                    },
                })
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
        "generated_at": datetime.now(UTC).isoformat(),
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
            "within_chapter_min_segments": WITHIN_CHAPTER_MIN_SEGMENTS,
            "within_chapter_pitch_variation_threshold": PITCH_VARIATION_WARN_RATIO,
            "within_chapter_rate_variation_threshold": RATE_VARIATION_WARN_RATIO,
            "within_chapter_pitch_jump_threshold": PITCH_JUMP_WARN_RATIO,
        },
    }
