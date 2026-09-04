"""Audit or selectively repair a completed project's speaker attribution."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brain.director.attribution_audit import audit_book_attribution
from brain.orchestrator.pipeline import Pipeline
from shared.artifacts import atomic_write_json, atomic_write_text, hash_file
from shared.models import BookScript, CharacterRegistry, ExtractedBook, ScriptChapter

CANDIDATE_CACHE_VERSION = "focused-attribution-v3-kind-boundaries"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Use focused model calls and atomically install a passing repair",
    )
    args = parser.parse_args()

    pipeline = Pipeline()
    project_dir = pipeline.projects_dir / args.project_id
    if not project_dir.is_dir():
        raise SystemExit(f"Project not found: {project_dir}")
    book = ExtractedBook.model_validate_json(
        (project_dir / "book.json").read_text(encoding="utf-8")
    )
    registry = CharacterRegistry.model_validate_json(
        (project_dir / "characters.json").read_text(encoding="utf-8")
    )
    script_paths = pipeline._script_files(project_dir / "script")
    scripts = [
        ScriptChapter.model_validate_json(path.read_text(encoding="utf-8"))
        for path in script_paths
    ]
    initial_report = audit_book_attribution(book, registry, scripts)
    if not args.apply:
        print(json.dumps(initial_report, indent=2))
        return 0 if initial_report["passed"] else 2
    if initial_report["passed"]:
        print(json.dumps({"status": "already_passed", "report": initial_report}, indent=2))
        return 0

    suspect_chapters = {
        int(issue["chapter_number"])
        for issue in initial_report["issues"]
        if issue.get("chapter_number") is not None
    }
    chapters = {chapter.number: chapter for chapter in book.chapters}
    candidate_scripts: list[ScriptChapter] = []
    repair_metrics: list[dict] = []
    candidate_dir = project_dir / "recovery" / "attribution-candidate"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    pipeline._start_ollama_server()
    try:
        for script, source_path in zip(scripts, script_paths, strict=True):
            if script.chapter_number not in suspect_chapters:
                candidate_scripts.append(script)
                continue
            cache_script = candidate_dir / source_path.name
            cache_meta = candidate_dir / f"{source_path.stem}.meta.json"
            source_hash = hash_file(source_path)
            if cache_script.is_file() and cache_meta.is_file():
                try:
                    metadata = json.loads(cache_meta.read_text(encoding="utf-8"))
                    cached = ScriptChapter.model_validate_json(
                        cache_script.read_text(encoding="utf-8")
                    )
                    chapter_report = audit_book_attribution(
                        book.model_copy(update={"chapters": [chapters[script.chapter_number]]}),
                        registry,
                        [cached],
                    )
                    if (
                        metadata.get("cache_version") == CANDIDATE_CACHE_VERSION
                        and metadata.get("source_script_hash") == source_hash
                        and chapter_report["passed"]
                    ):
                        candidate_scripts.append(cached)
                        repair_metrics.append(metadata["metrics"])
                        continue
                except (OSError, ValueError, KeyError):
                    pass
            repaired, metrics = pipeline.script_generator.repair_chapter_attribution(
                chapters[script.chapter_number],
                script,
                registry,
            )
            candidate_scripts.append(repaired)
            repair_metrics.append(metrics)
            atomic_write_text(cache_script, repaired.model_dump_json(indent=2))
            atomic_write_json(
                cache_meta,
                {
                    "cache_version": CANDIDATE_CACHE_VERSION,
                    "source_script_hash": source_hash,
                    "metrics": metrics,
                },
            )
    finally:
        pipeline.ollama.unload_model()
        pipeline._stop_ollama_server()

    final_report = audit_book_attribution(book, registry, candidate_scripts)
    if not final_report["passed"]:
        raise RuntimeError(
            "Candidate repair did not pass attribution audit; no project "
            "artifacts were changed. Remaining issues: "
            + json.dumps(final_report["issues"], ensure_ascii=False)
        )

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = project_dir / "recovery" / f"attribution-{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    shutil.copytree(project_dir / "script", backup_dir / "script")
    for name in ("book_script.json", "attribution_audit.json"):
        source = project_dir / name
        if source.is_file():
            shutil.copy2(source, backup_dir / name)

    candidate_by_number = {
        script.chapter_number: script for script in candidate_scripts
    }
    changed_chapters = sorted(
        metric["chapter_number"]
        for metric in repair_metrics
        if metric["changed_fragments"]
    )
    for path in script_paths:
        chapter_number = int(path.stem.split("_")[-1])
        script = candidate_by_number[chapter_number]
        atomic_write_text(path, script.model_dump_json(indent=2))
        atomic_write_json(
            path.with_name(f"chapter_{chapter_number:03d}.meta.json"),
            {
                "fingerprint": pipeline.script_generator.chapter_fingerprint(
                    chapters[chapter_number], registry
                )
            },
        )

    book_script = BookScript(
        metadata=book.metadata,
        character_registry=registry,
        chapters=candidate_scripts,
    )
    atomic_write_text(
        project_dir / "book_script.json",
        book_script.model_dump_json(indent=2),
    )
    atomic_write_json(project_dir / "attribution_audit.json", final_report)
    recovery = {
        "repair_version": "focused-conversation-batch-v3",
        "completed_at": datetime.now(UTC).isoformat(),
        "backup_dir": str(backup_dir),
        "changed_chapters": changed_chapters,
        "initial_report": initial_report,
        "final_report": final_report,
        "metrics": repair_metrics,
    }
    atomic_write_json(project_dir / "attribution_repair.json", recovery)
    shutil.rmtree(candidate_dir, ignore_errors=True)
    pipeline.job_queue.update_job(
        args.project_id,
        {
            "attribution_audit_passed": True,
            "attribution_repair_pending_chapters": changed_chapters,
            "error_message": None,
        },
    )
    print(json.dumps(recovery, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
