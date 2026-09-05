"""Shared model-free utilities for reproducible benchmark harnesses."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def git_identity(repo_root: Path) -> dict[str, Any]:
    def run(*args: str) -> str | None:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            # Provenance metadata must never stall a benchmark run.
            timeout=30,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    status = run("status", "--porcelain", "--untracked-files=normal")
    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty": bool(status),
        "status": status.splitlines() if status else [],
    }


def dependency_versions() -> dict[str, str | None]:
    result: dict[str, str | None] = {"python": sys.version.split()[0]}
    for package in ("torch", "qwen-tts", "soundfile", "openai-whisper"):
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = None
    return result


def balanced_order(repetitions: int, pattern: str) -> list[str]:
    """Return equal A/B counts while retaining an ABBA or BAAB order."""
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    pattern = pattern.upper()
    if pattern not in {"ABBA", "BAAB"}:
        raise ValueError("pattern must be ABBA or BAAB")
    target = {"A": repetitions, "B": repetitions}
    counts = {"A": 0, "B": 0}
    result: list[str] = []
    while counts != target:
        for item in pattern:
            if counts[item] >= target[item]:
                continue
            result.append(item)
            counts[item] += 1
    return result


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def summarize_tts_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    rtfs = [float(run["realtime_factor"]) for run in runs]
    walls = [float(run["wall_seconds"]) for run in runs]
    similarities = [
        float(run["speaker_similarity"]) for run in runs if isinstance(run.get("speaker_similarity"), (int, float))
    ]
    wers = [float(run["wer"]) for run in runs if isinstance(run.get("wer"), (int, float))]
    return {
        "runs": len(runs),
        "rtf_p50": percentile(rtfs, 0.50),
        "rtf_p90": percentile(rtfs, 0.90),
        "rtf_p95": percentile(rtfs, 0.95),
        "wall_seconds_p50": percentile(walls, 0.50),
        "wall_seconds_p95": percentile(walls, 0.95),
        "average_wer": statistics.fmean(wers) if wers else None,
        "maximum_wer": max(wers) if wers else None,
        "average_speaker_similarity": (statistics.fmean(similarities) if similarities else None),
        "minimum_speaker_similarity": min(similarities) if similarities else None,
    }
