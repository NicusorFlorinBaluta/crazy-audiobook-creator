"""Fast synthetic canary for the review pause -> decision -> release workflow."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brain.orchestrator.job_queue import JobQueue
from brain.orchestrator.review_gate import collect_review_gate, write_release_report


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="audiobook-review-canary-") as temp:
        root = Path(temp)
        project = root / "canary"
        (project / "script").mkdir(parents=True)
        (project / "script" / "chapter_001.json").write_text(
            json.dumps({"chapter_number": 1, "lines": []}), encoding="utf-8"
        )
        queue = JobQueue(str(root / "state.db"))
        queue.create_job("canary", {"status": "waiting_for_review"})
        queue.set_review_item("canary", "segment", "line-1", "unreviewed", "synthetic uncertainty")
        assert len(collect_review_gate("canary", project, queue).blocking_items) == 1
        queue.set_review_item("canary", "segment", "line-1", "acceptable", "synthetic approval")
        report = write_release_report("canary", project, queue)
        assert report["release_ready"] is True
        print("PASS: waiting_for_review -> human decision -> pre-master release")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
