# Legacy

Code kept for reference that is **not** part of the supported pipeline. Nothing
here is imported by `brain/`, `voice/` or `shared/`, and nothing here runs in a
normal pipeline execution.

| File | Why it is here |
| --- | --- |
| `parler_server.py` | The original Parler-TTS service, replaced by Qwen3-TTS. Its Transformers requirement conflicts with Qwen, so it can only be evaluated in a separate environment using `voice/requirements-legacy-parler.txt`. See [../docs/setup-windows.md](../docs/setup-windows.md). |

Do not add a dependency on anything in this directory. If something here becomes
load-bearing again, move it into `brain/`, `voice/` or `shared/` first.
