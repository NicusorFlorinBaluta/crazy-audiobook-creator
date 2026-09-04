"""Regression tests for the HIP/CUDA caching-allocator configuration.

`voice_crash.log` records three crashes of the form

    HIP out of memory. Tried to allocate 3.39 GiB. GPU 0 has a total capacity
    of 23.98 GiB of which 23.33 GiB is free.

raised inside `transformers.modeling_utils.caching_allocator_warmup`. Failing a
3.4 GiB allocation with 23 GiB free is allocator fragmentation, not exhaustion,
and the error text names `expandable_segments` as the remedy. It was set
nowhere in the project.

These tests pin the value, pin that every process which loads a model receives
it, and pin the one place where the value is duplicated as a literal.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from brain.orchestrator.pipeline import Pipeline
from shared.constants import (
    TORCH_ALLOC_CONF,
    TORCH_ALLOC_ENV_VARS,
    apply_torch_alloc_conf,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class _FakeProcess:
    pid = 4321
    returncode = None

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


class TorchAllocatorConfigTests(unittest.TestCase):
    def test_both_allocator_variable_names_are_set(self) -> None:
        """ROCm reads the HIP name, upstream Torch the CUDA name.

        Setting only one leaves the value at the mercy of which name a given
        Torch build happens to honour.
        """
        env: dict[str, str] = {}
        apply_torch_alloc_conf(env)
        self.assertEqual(set(env), {"PYTORCH_CUDA_ALLOC_CONF", "PYTORCH_HIP_ALLOC_CONF"})
        for name in TORCH_ALLOC_ENV_VARS:
            self.assertEqual(env[name], TORCH_ALLOC_CONF)

    def test_expandable_segments_is_the_configured_remedy(self) -> None:
        self.assertIn("expandable_segments:True", TORCH_ALLOC_CONF)

    def test_an_operator_override_is_never_clobbered(self) -> None:
        """Someone debugging an allocator problem must be able to override it."""
        env = {"PYTORCH_HIP_ALLOC_CONF": "garbage_collection_threshold:0.8"}
        apply_torch_alloc_conf(env)
        self.assertEqual(env["PYTORCH_HIP_ALLOC_CONF"], "garbage_collection_threshold:0.8")
        self.assertEqual(env["PYTORCH_CUDA_ALLOC_CONF"], TORCH_ALLOC_CONF)

    def test_managed_voice_server_subprocess_receives_the_allocator_config(
        self,
    ) -> None:
        """The Voice Server is the process that actually crashed.

        It loads, unloads and reloads Qwen3-TTS, the VoiceDesign helper and
        Whisper within one process at every stage boundary, which is precisely
        the pattern that fragments the caching allocator's arena.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python_exe = root / "python.exe"
            python_exe.touch()

            pipeline = object.__new__(Pipeline)
            pipeline.config = {
                "voice_server": {
                    "auto_start": True,
                    "python_executable": str(python_exe),
                    "venv": str(root),
                    "startup_timeout_seconds": 5,
                }
            }
            pipeline.projects_dir = root

            class _FakeVoiceClient:
                def health_check_once(self):
                    raise ConnectionError("not started")

                def wait_for_server(self, max_wait_seconds=120):
                    return True

            pipeline.voice_client = _FakeVoiceClient()

            with (
                patch("subprocess.run") as run,
                patch("subprocess.Popen", return_value=_FakeProcess()) as popen,
            ):
                run.return_value.returncode = 0
                pipeline._start_voice_server()

            popen.assert_called_once()
            child_env = popen.call_args.kwargs["env"]
            for name in TORCH_ALLOC_ENV_VARS:
                self.assertEqual(
                    child_env.get(name),
                    TORCH_ALLOC_CONF,
                    f"{name} missing from the managed Voice Server environment",
                )

            pipeline._voice_server_log_handle.close()
            pipeline._voice_server_log_handle = None
            pipeline._voice_server_proc = None

    def test_launcher_literals_match_the_shared_constant(self) -> None:
        """`start_app.pyw` duplicates the value as a literal, by design.

        It is deliberately dependency-free so it runs before any project import
        path exists. That makes it the one place the value can silently drift,
        so assert the duplication stays honest.
        """
        launcher = (REPO_ROOT / "start_app.pyw").read_text(encoding="utf-8")
        self.assertIn(
            TORCH_ALLOC_CONF,
            launcher,
            "start_app.pyw no longer sets the shared allocator value",
        )
        for name in TORCH_ALLOC_ENV_VARS:
            self.assertIn(
                name,
                launcher,
                f"start_app.pyw no longer sets {name}",
            )

    def test_dashboard_sets_the_allocator_before_importing_torch_users(
        self,
    ) -> None:
        """Order matters: the variable is read when Torch initialises.

        Setting it after an import that has already pulled in Torch is a no-op,
        so pin that it happens above the project imports rather than merely
        somewhere in the file.
        """
        source = (REPO_ROOT / "brain" / "dashboard" / "api" / "main.py").read_text(encoding="utf-8")
        alloc_at = source.find("TORCH_ALLOC_ENV_VARS")
        self.assertNotEqual(alloc_at, -1, "allocator config absent from main.py")

        first_project_import = min(
            (match.start() for match in re.finditer(r"^from (?:brain|voice)\.", source, re.MULTILINE)),
            default=-1,
        )
        self.assertNotEqual(first_project_import, -1)
        self.assertLess(
            alloc_at,
            first_project_import,
            "allocator config must be set before the first brain/voice import",
        )


if __name__ == "__main__":
    unittest.main()
