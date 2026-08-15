from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from brain.director.ollama_client import OllamaClient, OllamaError
from brain.orchestrator.pipeline import Pipeline


class _FakeStreamResponse:
    status_code = 200

    def __init__(self, owner: OllamaClient):
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        return None

    def iter_lines(self):
        yield json.dumps(
            {"message": {"content": "first"}, "done": False}
        )
        self.owner.cancel_current()
        yield json.dumps(
            {"message": {"content": "second"}, "done": False}
        )


class _FakeHttpClient:
    def __init__(self, owner: OllamaClient):
        self.owner = owner

    def stream(self, *args, **kwargs):
        return _FakeStreamResponse(self.owner)

    def close(self):
        return None


class _FailingHttpClient:
    def __init__(self):
        self.calls = 0

    def stream(self, *args, **kwargs):
        self.calls += 1
        raise httpx.ReadTimeout("stalled")

    def close(self):
        return None


class _ModelResponse:
    def __init__(self, status_code: int, *, content: str = ""):
        self.status_code = status_code
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://test/api/chat")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("model unavailable", request=request, response=response)

    def iter_lines(self):
        if self.content:
            yield json.dumps({"message": {"content": self.content}, "done": True})


class _ModelSelectionHttpClient:
    def __init__(self, installed: list[str]):
        self.installed = installed
        self.requested_models: list[str] = []

    def stream(self, *args, **kwargs):
        model = kwargs["json"]["model"]
        self.requested_models.append(model)
        if model == "missing:32b":
            return _ModelResponse(404)
        return _ModelResponse(200, content="selected")

    def get(self, *args, **kwargs):
        request = httpx.Request("GET", "http://test/api/tags")
        return httpx.Response(
            200,
            request=request,
            json={"models": [{"name": name} for name in self.installed]},
        )

    def close(self):
        return None


class _FakeManagedOllama:
    host = "http://127.0.0.1:11435"
    model = "test:latest"

    def __init__(self):
        self.health_checks = 0

    def check_health(self, *, quiet=False):
        self.health_checks += 1
        return self.health_checks >= 2


class _FakeProcess:
    returncode = None

    def poll(self):
        return None

    def terminate(self):
        return None

    def wait(self, timeout=None):
        return 0

    def kill(self):
        return None


class OllamaLifecycleTests(unittest.TestCase):
    def test_missing_primary_model_fails_closed_without_allowlist(self) -> None:
        client = OllamaClient(model="missing:32b", max_retries=2)
        client._client.close()
        fake = _ModelSelectionHttpClient(["deepseek-r1:32b"])
        client._client = fake

        with self.assertRaisesRegex(
            OllamaError,
            "no explicitly configured fallback",
        ):
            client.generate("test")

        self.assertEqual(client.model, "missing:32b")
        self.assertEqual(fake.requested_models, ["missing:32b"])

    def test_only_explicitly_allowed_installed_fallback_can_be_selected(self) -> None:
        client = OllamaClient(
            model="missing:32b",
            fallback_models=["qwen2.5:32b"],
            max_retries=2,
        )
        client._client.close()
        fake = _ModelSelectionHttpClient([
            "deepseek-r1:32b",
            "qwen2.5:32b",
        ])
        client._client = fake

        self.assertEqual(client.generate("test"), "selected")
        self.assertEqual(client.model, "qwen2.5:32b")
        self.assertEqual(
            fake.requested_models,
            ["missing:32b", "qwen2.5:32b"],
        )

    def test_health_check_requires_the_configured_tag_not_just_model_family(self) -> None:
        client = OllamaClient(model="qwen2.5:32b")
        client._client.close()
        client._client = _ModelSelectionHttpClient(["qwen2.5:14b"])

        self.assertFalse(client.check_health(quiet=True))

    def test_failed_requests_stop_when_retry_budget_is_exhausted(self) -> None:
        client = OllamaClient(max_retries=5, max_retry_seconds=10)
        client._client.close()
        failing = _FailingHttpClient()
        client._client = failing

        with (
            patch(
                "brain.director.ollama_client.time.monotonic",
                side_effect=[0.0, 0.0, 11.0],
            ),
            self.assertRaises(OllamaError),
        ):
            client.generate("test")

        self.assertEqual(failing.calls, 1)

    def test_streaming_generation_is_cooperatively_interruptible(self) -> None:
        client = OllamaClient(max_retries=1)
        client._client.close()
        client._client = _FakeHttpClient(client)

        with self.assertRaisesRegex(KeyboardInterrupt, "cancelled"):
            client.generate("test")

    def test_pipeline_stop_interrupts_ollama_immediately(self) -> None:
        pipeline = object.__new__(Pipeline)
        pipeline._stop_flags = {}

        class FakeOllama:
            cancelled = False
            forced = False

            def cancel_current(self, *, force=False):
                self.cancelled = True
                self.forced = force

        pipeline.ollama = FakeOllama()
        pipeline.stop("book")

        self.assertTrue(pipeline._stop_flags["book"])
        self.assertTrue(pipeline.ollama.cancelled)
        self.assertTrue(pipeline.ollama.forced)

    def test_managed_voice_server_launches_then_waits_for_readiness(self) -> None:
        class FakeVoiceClient:
            waited = False

            def health_check_once(self):
                raise ConnectionError("not started")

            def wait_for_server(self, max_wait_seconds=120):
                self.waited = True
                self.max_wait_seconds = max_wait_seconds
                return True

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
                    "startup_timeout_seconds": 37,
                }
            }
            pipeline.projects_dir = root
            pipeline.voice_client = FakeVoiceClient()

            with (
                patch("subprocess.run") as run,
                patch("subprocess.Popen", return_value=_FakeProcess()) as popen,
            ):
                run.return_value.returncode = 0
                pipeline._start_voice_server()

            popen.assert_called_once()
            self.assertTrue(pipeline.voice_client.waited)
            self.assertEqual(pipeline.voice_client.max_wait_seconds, 37)
            pipeline._voice_server_log_handle.close()
            pipeline._voice_server_log_handle = None
            pipeline._voice_server_proc = None

    def test_managed_ollama_receives_discrete_vulkan_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "ollama.exe"
            executable.touch()
            pipeline = object.__new__(Pipeline)
            pipeline.config = {
                "ollama": {
                    "auto_start": True,
                    "executable": str(executable),
                    "models_dir": r"E:\.ollama\models",
                    "vulkan_visible_devices": "0",
                    "startup_timeout_seconds": 1,
                }
            }
            pipeline.ollama = _FakeManagedOllama()
            pipeline.projects_dir = Path(directory)
            pipeline._ollama_server_proc = None
            pipeline._ollama_server_log_handle = None
            captured = {}

            def fake_popen(command, **kwargs):
                captured["command"] = command
                captured["env"] = kwargs["env"]
                return _FakeProcess()

            with patch("subprocess.Popen", side_effect=fake_popen):
                pipeline._start_ollama_server()

            self.assertEqual(
                captured["env"]["GGML_VK_VISIBLE_DEVICES"],
                "0",
            )
            self.assertEqual(
                captured["env"]["OLLAMA_MODELS"],
                r"E:\.ollama\models",
            )
            self.assertEqual(
                captured["env"]["OLLAMA_HOST"],
                "127.0.0.1:11435",
            )
            pipeline._stop_ollama_server()


if __name__ == "__main__":
    unittest.main()
