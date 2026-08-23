"""Artifact manifests, fingerprints, and atomic persistence helpers.

The pipeline is intentionally resumable and may generate only a subset of a
book's chapters.  These helpers make completion an artifact property instead
of a guess based on the last recorded pipeline stage.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from shared.constants import GENERATION_SCHEMA_VERSION, SCRIPT_SCHEMA_VERSION
from shared.models import ScriptChapter


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Any) -> str:
    return sha256_text(canonical_json(value))


def hash_file(path: str | Path) -> str:
    candidate = Path(path)
    if not candidate.is_file():
        return ""
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: str | Path, text: str) -> None:
    """Replace a UTF-8 text file atomically within its destination directory."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, destination)
        except PermissionError:
            shutil.copyfile(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_bytes(path: str | Path, content: bytes) -> None:
    """Replace a binary file atomically within its destination directory."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, destination)
        except PermissionError:
            shutil.copyfile(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: str | Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, default=str))


def normalize_for_coverage(text: str) -> str:
    """Normalize only whitespace for source-coverage comparisons."""
    return re.sub(r"\s+", "", text)


def assert_script_covers_source(chapter: ScriptChapter, source_text: str) -> None:
    """Ensure script fragments cover source text once, in source order."""
    if not chapter.lines and source_text.strip():
        raise ValueError(f"Chapter {chapter.chapter_number} has source text but no script lines")

    offsets_available = all(
        line.source_start is not None and line.source_end is not None
        for line in chapter.lines
    )
    if offsets_available:
        previous_end = 0
        reconstructed: list[str] = []
        for line in chapter.lines:
            assert line.source_start is not None and line.source_end is not None
            if line.source_start < previous_end:
                raise ValueError(
                    f"Chapter {chapter.chapter_number} has overlapping or out-of-order "
                    f"source spans at {line.line_id}"
                )
            source_slice = source_text[line.source_start : line.source_end]
            if source_slice != line.text:
                raise ValueError(
                    f"Chapter {chapter.chapter_number} source span mismatch at {line.line_id}"
                )
            reconstructed.append(source_slice)
            previous_end = line.source_end
        spoken = "".join(reconstructed)
    else:
        # Compatibility path for scripts created before source offsets existed.
        spoken = "".join(line.text for line in chapter.lines)

    if normalize_for_coverage(spoken) != normalize_for_coverage(source_text):
        raise ValueError(
            f"Chapter {chapter.chapter_number} script does not cover the normalized "
            "source exactly once and in order"
        )


def script_fingerprint(
    *,
    source_text: str,
    registry: Any,
    model_name: str,
    prompt_text: str,
    chunk_size_words: int,
    max_fragments_per_chunk: int = 60,
    adaptive_split_enabled: bool = True,
    adaptive_split_max_depth: int = 2,
    adaptive_split_min_fragments: int = 8,
    group_utterances: bool = False,
    utterance_target_chars: int = 260,
    utterance_max_words: int = 45,
    narrator_target_chars: int = 340,
    narrator_max_words: int = 58,
    expressive_target_chars: int = 180,
    expressive_max_words: int = 30,
    speaker_confidence_threshold: float = 0.55,
) -> str:
    return fingerprint(
        {
            "schema": SCRIPT_SCHEMA_VERSION,
            "source": sha256_text(source_text),
            "registry": registry,
            "model": model_name,
            "prompt": sha256_text(prompt_text),
            "chunk_size_words": chunk_size_words,
            "max_fragments_per_chunk": max_fragments_per_chunk,
            "adaptive_split_enabled": adaptive_split_enabled,
            "adaptive_split_max_depth": adaptive_split_max_depth,
            "adaptive_split_min_fragments": adaptive_split_min_fragments,
            "group_utterances": group_utterances,
            "utterance_target_chars": utterance_target_chars,
            "utterance_max_words": utterance_max_words,
            "narrator_target_chars": narrator_target_chars,
            "narrator_max_words": narrator_max_words,
            "expressive_target_chars": expressive_target_chars,
            "expressive_max_words": expressive_max_words,
            "speaker_confidence_threshold": speaker_confidence_threshold,
        }
    )


def build_segment_manifest(
    project_id: str,
    chapter: ScriptChapter,
    generation_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = deepcopy(generation_config or {})
    voice_reference_hashes = config.pop("voice_reference_hashes", {})
    entries = []
    for order, line in enumerate(chapter.lines):
        voice_id = line.voice_id or line.speaker
        entry = {
                "order": order,
                "line_id": line.line_id,
                "speaker": line.speaker,
                "voice_id": voice_id,
                "voice_reference_hash": voice_reference_hashes.get(voice_id, ""),
                "text_hash": sha256_text(line.text),
                "source_fragment_id": line.source_fragment_id,
                "source_fragment_ids": line.source_fragment_ids,
                "source_start": line.source_start,
                "source_end": line.source_end,
                "file": f"{project_id}/segments/{line.line_id}.wav",
                "pause_before_ms": line.pause_before_ms,
                "pause_after_ms": line.pause_after_ms,
            }
        if line.spoken_text:
            entry["spoken_text_hash"] = sha256_text(line.spoken_text)
        entries.append(entry)
    chapter_payload = chapter.model_dump(mode="json")
    for line_payload in chapter_payload.get("lines", []):
        if line_payload.get("spoken_text") is None:
            line_payload.pop("spoken_text", None)
        # dialogue_kind is source-audit metadata, not a synthesis input. Keep
        # legacy scripts/manifests stable when the optional field is absent;
        # explicit classifications remain in the script hash so repaired
        # chapters are regenerated.
        if line_payload.get("dialogue_kind") is None:
            line_payload.pop("dialogue_kind", None)
    payload = {
        "schema": GENERATION_SCHEMA_VERSION,
        "project_id": project_id,
        "chapter_number": chapter.chapter_number,
        # Omitting null spoken_text keeps pre-lexicon manifests stable. A real
        # synthesis override participates in the dependency hash.
        "script_hash": fingerprint(chapter_payload),
        "generation_config": config,
        "segments": entries,
    }
    payload["dependency_hash"] = fingerprint(payload)
    payload["manifest_hash"] = fingerprint(payload)
    return payload


def finalize_segment_manifest(
    manifest: dict[str, Any],
    workspace_root: str | Path = "workspace",
) -> dict[str, Any]:
    """Bind a dependency manifest to the exact bytes of all output segments."""
    finalized = deepcopy(manifest)
    root = Path(workspace_root)
    for item in finalized.get("segments", []):
        output_path = root / item["file"]
        output_hash = hash_file(output_path)
        if not output_hash:
            raise ValueError(f"Missing generated segment: {output_path}")
        item["output_hash"] = output_hash
    payload = {
        key: value
        for key, value in finalized.items()
        if key != "manifest_hash"
    }
    finalized["manifest_hash"] = fingerprint(payload)
    return finalized


def manifest_path(project_dir: str | Path, chapter_number: int) -> Path:
    return Path(project_dir) / "manifests" / f"chapter_{chapter_number:03d}.segments.json"


def master_manifest_path(project_dir: str | Path, chapter_number: int) -> Path:
    return Path(project_dir) / "manifests" / f"chapter_{chapter_number:03d}.master.json"


def format_chapter_set(chapters: list[int] | set[int]) -> str:
    """Format chapter numbers compactly, e.g. ``1-3_5_8-10``."""
    ordered = sorted(set(chapters))
    if not ordered:
        return "none"
    ranges: list[str] = []
    start = previous = ordered[0]
    for number in ordered[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return "_".join(ranges)
