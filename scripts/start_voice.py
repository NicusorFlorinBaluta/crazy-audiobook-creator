"""Start the local Voice API on the interpreter the project configures for it.

This replaces the legacy SSH launcher. It intentionally does not stop existing
processes or read remote-host credentials.

**It does not use `sys.executable`.** There are two interpreters on this machine
and only one of them has the audio stack: the repo venv runs the Brain, tests
and lint, and a separate ROCm venv is the only one with `torch`, `qwen-tts` and
Whisper. Launching this script from the repo venv used to start a Voice server
that could not import its own models, and the resolution order below is the
same one `Pipeline._start_voice_server` already uses, so the manual and the
automatic path now agree about which Python to run.

Note for anyone debugging ROCm here: `PyTorch: Unknown command line argument
'env\\my_venv\\...\\offload-arch.exe'` is **cosmetic** and appears whichever
path spells the venv, because the SDK builds an unquoted command from its own
install directory. It is not a sign that the wrong interpreter was picked.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def resolve_voice_python() -> tuple[Path, str]:
    """Return the interpreter to run the Voice server with, and why.

    Precedence matches the pipeline's auto-start: an explicit environment
    override, then `voice_server.python_executable` from `brain/config.yaml`, then the
    `Scripts/python.exe` of the configured venv. Falls back to the current
    interpreter only when none of those exist, and says so.
    """
    override = os.environ.get("CRAZY_AUDIOBOOK_VOICE_PYTHON")
    if override:
        return Path(override), "CRAZY_AUDIOBOOK_VOICE_PYTHON"

    try:
        import yaml

        from shared import paths as shared_paths

        config = yaml.safe_load(Path(shared_paths.BRAIN_CONFIG_PATH).read_text(encoding="utf-8")) or {}
        voice_cfg = config.get("voice_server", {}) or {}
    except (OSError, ValueError, ImportError) as exc:
        print(f"could not read brain/config.yaml ({exc}); using the current interpreter", file=sys.stderr)
        return Path(sys.executable), "current interpreter"

    configured = voice_cfg.get("python_executable")
    if configured:
        return Path(configured), "brain/config.yaml voice_server.python_executable"

    venv = voice_cfg.get("venv")
    if venv:
        return Path(venv) / "Scripts" / "python.exe", "brain/config.yaml voice_server.venv"

    return Path(sys.executable), "current interpreter"


def main() -> int:
    python_exe, source = resolve_voice_python()
    if not python_exe.is_file():
        fallback = Path(sys.executable)
        print(
            f"configured Voice interpreter is missing: {python_exe} (from {source})\n"
            f"falling back to {fallback}, which may not have the audio stack",
            file=sys.stderr,
        )
        python_exe = fallback
        source = "fallback"

    print(f"starting the Voice server with {python_exe} (from {source})")
    return subprocess.call(
        [str(python_exe), "-m", "voice.tts_server.main"],
        cwd=ROOT,
    )


if __name__ == "__main__":
    raise SystemExit(main())
