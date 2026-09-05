"""Run real audio stages for one existing, already-scripted sample chapter."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from brain.orchestrator.pipeline import Pipeline, _WaitingForReview
from brain.orchestrator.review_gate import write_release_report
from shared.constants import PipelineStage
from shared.single_instance import SingleInstanceLock


def _move_if_present(path: Path, backup_root: Path) -> None:
    if not path.exists():
        return
    destination = backup_root / path.resolve().relative_to(REPO_ROOT.resolve())
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copy2(path, destination)
    path.chmod(stat.S_IREAD | stat.S_IWRITE)
    path.unlink()


def _load_env() -> None:
    """Load repo-local credentials without overwriting caller-provided values."""
    path = REPO_ROOT / ".env"
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id")
    parser.add_argument("chapter", type=int)
    parser.add_argument(
        "--prepared",
        action="store_true",
        help="Artifacts were already invalidated through the dashboard API",
    )
    args = parser.parse_args()
    if not args.project_id.startswith("sample_book-"):
        raise SystemExit("This recovery-safe runner is restricted to sample_book-* projects")

    _load_env()
    pipeline = Pipeline()
    project_dir = (pipeline.projects_dir / args.project_id).resolve()
    chapter_path = project_dir / "script" / f"chapter_{args.chapter:03d}.json"
    if not chapter_path.is_file():
        raise SystemExit(f"Missing chapter script: {chapter_path}")
    state = pipeline.job_queue.get_job(args.project_id)
    if state.get("running"):
        raise SystemExit("Project is already running")
    chapter = json.loads(chapter_path.read_text(encoding="utf-8"))
    line_ids = [str(line.get("line_id") or line.get("id")) for line in chapter.get("lines", [])]

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = project_dir / "stage_canary_backups" / stamp
    if not args.prepared:
        for root in (REPO_ROOT / "workspace" / args.project_id, project_dir):
            for line_id in line_ids:
                for suffix in (".wav", ".pt"):
                    _move_if_present(root / "segments" / f"{line_id}{suffix}", backup)
            _move_if_present(root / "mastered" / f"chapter_{args.chapter:03d}.wav", backup)
        for path in (
            project_dir / "manifests" / f"chapter_{args.chapter:03d}.segments.json",
            project_dir / "manifests" / f"chapter_{args.chapter:03d}.master.json",
        ):
            _move_if_present(path, backup)

    pipeline.job_queue.update_job(
        args.project_id,
        {
            "generated_chapters": [n for n in state.get("generated_chapters", []) if n != args.chapter],
            "mastered_chapters": [n for n in state.get("mastered_chapters", []) if n != args.chapter],
            "active_generation_chapter_selection": [args.chapter],
            "generation_chapter_selection": [args.chapter],
            "running": True,
            "error_message": None,
        },
    )

    lock = SingleInstanceLock("crazy-audiobook-pipeline.lock")
    if not lock.acquire():
        raise SystemExit("Another pipeline worker owns the GPU lease")
    try:
        pipeline._start_voice_server()
        pipeline._run_generation(args.project_id, project_dir, {args.chapter})
        release = write_release_report(args.project_id, project_dir, pipeline.job_queue)
        if not release["release_ready"]:
            pipeline._update_stage(args.project_id, PipelineStage.WAITING_FOR_REVIEW)
            print(json.dumps({"status": "waiting_for_review", "release": release}, indent=2))
            return 2
        pipeline._run_mastering(args.project_id, project_dir, {args.chapter})
        output = project_dir / "stage_canary" / f"chapter-{args.chapter:03d}-partial.m4b"
        output.parent.mkdir(parents=True, exist_ok=True)
        pipeline._run_export(
            args.project_id,
            project_dir,
            partial=True,
            chapter_selection={args.chapter},
            temp_output=output,
        )
        pipeline._update_stage(args.project_id, PipelineStage.SELECTION_COMPLETE)
        print(json.dumps({"status": "complete", "output": str(output), "backup": str(backup)}, indent=2))
        return 0
    except _WaitingForReview as review_pause:
        pipeline._update_stage(args.project_id, PipelineStage.WAITING_FOR_REVIEW)
        pipeline.job_queue.update_job(
            args.project_id,
            {
                "pause_reason": review_pause.reason,
                "review_blocking_item_ids": review_pause.item_ids,
                "error_message": None,
            },
        )
        print(
            json.dumps(
                {
                    "status": "waiting_for_review",
                    "review_blocking_item_ids": review_pause.item_ids,
                    "reason": review_pause.reason,
                },
                indent=2,
            )
        )
        return 2
    except Exception as exc:
        pipeline.job_queue.update_job(
            args.project_id,
            {
                "status": PipelineStage.ERROR.value,
                "active_stage": PipelineStage.ERROR.value,
                "error_message": str(exc),
            },
        )
        raise
    finally:
        pipeline.job_queue.update_job(args.project_id, {"running": False})
        pipeline._stop_voice_server()
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
