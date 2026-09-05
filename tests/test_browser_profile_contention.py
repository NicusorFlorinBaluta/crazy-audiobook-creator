"""The Gemini browser tier must queue for its profile, not fail on contention.

Measured across 794 recorded `gemini_web` failures on 2026-09-05:

    499  circuit is cooling down for Ns after repeated failures   (consequence)
    117  Gemini browser profile is already in use                 (cause)
     80  Locator.fill: Timeout 30000ms exceeded
     69  Playwright is not installed                              (historical)
     20  Target page, context or browser has been closed

`SingleInstanceLock.acquire()` does not block, so a second caller arriving
while a conversation was in flight failed instantly. Three such failures trip
the provider circuit into a 900-second cooldown, so those 117 avoidable
failures generated 499 further rejections and pushed work to manual review
that the browser tier could have resolved.
"""

from __future__ import annotations

import logging
import threading
import time
import unittest
import uuid

from brain.validators.gemini_validation import GeminiWebClient
from shared.single_instance import SingleInstanceLock


class QuietAcquireTests(unittest.TestCase):
    """A caller that is deliberately polling must not warn on every attempt."""

    def setUp(self) -> None:
        self.name = f"contention-test-{uuid.uuid4().hex}.lock"
        self.held = SingleInstanceLock(self.name)
        self.assertTrue(self.held.acquire())

    def tearDown(self) -> None:
        self.held.release()

    def test_quiet_contention_does_not_warn(self) -> None:
        other = SingleInstanceLock(self.name)
        with self.assertLogs("shared.single_instance", level=logging.DEBUG) as captured:
            self.assertFalse(other.acquire(quiet=True))
        self.assertTrue(all(record.levelno == logging.DEBUG for record in captured.records), captured.output)

    def test_loud_contention_still_warns(self) -> None:
        """The default must not change: a one-shot check still reports clearly."""
        other = SingleInstanceLock(self.name)
        with self.assertLogs("shared.single_instance", level=logging.WARNING) as captured:
            self.assertFalse(other.acquire())
        self.assertTrue(captured.records)


class ProfileQueueTests(unittest.TestCase):
    def test_a_second_caller_waits_and_then_proceeds(self) -> None:
        """The behaviour the 117 failures needed: queue, do not give up."""
        name = f"contention-test-{uuid.uuid4().hex}.lock"
        first = SingleInstanceLock(name)
        self.assertTrue(first.acquire())

        outcome: dict[str, object] = {}

        def queued() -> None:
            lock = SingleInstanceLock(name)
            started = time.monotonic()
            deadline = started + 10
            while not lock.acquire(quiet=True):
                if time.monotonic() >= deadline:
                    outcome["result"] = "gave up"
                    return
                time.sleep(0.2)
            outcome["result"] = "acquired"
            outcome["waited"] = time.monotonic() - started
            lock.release()

        worker = threading.Thread(target=queued)
        worker.start()
        time.sleep(1.0)
        self.assertNotIn("result", outcome, "the second caller should still be waiting, not failed")
        first.release()
        worker.join(timeout=15)

        self.assertEqual(outcome.get("result"), "acquired")
        self.assertGreaterEqual(float(outcome.get("waited", 0)), 0.9, "it should have actually waited")

    def test_the_wait_is_bounded_and_configurable(self) -> None:
        """A wedged profile must surface, not hang the pipeline forever."""
        self.assertEqual(GeminiWebClient({"enabled": True})._profile_wait_seconds, 240.0)
        self.assertEqual(
            GeminiWebClient({"enabled": True, "profile_wait_seconds": 30})._profile_wait_seconds,
            30.0,
        )


if __name__ == "__main__":
    unittest.main()
