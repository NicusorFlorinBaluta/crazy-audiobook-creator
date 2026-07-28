"""Start the local Voice API in the current Python environment.

This replaces the legacy SSH launcher. It intentionally does not stop existing
processes or read remote-host credentials.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    return subprocess.call(
        [sys.executable, "-m", "voice.tts_server.main"],
        cwd=repo_root,
    )


if __name__ == "__main__":
    raise SystemExit(main())
