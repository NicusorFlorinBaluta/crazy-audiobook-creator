"""External-validation events must not be able to grow the database without bound.

Measured 2026-09-04: `pipeline_state.db` had reached 2.4 GB, and 2.27 GB of it
was the `reason` column of `external_validation_events`. Of 7,742 rows, 7,642
were under 1 KB; the other 100 held Playwright failures from the `gemini_web`
tier -- "Locator.fill: Target page, context or browser has been closed"
followed by Playwright's Call log, which expands without limit. The largest
single value was 109,165,541 bytes, and the identical string was written once
per affected line.

One browser failure during one chapter therefore cost about a gigabyte.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from brain.orchestrator.job_queue import JobQueue


class EventTextBoundsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = str(Path(self._tmp.name) / "state.db")
        self.queue = JobQueue(db_path=self.db)
        self.queue.create_job("p", {"title": "T"})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _stored(self) -> tuple[str, str]:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT reason, details FROM external_validation_events").fetchone()
        finally:
            conn.close()

    def _log(self, reason: str, details: dict | None = None) -> None:
        self.queue.log_external_validation(
            "p", "attribution", "ch01_0001", "gemini_web", "web", "failed", None, reason, details=details
        )

    def test_a_playwright_sized_reason_cannot_reach_the_database(self) -> None:
        """The exact shape that produced the 2.27 GB column."""
        self._log("Locator.fill: Target page, context or browser has been closed\n" + ("call log line\n" * 400_000))
        reason, _ = self._stored()
        self.assertLessEqual(len(reason), JobQueue.MAX_EVENT_REASON_CHARS + 64)

    def test_the_diagnosis_survives_the_trim(self) -> None:
        """Truncating must keep the part that says what went wrong."""
        self._log("Locator.fill: Target page, context or browser has been closed\n" + ("x" * 5_000_000))
        reason, _ = self._stored()
        self.assertIn("Locator.fill", reason)
        self.assertIn("browser has been closed", reason)

    def test_a_trimmed_value_says_it_was_trimmed(self) -> None:
        """A silently cut string reads as the whole story and misleads."""
        self._log("y" * 5_000_000)
        reason, _ = self._stored()
        self.assertIn("truncated", reason)
        self.assertIn("chars", reason)

    def test_ordinary_reasons_are_untouched(self) -> None:
        """The 7,642 well-behaved rows must round-trip byte for byte."""
        original = "Gemini resolved the speaker from the preceding narrator tag."
        self._log(original)
        reason, _ = self._stored()
        self.assertEqual(reason, original)

    def test_details_are_bounded_and_ordinary_details_still_parse(self) -> None:
        self._log("short", details={"blob": "z" * 500_000})
        _, details = self._stored()
        self.assertLessEqual(len(details), JobQueue.MAX_EVENT_DETAILS_CHARS + 64)

        self.queue.log_external_validation(
            "p", "attribution", "ch01_0002", "gemini_api", "flash", "resolved", 0.9, "ok", details={"a": 1}
        )
        conn = sqlite3.connect(self.db)
        try:
            stored = conn.execute(
                "SELECT details FROM external_validation_events WHERE item_id = 'ch01_0002'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(json.loads(stored), {"a": 1})


if __name__ == "__main__":
    unittest.main()
