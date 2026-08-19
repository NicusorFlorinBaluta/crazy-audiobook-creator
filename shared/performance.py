"""Versioned pipeline performance records and deterministic summaries."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

METRICS_SCHEMA_VERSION = 3


def _percentile(values: list[float], quantile: float) -> float:
    """Return a deterministic linearly interpolated percentile."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 6)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return round(
        ordered[lower] + (ordered[upper] - ordered[lower]) * fraction,
        6,
    )


def _segment_latency_summary(
    segments: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    items = list(segments)
    synthesis = [
        float(item["synthesis_seconds"])
        for item in items
        if isinstance(item.get("synthesis_seconds"), (int, float))
    ]
    rtfs = [
        float(item["synthesis_audio_rtf"])
        for item in items
        if isinstance(item.get("synthesis_audio_rtf"), (int, float))
    ]
    return {
        "segments": len(items),
        "synthesis_seconds": {
            "p50": _percentile(synthesis, 0.50),
            "p90": _percentile(synthesis, 0.90),
            "p95": _percentile(synthesis, 0.95),
        },
        "synthesis_audio_rtf": {
            "p50": _percentile(rtfs, 0.50),
            "p90": _percentile(rtfs, 0.90),
            "p95": _percentile(rtfs, 0.95),
        },
    }


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _tts_segment_summary(
    generation: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    segments = [
        item
        for record in generation
        for item in (record.get("segment_metrics") or [])
        if isinstance(item, dict)
    ]
    if not segments:
        return {}

    fresh = [item for item in segments if not item.get("synthesis_cache_hit")]
    cached = [item for item in segments if item.get("synthesis_cache_hit")]
    length_groups = {
        "short_0_80": [
            item
            for item in fresh
            if _safe_int(item.get("text_characters")) <= 80
        ],
        "medium_81_260": [
            item
            for item in fresh
            if 80 < _safe_int(item.get("text_characters")) <= 260
        ],
        "long_261_plus": [
            item
            for item in fresh
            if _safe_int(item.get("text_characters")) > 260
        ],
    }
    roles = sorted(
        {
            str(item.get("speaker_role"))
            for item in fresh
            if item.get("speaker_role")
        }
    )
    substage_totals: defaultdict[str, float] = defaultdict(float)
    for item in fresh:
        for key, value in item.items():
            if (
                key.endswith("_seconds")
                and key.startswith(("tts_", "retry_tts_"))
                and isinstance(value, (int, float))
            ):
                substage_totals[key] += float(value)

    return {
        "all": _segment_latency_summary(segments),
        "fresh": _segment_latency_summary(fresh),
        "cached": _segment_latency_summary(cached),
        "by_text_length": {
            key: _segment_latency_summary(value)
            for key, value in length_groups.items()
        },
        "by_speaker_role": {
            role: _segment_latency_summary(
                item for item in fresh if item.get("speaker_role") == role
            )
            for role in roles
        },
        "by_model_state": {
            "cold": _segment_latency_summary(
                item
                for item in fresh
                if _safe_int(item.get("tts_cold_model_loads")) > 0
            ),
            "warm": _segment_latency_summary(
                item
                for item in fresh
                if _safe_int(item.get("tts_cold_model_loads")) == 0
            ),
        },
        "substage_totals_seconds": dict(sorted(substage_totals.items())),
    }


def read_metrics(path: str | Path) -> list[dict[str, Any]]:
    """Read valid JSONL metrics while tolerating a truncated final line."""
    records: list[dict[str, Any]] = []
    metric_path = Path(path)
    if not metric_path.is_file():
        return records
    for raw_line in metric_path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines():
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def latest_chapter_records(
    records: Iterable[dict[str, Any]], event: str
) -> list[dict[str, Any]]:
    """Select the final successful-looking record for each chapter."""
    latest: dict[int, dict[str, Any]] = {}
    for record in records:
        if record.get("event") != event:
            continue
        try:
            chapter = int(record["chapter_number"])
        except (KeyError, TypeError, ValueError):
            continue
        if record.get("failed_validation", 0) or record.get("status") == "failed":
            continue
        latest[chapter] = record
    return [latest[key] for key in sorted(latest)]


def summarize_metrics(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Build cache- and resume-aware totals from versioned JSONL records."""
    records = list(records)
    director_runs = [
        {
            "director_mode": str(
                record.get("director_mode")
                or (
                    "two_pass"
                    if _safe_float(record.get("pass1_seconds")) > 0
                    else "unknown"
                )
            ),
            "pass1_seconds": _safe_float(record.get("pass1_seconds")),
            "pass2_seconds": _safe_float(record.get("pass2_seconds")),
            "reconciliation_seconds": _safe_float(
                record.get("reconciliation_seconds")
            ),
            "total_seconds": _safe_float(record.get("total_seconds")),
            "chapters": _safe_int(record.get("chapters")),
            "segments": _safe_int(record.get("segments")),
        }
        for record in records
        if record.get("event") == "script_generation"
    ]
    generation = latest_chapter_records(records, "chapter_generation")
    mastering = latest_chapter_records(records, "chapter_mastering")
    timing_totals: defaultdict[str, float] = defaultdict(float)
    totals: defaultdict[str, float] = defaultdict(float)
    for record in generation:
        for key, value in (record.get("timings_seconds") or {}).items():
            if isinstance(value, (int, float)):
                timing_totals[key] += float(value)
        for key in (
            "segments",
            "synthesis_cache_hits",
            "synthesis_cache_misses",
            "validation_cache_hits",
            "validation_cache_misses",
            "retries",
            "accepted_with_warning",
            "failed_validation",
            "audio_duration_seconds",
        ):
            value = record.get(key)
            if isinstance(value, (int, float)):
                totals[key] += float(value)
    return {
        "schema_version": METRICS_SCHEMA_VERSION,
        "generation_chapters": len(generation),
        "mastered_chapters": len(mastering),
        "generation_totals": dict(sorted(totals.items())),
        "timing_totals_seconds": dict(sorted(timing_totals.items())),
        "tts_segments": _tts_segment_summary(generation),
        "script_director_runs": director_runs,
        "chapters": generation,
    }
