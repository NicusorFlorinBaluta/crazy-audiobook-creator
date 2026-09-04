"""Liveness contracts for the Voice server's control endpoints.

A chapter generation holds ``gpu_job_lock`` for its entire duration. The
control endpoints an operator uses to stop that work -- ``/cancel`` and
``/unload`` -- must therefore never block on it, and must not be declared
``async def`` in a way that would park the uvicorn event loop while a job runs.

These tests are deliberately source- and threading-level rather than a live
server test: they encode *why* the endpoint shapes are what they are, so a
future refactor back to a blocking ``async def`` fails loudly here.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
import unittest
import unittest.mock

from shared.models import GenerateChapterRequest, ScriptLine
from voice.tts_server import main as voice_main


def _chapter_request(project_id: str) -> GenerateChapterRequest:
    """A minimal, model-free chapter request for slot-contention tests."""
    return GenerateChapterRequest(
        project_id=project_id,
        chapter_number=1,
        lines=[ScriptLine(line_id="ch01_0001", speaker="narrator", text="Hello.")],
    )


class UnloadEndpointDoesNotBlockTheEventLoopTests(unittest.TestCase):
    def test_unload_is_synchronous_so_it_runs_off_the_event_loop(self) -> None:
        """A blocking lock acquire must not sit on the event loop.

        FastAPI runs a plain ``def`` endpoint in the threadpool. If ``/unload``
        were ``async def`` and acquired ``gpu_job_lock``, a running chapter
        would freeze ``/health``, ``/cancel`` and ``/ws/progress`` with it.
        """
        self.assertFalse(
            inspect.iscoroutinefunction(voice_main.unload_models),
            "/unload must stay a synchronous endpoint; see its docstring",
        )

    def test_unload_reports_busy_instead_of_waiting_for_the_gpu_lock(self) -> None:
        """The 409 must be reachable while a GPU job holds the lock."""
        released = threading.Event()
        holding = threading.Event()

        def hold_lock() -> None:
            with voice_main.gpu_job_lock:
                holding.set()
                released.wait(timeout=10)

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()
        try:
            self.assertTrue(holding.wait(timeout=5), "helper never took the lock")

            from fastapi import HTTPException

            with self.assertRaises(HTTPException) as caught:
                voice_main.unload_models()
            self.assertEqual(caught.exception.status_code, 409)
        finally:
            released.set()
            holder.join(timeout=5)

    def test_unload_succeeds_once_the_gpu_lock_is_free(self) -> None:
        """With no job running, unload proceeds and reports what it released."""
        self.assertIsNone(voice_main.engine)
        self.assertIsNone(voice_main.validator)
        result = voice_main.unload_models()
        self.assertEqual(result["status"], "unloaded")
        self.assertEqual(result["models"], [])
        # The lock must not be left held on the success path.
        self.assertTrue(voice_main.gpu_job_lock.acquire(blocking=False))
        voice_main.gpu_job_lock.release()

    def test_cancel_stays_async_and_only_touches_the_run_registry(self) -> None:
        """``/cancel`` must remain cheap and non-blocking.

        It is the endpoint that releases a stuck job, so it may only take the
        short-lived ``run_state_lock``, never ``gpu_job_lock``.
        """
        self.assertTrue(inspect.iscoroutinefunction(voice_main.cancel_project))
        source = inspect.getsource(voice_main.cancel_project)
        self.assertIn("run_state_lock", source)
        self.assertNotIn("gpu_job_lock", source)

    def test_cancel_is_answerable_while_a_gpu_job_holds_the_lock(self) -> None:
        released = threading.Event()
        holding = threading.Event()

        def hold_lock() -> None:
            with voice_main.gpu_job_lock:
                holding.set()
                released.wait(timeout=10)

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()
        try:
            self.assertTrue(holding.wait(timeout=5), "helper never took the lock")
            result = asyncio.run(voice_main.cancel_project("no-such-project"))
            self.assertEqual(result["status"], "idle")
        finally:
            released.set()
            holder.join(timeout=5)

    def test_health_stays_async_and_never_takes_the_gpu_lock(self) -> None:
        self.assertTrue(inspect.iscoroutinefunction(voice_main.health_check))
        self.assertNotIn("gpu_job_lock", inspect.getsource(voice_main.health_check))


class ChapterRunSlotTakeoverTests(unittest.TestCase):
    """A new chapter request must not silently queue behind a stuck run.

    The previous behaviour overwrote the run-registry entry immediately and
    started a second worker, which then blocked on `gpu_job_lock` for however
    long the incumbent took to reach a cancellation boundary -- streaming only
    keepalive newlines, with no diagnostic for the caller.
    """

    def setUp(self) -> None:
        self._saved = dict(voice_main.active_project_runs)
        voice_main.active_project_runs.clear()
        # `generate_chapter` refuses early without a validator. Supply a stub so
        # the test exercises run-slot contention, not the initialization guard.
        self._validator_patch = unittest.mock.patch.object(
            voice_main, "validator", unittest.mock.Mock()
        )
        self._validator_patch.start()

    def tearDown(self) -> None:
        self._validator_patch.stop()
        voice_main.active_project_runs.clear()
        voice_main.active_project_runs.update(self._saved)

    def test_takeover_signals_the_incumbent_before_waiting(self) -> None:
        incumbent = threading.Event()
        voice_main.active_project_runs["book"] = incumbent
        self.assertFalse(incumbent.is_set())

        from fastapi import HTTPException

        # Shorten the wait so the test does not sit for the production timeout.
        with unittest.mock.patch.object(
            voice_main, "RUN_SLOT_TAKEOVER_TIMEOUT_SECONDS", 0.5
        ), unittest.mock.patch.object(
            voice_main, "RUN_SLOT_POLL_ATTEMPTS", 2
        ), unittest.mock.patch.object(
            voice_main, "RUN_SLOT_POLL_INTERVAL_SECONDS", 0.01
        ):
            with self.assertRaises(HTTPException) as caught:
                voice_main.generate_chapter(
                    _chapter_request("book"),
                    fast_req=unittest.mock.Mock(),
                )

        self.assertEqual(caught.exception.status_code, 503)
        self.assertTrue(
            incumbent.is_set(),
            "the incumbent run must be asked to cancel even when takeover fails",
        )
        # The incumbent's own entry must survive so its `finally` can pop it.
        self.assertIs(voice_main.active_project_runs.get("book"), incumbent)

    def test_a_released_slot_is_acquired_without_error(self) -> None:
        """When the incumbent releases in time, the new run proceeds."""
        incumbent = threading.Event()
        voice_main.active_project_runs["book"] = incumbent

        def release_soon() -> None:
            time.sleep(0.05)
            with voice_main.run_state_lock:
                voice_main.active_project_runs.pop("book", None)

        threading.Thread(target=release_soon, daemon=True).start()

        with unittest.mock.patch.object(
            voice_main, "RUN_SLOT_TAKEOVER_TIMEOUT_SECONDS", 5
        ), unittest.mock.patch.object(
            voice_main, "RUN_SLOT_POLL_ATTEMPTS", 1
        ), unittest.mock.patch.object(
            voice_main, "RUN_SLOT_POLL_INTERVAL_SECONDS", 0.01
        ):
            response = voice_main.generate_chapter(
                _chapter_request("book"),
                fast_req=unittest.mock.Mock(),
            )

        self.assertIsNotNone(response)
        self.assertTrue(incumbent.is_set())


if __name__ == "__main__":
    unittest.main()
