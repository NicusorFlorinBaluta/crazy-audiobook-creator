"""Benchmark two script chunk bounds on identical immutable source excerpts."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain.orchestrator.pipeline import Pipeline
from scripts.benchmark_support import git_identity
from shared.artifacts import atomic_write_json, normalize_for_coverage
from shared.live_test_guard import add_model_opt_in
from shared.models import CharacterRegistry, ExtractedChapter


def _parse_configs(value: str) -> list[dict[str, int | str]]:
    configs: list[dict[str, int | str]] = []
    for raw in value.split(","):
        try:
            words_text, fragments_text = raw.strip().split(":", 1)
            words = int(words_text)
            fragments = int(fragments_text)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "configs must use WORDS:FRAGMENTS pairs, for example 350:40"
            ) from exc
        if words < 1 or fragments < 1:
            raise ValueError("script chunk bounds must be positive")
        configs.append(
            {
                "label": f"w{words}_f{fragments}",
                "chunk_size_words": words,
                "max_fragments_per_chunk": fragments,
            }
        )
    if len(configs) != 2:
        raise ValueError("exactly two script chunk configurations are required")
    return configs


def _excerpt_hash(fragments) -> str:
    digest = hashlib.sha256()
    for fragment in fragments:
        digest.update(fragment.text.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * 0.95
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _run_excerpt(
    generator,
    fragments,
    registry: CharacterRegistry,
    chapter: ExtractedChapter,
    config: dict[str, int | str],
    *,
    source_offset: int = 0,
    baseline_speakers: dict[int, str] | None = None,
    target_fragments: set[int] | None = None,
) -> dict[str, Any]:
    generator.chunk_size_words = int(config["chunk_size_words"])
    generator.max_fragments_per_chunk = int(config["max_fragments_per_chunk"])
    generator.call_metrics = []
    chunks = generator._chunk_fragments(fragments)
    lines = []
    summaries: list[str] = []
    offset = 0
    started = time.perf_counter()
    for chunk in chunks:
        script = generator._process_fragments(
            chunk,
            chapter.number,
            chapter.title,
            registry,
            " ".join(summaries)[-2000:],
            offset,
            chapter_text=chapter.text,
        )
        lines.extend(script.lines)
        if script.chapter_summary:
            summaries.append(script.chapter_summary)
        offset += len(chunk)
    wall_seconds = time.perf_counter() - started

    expected = normalize_for_coverage("".join(item.text for item in fragments))
    actual = normalize_for_coverage("".join(line.text for line in lines))
    line_ids = [line.line_id for line in lines]
    allowed_speakers = set(registry.characters)
    unknown_speakers = sorted(
        {line.speaker for line in lines if line.speaker not in allowed_speakers}
    )
    coverage_ok = expected == actual
    ids_ok = len(line_ids) == len(set(line_ids)) == len(fragments)
    baseline_speakers = baseline_speakers or {}
    target_fragments = target_fragments or set()
    changed_from_baseline = 0
    changed_fragment_ids: list[int] = []
    target_rows = []
    for local_index, line in enumerate(lines):
        global_index = source_offset + local_index
        baseline = baseline_speakers.get(global_index)
        if baseline is not None and baseline != line.speaker:
            changed_from_baseline += 1
            changed_fragment_ids.append(global_index)
        if global_index in target_fragments:
            target_rows.append(
                {
                    "fragment_id": global_index,
                    "changed_from_baseline": baseline != line.speaker,
                    "speaker_confidence": line.speaker_confidence,
                    "review_required": line.attribution_review_required,
                }
            )
    invariant_errors = []
    if not coverage_ok:
        invariant_errors.append("source_coverage")
    if not ids_ok:
        invariant_errors.append("fragment_ids")
    if unknown_speakers:
        invariant_errors.append("unknown_speakers")

    return {
        "config": config["label"],
        "chunk_count": len(chunks),
        "fragment_count": len(fragments),
        "source_words": sum(len(item.text.split()) for item in fragments),
        "output_lines": len(lines),
        "wall_seconds": wall_seconds,
        "wall_seconds_per_source_word": (
            wall_seconds
            / max(1, sum(len(item.text.split()) for item in fragments))
        ),
        "coverage_ok": coverage_ok,
        "ids_ok": ids_ok,
        "unknown_speakers": unknown_speakers,
        "invariant_errors": invariant_errors,
        "baseline_attribution_changes": changed_from_baseline,
        "baseline_attribution_change_ids": changed_fragment_ids,
        "target_validation": target_rows,
        "full_attempts": sum(
            int(item.get("full_attempts", item.get("attempts", 0)) or 0)
            for item in generator.call_metrics
        ),
        "structural_retries": sum(
            int(item.get("structural_retries", 0) or 0)
            for item in generator.call_metrics
        ),
        "focused_retries": sum(
            int(item.get("focused_retries", 0) or 0)
            for item in generator.call_metrics
        ),
        "local_repairs": sum(
            int(item.get("local_repairs", 0) or 0)
            for item in generator.call_metrics
        ),
        "fragment_fallbacks": sum(
            int(item.get("fragment_fallbacks", 0) or 0)
            for item in generator.call_metrics
        ),
        "calls": list(generator.call_metrics),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--chapter", type=int, default=7)
    parser.add_argument("--configs", default="350:40,550:60")
    parser.add_argument(
        "--offsets",
        default="0,80,160",
        help="Comma-separated starting fragment offsets",
    )
    parser.add_argument("--window-fragments", type=int, default=60)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--order", choices=("AB", "BA"), default="AB")
    parser.add_argument("--warmup-fragments", type=int, default=8)
    parser.add_argument(
        "--model",
        default="",
        help="Exact installed Ollama model tag; defaults to brain/config.yaml.",
    )
    parser.add_argument(
        "--context-window",
        type=int,
        default=0,
        help="Per-request context override; defaults to brain/config.yaml.",
    )
    parser.add_argument(
        "--dialogue-focused-schema",
        action="store_true",
        help="Benchmark experimental sparse dialogue-focused metadata schema v5.",
    )
    parser.add_argument(
        "--target-fragments",
        default="",
        help="Comma-separated global fragment IDs expected to be corrected.",
    )
    parser.add_argument(
        "--validation-only",
        action="store_true",
        help=(
            "Run only the first configuration for correctness/performance "
            "validation without a duplicate A/B candidate."
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    add_model_opt_in(parser)
    args = parser.parse_args()
    if not args.allow_models:
        parser.error("--allow-models is required")
    if args.window_fragments < 1 or args.repetitions < 1:
        parser.error("window fragments and repetitions must be positive")
    try:
        configs = _parse_configs(args.configs)
        offsets = [int(item) for item in args.offsets.split(",")]
        target_fragments = {
            int(item) for item in args.target_fragments.split(",") if item.strip()
        }
    except ValueError as exc:
        parser.error(str(exc))

    project_dir = ROOT / "brain" / "projects" / args.project
    config_path = ROOT / "brain" / "config.yaml"
    book_path = project_dir / "book.json"
    registry_path = project_dir / "characters.json"
    registry_payload: dict[str, Any]
    if registry_path.is_file():
        registry_payload = json.loads(registry_path.read_text(encoding="utf-8"))
    else:
        registry_path = project_dir / "characters.joint.checkpoint.json"
        checkpoint = json.loads(registry_path.read_text(encoding="utf-8"))
        registry_payload = checkpoint.get("registry") or {}
    book = json.loads(book_path.read_text(encoding="utf-8"))
    chapter_data = next(
        item for item in book["chapters"] if int(item["number"]) == args.chapter
    )
    chapter = ExtractedChapter.model_validate(chapter_data)
    registry = CharacterRegistry.model_validate(registry_payload)
    baseline_path = project_dir / "script" / f"chapter_{args.chapter:03d}.json"
    baseline_speakers: dict[int, str] = {}
    if baseline_path.is_file():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        for line in baseline.get("lines", []):
            for fragment_id in line.get("source_fragment_ids") or [
                line.get("source_fragment_id")
            ]:
                if fragment_id is not None:
                    baseline_speakers[int(fragment_id)] = str(
                        line.get("speaker") or "narrator"
                    )
    pipeline = Pipeline()
    if args.model:
        pipeline.ollama.model = args.model
    if args.context_window:
        if args.context_window < 4096:
            parser.error("--context-window must be at least 4096")
        pipeline.ollama.context_window = args.context_window
    generator = pipeline.script_generator
    generator.dialogue_focused_schema = args.dialogue_focused_schema
    all_fragments = generator._split_into_fragment_spans(chapter.text)
    excerpts = []
    for offset in offsets:
        if offset < 0 or offset >= len(all_fragments):
            continue
        selected = all_fragments[offset : offset + args.window_fragments]
        if selected:
            excerpts.append((offset, selected))
    if not excerpts:
        parser.error("no requested excerpt offset exists in the chapter")

    report: dict[str, Any] = {
        "schema_version": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project": args.project,
        "chapter": args.chapter,
        "source_control": git_identity(ROOT),
        "python_version": sys.version.split()[0],
        "brain_config_sha256": hashlib.sha256(
            config_path.read_bytes()
        ).hexdigest(),
        "runtime": {
            "model": getattr(pipeline.ollama, "model", None),
            "context_window": getattr(pipeline.ollama, "context_window", None),
            "think": getattr(pipeline.ollama, "think", None),
            "temperature": generator.temperature,
            "speaker_confidence_threshold": (
                generator.speaker_confidence_threshold
            ),
            "dialogue_focused_schema": generator.dialogue_focused_schema,
        },
        "book_sha256": hashlib.sha256(book_path.read_bytes()).hexdigest(),
        "registry_source": registry_path.name,
        "registry_sha256": hashlib.sha256(
            json.dumps(
                registry_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "available_fragments": len(all_fragments),
        "configs": configs,
        "protocol": {
            "order": args.order,
            "repetitions_per_config_per_excerpt": args.repetitions,
            "window_fragments": args.window_fragments,
            "warmup_fragments": args.warmup_fragments,
            "same_source_per_pair": True,
        },
        "excerpts": [
            {
                "offset": offset,
                "fragment_count": len(selected),
                "source_words": sum(len(item.text.split()) for item in selected),
                "source_sha256": _excerpt_hash(selected),
            }
            for offset, selected in excerpts
        ],
        "runs": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(args.output, report)

    managed_before = getattr(pipeline, "_ollama_server_proc", None)
    try:
        started = time.perf_counter()
        pipeline._start_ollama_server()
        report["server_start_seconds"] = time.perf_counter() - started
        pipeline.ollama.begin_run()

        warm_count = min(args.warmup_fragments, len(all_fragments))
        if warm_count:
            generator.call_metrics = []
            started = time.perf_counter()
            generator._process_fragments(
                all_fragments[:warm_count],
                chapter.number,
                chapter.title,
                registry,
                "",
                0,
            )
            report["warmup_seconds"] = time.perf_counter() - started
            report["warmup_fragment_count"] = warm_count
            atomic_write_json(args.output, report)

        for excerpt_index, (offset, selected) in enumerate(excerpts):
            order = ["A"] if args.validation_only else list(args.order)
            if excerpt_index % 2:
                order.reverse()
            for repetition in range(1, args.repetitions + 1):
                for slot, label in enumerate(order, 1):
                    config = configs[0 if label == "A" else 1]
                    run = _run_excerpt(
                        generator,
                        selected,
                        registry,
                        chapter,
                        config,
                        source_offset=offset,
                        baseline_speakers=baseline_speakers,
                        target_fragments=target_fragments,
                    )
                    run.update(
                        {
                            "excerpt_offset": offset,
                            "source_sha256": _excerpt_hash(selected),
                            "repetition": repetition,
                            "slot": slot,
                            "mode_label": label,
                        }
                    )
                    report["runs"].append(run)
                    atomic_write_json(args.output, report)
        report["summary"] = {}
        for config in configs:
            runs = [
                run
                for run in report["runs"]
                if run["config"] == config["label"]
            ]
            if not runs:
                continue
            normalized = [
                float(run["wall_seconds_per_source_word"])
                for run in runs
            ]
            report["summary"][config["label"]] = {
                "runs": len(runs),
                "wall_seconds_per_source_word_p50": statistics.median(normalized),
                "wall_seconds_per_source_word_p95": _p95(normalized),
                "full_attempts": sum(run["full_attempts"] for run in runs),
                "structural_retries": sum(
                    run["structural_retries"] for run in runs
                ),
                "focused_retries": sum(run["focused_retries"] for run in runs),
                "local_repairs": sum(run["local_repairs"] for run in runs),
                "fragment_fallbacks": sum(
                    run["fragment_fallbacks"] for run in runs
                ),
                "all_invariants_passed": all(
                    run["coverage_ok"]
                    and run["ids_ok"]
                    and not run["unknown_speakers"]
                    for run in runs
                ),
            }
        control = report["summary"][configs[0]["label"]]
        if args.validation_only:
            report["decision"] = {
                "automated_quality_gate_pass": bool(
                    control["all_invariants_passed"]
                    and control["fragment_fallbacks"] == 0
                ),
                "requires_manual_attribution_review": True,
                "promote": False,
                "mode": "validation_only",
            }
            atomic_write_json(args.output, report)
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0
        candidate = report["summary"][configs[1]["label"]]
        change = (
            candidate["wall_seconds_per_source_word_p50"]
            / control["wall_seconds_per_source_word_p50"]
            - 1.0
            if control["wall_seconds_per_source_word_p50"]
            else None
        )
        report["decision"] = {
            "normalized_wall_change_fraction": change,
            "speed_gate_pass": change is not None and change <= -0.20,
            "automated_quality_gate_pass": bool(
                candidate["all_invariants_passed"]
                and candidate["fragment_fallbacks"]
                <= control["fragment_fallbacks"]
            ),
            "requires_manual_attribution_review": True,
            "promote": False,
        }
        atomic_write_json(args.output, report)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    finally:
        if managed_before is None:
            pipeline._stop_ollama_server()


if __name__ == "__main__":
    raise SystemExit(main())
