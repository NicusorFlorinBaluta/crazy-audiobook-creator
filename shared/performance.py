"""Versioned pipeline performance records and deterministic summaries."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

METRICS_SCHEMA_VERSION = 2


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
        "chapters": generation,
    }
