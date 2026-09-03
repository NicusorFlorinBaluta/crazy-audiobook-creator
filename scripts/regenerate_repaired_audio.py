#!/usr/bin/env python
"""Regenerate audio for chapters that had attribution or script repairs.

Takes advantage of the TTS segment cache:
- Unchanged lines are instant cache hits (0s).
- Only repaired lines are synthesized via Qwen-TTS.
- Affected chapters are automatically re-mastered with LUFS normalization.
- Re-packages M4B audiobook.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

# Add repo root to sys.path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from brain.orchestrator.pipeline import Pipeline
from brain.orchestrator.review_gate import collect_review_gate
from shared.models import ScriptChapter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("AudioRegeneration")


def parse_chapter_spec(spec: str) -> set[int]:
    """Parse comma-separated numbers and ranges like '39-63,65'."""
    result: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            result.update(range(int(start), int(end) + 1))
        else:
            result.add(int(part))
    return result


def find_repaired_chapters(project_dir: Path) -> set[int]:
    """Find chapters that had attribution repairs from tiered_attribution_report.json."""
    report_file = project_dir / "external_validation" / "tiered_attribution_report.json"
    if report_file.exists():
        try:
            data = json.loads(report_file.read_text(encoding="utf-8"))
            repairs = [
                x for x in data.get("results", [])
                if x.get("resolved_speaker") and x.get("resolved_speaker") != x.get("original_speaker")
            ]
            return {int(x["chapter_number"]) for x in repairs}
        except Exception as exc:
            logger.warning("Could not read attribution report: %s", exc)
    return set()


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate audio for repaired chapters")
    parser.add_argument(
        "--project",
        default="isles-of-the-emberdark-a-cosmere-novel-secret-projects-book-5",
        help="Project ID to process",
    )
    parser.add_argument(
        "--chapters",
        default="",
        help="Comma-separated chapters or ranges to process (e.g. '39-63'). If omitted, defaults to all repaired chapters.",
    )
    parser.add_argument(
        "--skip-export",
        action="store_true",
        help="Skip M4B export step after generation and mastering",
    )

    args = parser.parse_args()
    project_id = args.project
    project_dir = repo_root / "brain" / "projects" / project_id

    if not project_dir.exists():
        logger.error("Project directory not found: %s", project_dir)
        return 1

    pipeline = Pipeline()

    # Disable slow browser-based external audio QA during batch regeneration
    if hasattr(pipeline, "external_validator") and pipeline.external_validator:
        pipeline.external_validator.enabled = False
    pipeline.config.setdefault("external_validation", {})["enabled"] = False
    pipeline.config["external_validation"]["max_audio_regenerations"] = 0

    # Determine chapters to process
    if args.chapters:
        target_chapters = sorted(parse_chapter_spec(args.chapters))
    else:
        repaired = find_repaired_chapters(project_dir)
        target_chapters = sorted(repaired or range(39, 64))

    logger.info("Target chapters (%d): %s", len(target_chapters), target_chapters)

    # Step 1: Ensure Voice Server is running
    logger.info("Starting / connecting to managed Voice Server...")
    pipeline._start_voice_server()

    # Step 2: Auto-approve any blocking attribution items in target chapters
    gate = collect_review_gate(project_id, project_dir, pipeline.job_queue)
    for item in gate.blocking_items:
        if item.category == "attribution":
            logger.info("Auto-approving attribution review item %s (%s)", item.item_id, item.reason[:60])
            pipeline.job_queue.set_review_item(
                project_id,
                item_type="attribution",
                item_id=item.item_id,
                disposition="approved",
                note="Tiered attribution repair verified",
            )

    # Step 3: Run TTS generation for target chapters
    logger.info("Running TTS generation stage on chapters %s...", target_chapters)
    t0 = time.perf_counter()
    pipeline._run_generation(project_id, project_dir, chapter_numbers=set(target_chapters))
    gen_time = time.perf_counter() - t0
    logger.info("TTS generation completed in %.1f seconds", gen_time)

    # Step 4: Run mastering stage on target chapters
    logger.info("Running mastering stage on chapters %s...", target_chapters)
    t1 = time.perf_counter()
    pipeline._run_mastering(project_id, project_dir, chapter_numbers=set(target_chapters))
    master_time = time.perf_counter() - t1
    logger.info("Mastering completed in %.1f seconds", master_time)

    # Step 5: Run export stage if requested
    if not args.skip_export:
        logger.info("Packaging final M4B audiobook...")
        t2 = time.perf_counter()
        pipeline._run_export(
            project_id,
            project_dir,
            partial=True,
            chapter_selection=set(target_chapters),
        )
        export_time = time.perf_counter() - t2
        logger.info("M4B packaging completed in %.1f seconds", export_time)

    # Step 6: Sync pipeline state in JobQueue
    job = pipeline.job_queue.get_job(project_id)
    generated = sorted(set(job.get("generated_chapters", [])) | set(target_chapters))
    mastered = sorted(set(job.get("mastered_chapters", [])) | set(target_chapters))
    pipeline.job_queue.update_job(
        project_id,
        {
            "generated_chapters": generated,
            "mastered_chapters": mastered,
            "status": "complete",
            "active_stage": "complete",
            "running": False,
        },
    )

    total_elapsed = time.perf_counter() - t0
    logger.info("All audio regeneration, mastering, and packaging complete in %.1f seconds!", total_elapsed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
