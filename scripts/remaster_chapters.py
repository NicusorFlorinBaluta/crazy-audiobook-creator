r"""Re-assemble specific chapters after their segments changed underneath them.

    python scripts/remaster_chapters.py <project_id> 2 4 7 8 9

`repair_outlier_lines.py` replaces individual takes, which leaves each affected
chapter's master stale on purpose -- the stored `segment_manifest_hash` no
longer matches the segment manifest it was built from. Until the chapter is
re-assembled, the repaired audio exists on disk but no delivery contains it.

This calls the pipeline's own `_run_mastering` rather than rebuilding a
`MasterChapterRequest` by hand, because the master manifest carries a
`dependency_hash` derived from the request itself; computing that anywhere else
is how the two drift apart and every chapter starts looking stale forever.

Mastering is assembly and normalisation, not synthesis, so this is minutes
rather than hours. It still needs the voice server on port 8100.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain.orchestrator.pipeline import Pipeline


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project_id")
    parser.add_argument("chapters", nargs="+", type=int, help="chapter numbers to re-master")
    args = parser.parse_args()

    pipeline = Pipeline()
    if "schedule" in pipeline.config:
        pipeline.config["schedule"]["enabled"] = False
    project_dir = pipeline.projects_dir / args.project_id
    if not project_dir.is_dir():
        print(f"no such project: {project_dir}", file=sys.stderr)
        return 1

    chapters = sorted(set(args.chapters))
    print(f"re-mastering {args.project_id} chapters {chapters}")
    for c in chapters:
        m_path = project_dir / "manifests" / f"chapter_{c:03d}.master.json"
        if m_path.is_file():
            m_path.unlink()
    pipeline._run_mastering(args.project_id, project_dir, set(chapters))
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
