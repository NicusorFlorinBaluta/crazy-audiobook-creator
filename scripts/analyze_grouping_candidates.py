"""Screen larger same-speaker grouping bounds without loading any models."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain.director.script_generator import ScriptGenerator
from shared.models import ScriptChapter


DEFAULT_CANDIDATES = "300:50:400:68,340:58:460:78"


@dataclass(frozen=True)
class GroupingBounds:
    name: str
    ordinary_chars: int
    ordinary_words: int
    narrator_chars: int
    narrator_words: int


def parse_candidates(value: str) -> list[GroupingBounds]:
    candidates: list[GroupingBounds] = []
    for index, raw in enumerate(value.split(","), 1):
        fields = raw.strip().split(":")
        if len(fields) != 4:
            raise ValueError(
                "grouping candidates must use ordinary_chars:ordinary_words:"
                "narrator_chars:narrator_words"
            )
        ordinary_chars, ordinary_words, narrator_chars, narrator_words = (
            int(field) for field in fields
        )
        if min(ordinary_chars, ordinary_words, narrator_chars, narrator_words) <= 0:
            raise ValueError("grouping bounds must be positive")
        if ordinary_chars > 500 or narrator_chars > 500:
            raise ValueError("character bounds must not exceed the TTS 500-character ceiling")
        candidates.append(
            GroupingBounds(
                name=f"candidate_{index}",
                ordinary_chars=ordinary_chars,
                ordinary_words=ordinary_words,
                narrator_chars=narrator_chars,
                narrator_words=narrator_words,
            )
        )
    if not candidates:
        raise ValueError("at least one grouping candidate is required")
    return candidates


def _fragment_ids(script: ScriptChapter) -> list[int]:
    return [
        fragment_id
        for line in script.lines
        for fragment_id in (
            line.source_fragment_ids
            or ([line.source_fragment_id] if line.source_fragment_id is not None else [])
        )
    ]


def _source_mismatches(script: ScriptChapter, source_text: str) -> list[str]:
    mismatches: list[str] = []
    for line in script.lines:
        if line.source_start is None or line.source_end is None:
            mismatches.append(line.line_id)
        elif line.text != source_text[line.source_start : line.source_end]:
            mismatches.append(line.line_id)
    return mismatches


def analyze_chapter(
    control: ScriptChapter,
    source_text: str,
    bounds: GroupingBounds,
) -> dict[str, Any]:
    generator = ScriptGenerator(
        ollama=object(),  # Grouping is deterministic and never calls Ollama.
        group_utterances=True,
        utterance_target_chars=bounds.ordinary_chars,
        utterance_max_words=bounds.ordinary_words,
        narrator_target_chars=bounds.narrator_chars,
        narrator_max_words=bounds.narrator_words,
    )
    candidate = generator._group_adjacent_utterances(control, source_text)
    control_ranges = {
        (line.source_start, line.source_end): line.line_id for line in control.lines
    }
    added_merges: list[dict[str, Any]] = []
    for line in candidate.lines:
        line_range = (line.source_start, line.source_end)
        if line_range in control_ranges:
            continue
        constituents = [
            item.line_id
            for item in control.lines
            if item.source_start is not None
            and item.source_end is not None
            and line.source_start is not None
            and line.source_end is not None
            and item.source_start >= line.source_start
            and item.source_end <= line.source_end
        ]
        added_merges.append(
            {
                "line_id": line.line_id,
                "speaker": line.speaker,
                "characters": len(line.text),
                "words": len(line.text.split()),
                "constituent_line_ids": constituents,
                "text_sha256": hashlib.sha256(line.text.encode("utf-8")).hexdigest(),
                "text_preview": line.text[:160],
            }
        )

    control_fragments = _fragment_ids(control)
    candidate_fragments = _fragment_ids(candidate)
    control_narrator = sum(line.speaker == "narrator" for line in control.lines)
    candidate_narrator = sum(line.speaker == "narrator" for line in candidate.lines)
    control_max_characters = max((len(line.text) for line in control.lines), default=0)
    max_characters = max((len(line.text) for line in candidate.lines), default=0)
    introduced_over_engine_ceiling = any(
        merge["characters"] > 500 for merge in added_merges
    )
    return {
        "chapter": control.chapter_number,
        "control_calls": len(control.lines),
        "candidate_calls": len(candidate.lines),
        "call_reduction": len(control.lines) - len(candidate.lines),
        "narrator_call_reduction": control_narrator - candidate_narrator,
        "character_call_reduction": (
            (len(control.lines) - control_narrator)
            - (len(candidate.lines) - candidate_narrator)
        ),
        "control_max_characters": control_max_characters,
        "max_characters": max_characters,
        "preexisting_over_engine_ceiling": control_max_characters > 500,
        "introduced_over_engine_ceiling": introduced_over_engine_ceiling,
        "fragment_trace_preserved": control_fragments == candidate_fragments,
        "unique_fragment_trace": len(candidate_fragments) == len(set(candidate_fragments)),
        "source_mismatches": _source_mismatches(candidate, source_text),
        "added_merges": added_merges,
    }


def _source_control() -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.check_output(
            ["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()

    try:
        return {
            "commit": run("rev-parse", "HEAD"),
            "branch": run("branch", "--show-current"),
            "dirty": bool(run("status", "--porcelain")),
        }
    except (OSError, subprocess.CalledProcessError):
        return {"commit": "unknown", "branch": "unknown", "dirty": True}


def analyze_project(project_dir: Path, candidates: list[GroupingBounds]) -> dict[str, Any]:
    book = json.loads((project_dir / "book.json").read_text(encoding="utf-8"))
    source_by_chapter = {
        int(chapter["number"]): str(chapter["text"]) for chapter in book["chapters"]
    }
    report: dict[str, Any] = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project": project_dir.name,
        "source_control": _source_control(),
        "control": {
            "ordinary_chars": 260,
            "ordinary_words": 45,
            "narrator_chars": 340,
            "narrator_words": 58,
            "expressive_chars": 180,
            "expressive_words": 30,
        },
        "candidates": [],
    }
    script_paths = sorted((project_dir / "script").glob("chapter_*.json"))
    script_paths = [path for path in script_paths if not path.name.endswith(".meta.json")]
    for bounds in candidates:
        chapters: list[dict[str, Any]] = []
        for script_path in script_paths:
            control = ScriptChapter.model_validate_json(
                script_path.read_text(encoding="utf-8")
            )
            chapters.append(
                analyze_chapter(control, source_by_chapter[control.chapter_number], bounds)
            )
        control_calls = sum(chapter["control_calls"] for chapter in chapters)
        candidate_calls = sum(chapter["candidate_calls"] for chapter in chapters)
        all_merges = [merge for chapter in chapters for merge in chapter["added_merges"]]
        invariants_passed = all(
            chapter["fragment_trace_preserved"]
            and chapter["unique_fragment_trace"]
            and not chapter["source_mismatches"]
            and not chapter["introduced_over_engine_ceiling"]
            for chapter in chapters
        )
        report["candidates"].append(
            {
                "name": bounds.name,
                "bounds": {
                    "ordinary_chars": bounds.ordinary_chars,
                    "ordinary_words": bounds.ordinary_words,
                    "narrator_chars": bounds.narrator_chars,
                    "narrator_words": bounds.narrator_words,
                },
                "control_calls": control_calls,
                "candidate_calls": candidate_calls,
                "call_reduction": control_calls - candidate_calls,
                "call_reduction_fraction": (
                    (control_calls - candidate_calls) / control_calls if control_calls else 0.0
                ),
                "narrator_call_reduction": sum(
                    chapter["narrator_call_reduction"] for chapter in chapters
                ),
                "character_call_reduction": sum(
                    chapter["character_call_reduction"] for chapter in chapters
                ),
                "max_characters": max(
                    (chapter["max_characters"] for chapter in chapters), default=0
                ),
                "preexisting_over_engine_ceiling": any(
                    chapter["preexisting_over_engine_ceiling"] for chapter in chapters
                ),
                "introduced_over_engine_ceiling": any(
                    chapter["introduced_over_engine_ceiling"] for chapter in chapters
                ),
                "invariants_passed": invariants_passed,
                "added_merge_count": len(all_merges),
                "added_merges": all_merges,
                "chapters": chapters,
            }
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--candidates", default=DEFAULT_CANDIDATES)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    project_dir = ROOT / "brain" / "projects" / args.project
    if not project_dir.is_dir():
        parser.error(f"project does not exist: {project_dir}")
    try:
        candidates = parse_candidates(args.candidates)
    except ValueError as exc:
        parser.error(str(exc))
    report = analyze_project(project_dir, candidates)
    output = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        output_path = args.output if args.output.is_absolute() else ROOT / args.output
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output, encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
