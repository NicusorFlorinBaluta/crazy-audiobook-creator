r"""Repackage deliveries whose chapters changed underneath them.

    python scripts/reexport_deliveries.py <project_id> --batch 1-5 --batch 6-10
    python scripts/reexport_deliveries.py <project_id> --full
    python scripts/reexport_deliveries.py <project_id> --stale        # detect

Re-mastering a chapter updates `chapters/chapter_NNN.wav`. It does **not**
touch the M4B files, which are what anyone actually plays, so a repair can be
complete in the workspace and entirely absent from the delivery. That gap is
easy to miss because every intermediate check passes: segments repaired,
manifests consistent, masters reconciled, measurement improved -- and the
audiobook on disk unchanged.

`--stale` lists deliveries older than a chapter they contain, which is the
check worth running after any repair.

Exports go through the pipeline's own `_run_export`, so attribution gating,
delivery locking and revision bookkeeping behave exactly as they do in a normal
run. Needs the voice server on port 8100.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain.orchestrator.pipeline import Pipeline

_BATCH = re.compile(r"^(\d+)-(\d+)$")


def _parse_batch(value: str) -> set[int]:
    match = _BATCH.match(value.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"expected a range like 1-5, got {value!r}")
    first, last = int(match.group(1)), int(match.group(2))
    if last < first:
        raise argparse.ArgumentTypeError(f"range runs backwards: {value!r}")
    return set(range(first, last + 1))


def _report_stale(project_id: str, workspace: Path) -> int:
    """Deliveries older than a chapter wav they contain."""
    chapters_dir = workspace / "chapters"
    output_dir = workspace / "output"
    if not chapters_dir.is_dir() or not output_dir.is_dir():
        print("nothing to check", file=sys.stderr)
        return 1

    mtimes = {
        int(path.stem.split("_")[1]): path.stat().st_mtime
        for path in chapters_dir.glob("chapter_*.wav")
        if path.stem.split("_")[-1].isdigit()
    }
    stale = False
    for delivery in sorted(output_dir.glob("*.m4b")):
        built = delivery.stat().st_mtime
        match = re.search(r"_chapters_(\d+)-(\d+)\.m4b$", delivery.name)
        covered = set(range(int(match.group(1)), int(match.group(2)) + 1)) if match else set(mtimes)
        newer = sorted(number for number in covered if mtimes.get(number, 0) > built)
        if newer:
            stale = True
            print(f"STALE  {delivery.name}  (chapters re-mastered since it was built: {newer})")
        else:
            print(f"ok     {delivery.name}")
    if stale:
        print("\nRe-export the stale ones, or the repaired audio never reaches a listener.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project_id")
    parser.add_argument("--batch", action="append", type=_parse_batch, default=[], help="chapter range, e.g. 1-5")
    parser.add_argument("--full", action="store_true", help="also rebuild the whole-book M4B")
    parser.add_argument("--stale", action="store_true", help="only report which deliveries are out of date")
    args = parser.parse_args()

    pipeline = Pipeline()
    project_dir = pipeline.projects_dir / args.project_id
    if not project_dir.is_dir():
        print(f"no such project: {project_dir}", file=sys.stderr)
        return 1

    if args.stale:
        return _report_stale(args.project_id, pipeline.workspace_dir / args.project_id)
    if not args.batch and not args.full:
        parser.error("give at least one --batch, or --full, or --stale")

    for selection in args.batch:
        print(f"re-exporting chapters {min(selection)}-{max(selection)}")
        pipeline._run_export(args.project_id, project_dir, partial=True, chapter_selection=selection)
    if args.full:
        print("re-exporting the full book")
        pipeline._run_export(args.project_id, project_dir, partial=False)
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
