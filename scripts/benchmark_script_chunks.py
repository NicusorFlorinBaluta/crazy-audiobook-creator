"""Run two non-persisting real-data script annotation chunk sizes."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain.orchestrator.pipeline import Pipeline
from shared.live_test_guard import add_model_opt_in
from shared.models import CharacterRegistry, ExtractedChapter


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--chapter", type=int, default=7)
    parser.add_argument("--sizes", default="40,60")
    parser.add_argument("--output", type=Path, required=True)
    add_model_opt_in(parser)
    args = parser.parse_args()
    if not args.allow_models:
        parser.error("--allow-models is required")

    project_dir = ROOT / "brain" / "projects" / args.project
    book = json.loads((project_dir / "book.json").read_text(encoding="utf-8"))
    chapter_data = next(
        item for item in book["chapters"] if int(item["number"]) == args.chapter
    )
    chapter = ExtractedChapter.model_validate(chapter_data)
    registry = CharacterRegistry.model_validate_json(
        (project_dir / "characters.json").read_text(encoding="utf-8")
    )
    sizes = [int(item) for item in args.sizes.split(",") if int(item) > 0]
    pipeline = Pipeline()
    generator = pipeline.script_generator
    fragments = generator._split_into_fragment_spans(chapter.text)
    report = {
        "schema_version": 1,
        "project": args.project,
        "chapter": args.chapter,
        "available_fragments": len(fragments),
        "runs": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    managed_before = getattr(pipeline, "_ollama_server_proc", None)
    try:
        started = time.perf_counter()
        pipeline._start_ollama_server()
        report["server_start_seconds"] = time.perf_counter() - started
        pipeline.ollama.begin_run()
        for size in sizes:
            selected = fragments[: min(size, len(fragments))]
            generator.call_metrics = []
            started = time.perf_counter()
            result = generator._process_fragments(
                selected, chapter.number, chapter.title, registry, "", 0
            )
            wall = time.perf_counter() - started
            metric = generator.call_metrics[-1]
            report["runs"].append({
                "requested_fragments": size,
                "fragment_count": len(selected),
                "source_words": sum(len(item.text.split()) for item in selected),
                "output_lines": len(result.lines),
                "wall_seconds": wall,
                "fallback": metric.get("used_fallback", False),
                "attempts": metric.get("attempts"),
                "prompt_characters": metric.get("prompt_characters"),
                "ollama": metric.get("ollama", {}),
            })
            args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 0
    finally:
        if managed_before is None:
            pipeline._stop_ollama_server()


if __name__ == "__main__":
    raise SystemExit(main())
