"""Validate a small risk-weighted set of existing segments with one model load."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain.orchestrator.job_queue import JobQueue
from shared.live_test_guard import add_model_opt_in
from shared.models import ScriptChapter
from voice.validator.whisper_validator import WhisperValidator


def _line_index(project_id: str) -> dict[str, object]:
    result = {}
    for path in sorted((ROOT / "brain" / "projects" / project_id / "script").glob("chapter_*.json")):
        if path.name.endswith(".meta.json"):
            continue
        chapter = ScriptChapter.model_validate_json(path.read_text(encoding="utf-8"))
        for line in chapter.lines:
            result[line.line_id] = line
    return result


def _select_ids(project_id: str, lines: dict[str, object], limit: int) -> list[str]:
    queue = JobQueue(str(ROOT / "brain" / "projects" / "pipeline_state.db"))
    logs = queue.get_quality_report(project_id)
    selected = queue._selected_quality_logs(logs)
    ranked: list[tuple[int, float, str]] = []
    max_attempt: dict[str, int] = {}
    for item in logs:
        max_attempt[item["line_id"]] = max(
            max_attempt.get(item["line_id"], 1), int(item.get("attempt") or 1)
        )
    for item in selected:
        line_id = item["line_id"]
        if line_id not in lines:
            continue
        status_rank = 0 if item.get("status") == "accepted_with_warning" else 1
        retry_rank = -max_attempt.get(line_id, 1)
        ranked.append((status_rank, retry_rank, line_id))
    chosen = [item[2] for item in sorted(ranked)[:limit]]
    if len(chosen) < limit:
        shortest = sorted(
            (line for line in lines.values() if line.line_id not in chosen),
            key=lambda line: len((line.spoken_text or line.text).split()),
        )
        chosen.extend(line.line_id for line in shortest[: limit - len(chosen)])
    return chosen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--limit", type=int, default=7)
    parser.add_argument("--compare-vad", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    add_model_opt_in(parser)
    args = parser.parse_args()
    if not args.allow_models:
        parser.error("--allow-models is required")

    config = yaml.safe_load((ROOT / "voice" / "config.yaml").read_text(encoding="utf-8")) or {}
    validation = config.get("validation", {})
    lines = _line_index(args.project)
    line_ids = _select_ids(args.project, lines, max(1, args.limit))
    segments = ROOT / "workspace" / args.project / "segments"
    validator = WhisperValidator(
        model_name=validation.get("whisper_model", "large-v3"),
        device=validation.get("whisper_device", "auto"),
        backend=validation.get("whisper_backend", "openai_whisper"),
        vad_filter=False,
    )
    report = {
        "schema_version": 1,
        "project_id": args.project,
        "model": validator.model_name,
        "backend": validator.backend,
        "line_ids": line_ids,
        "results": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        load_started = time.perf_counter()
        validator.load()
        report["model_load_seconds"] = time.perf_counter() - load_started
        for line_id in line_ids:
            line = lines[line_id]
            audio = segments / f"{line_id}.wav"
            expected = line.spoken_text or line.text
            started = time.perf_counter()
            raw_text = validator.transcribe(str(audio))
            item = {
                "line_id": line_id,
                "words": len(expected.split()),
                "audio_bytes": audio.stat().st_size,
                "raw_seconds": time.perf_counter() - started,
                "raw_wer": validator.calculate_wer(expected, raw_text),
            }
            if args.compare_vad:
                validator.vad_filter = True
                started = time.perf_counter()
                vad_text = validator.transcribe(str(audio))
                item.update({
                    "vad_seconds": time.perf_counter() - started,
                    "vad_wer": validator.calculate_wer(expected, vad_text),
                })
                validator.vad_filter = False
            report["results"].append(item)
            args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        report["raw_average_wer"] = sum(x["raw_wer"] for x in report["results"]) / len(report["results"])
        report["raw_failures"] = sum(x["raw_wer"] > 0.20 for x in report["results"])
        if args.compare_vad:
            report["vad_average_wer"] = sum(x["vad_wer"] for x in report["results"]) / len(report["results"])
            report["vad_failures"] = sum(x["vad_wer"] > 0.20 for x in report["results"])
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 0
    finally:
        validator.unload()


if __name__ == "__main__":
    raise SystemExit(main())
