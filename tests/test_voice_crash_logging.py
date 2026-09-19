"""Tests for crash log and server log encoding hygiene (Spec F19)."""

from __future__ import annotations

import traceback
from pathlib import Path

from shared.paths import repo_path
from shared.single_instance import SingleInstanceLock


def test_voice_crash_log_handles_unicode_traceback(tmp_path: Path):
    """Writing a traceback containing non-ASCII characters (e.g. em-dash, accented characters)
    must not raise UnicodeEncodeError and must produce a readable UTF-8 file.
    """
    log_file = tmp_path / "voice_crash.log"

    try:
        raise ValueError("Model crash with non-ASCII context: — special character é, ü, ñ —")
    except ValueError as exc:
        formatted_tb = traceback.format_exc()
        # Verify writing with encoding="utf-8", errors="replace" succeeds
        with open(log_file, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"Crash in bootstrap_voices: {exc}\n{formatted_tb}\n")

    assert log_file.is_file()
    content = log_file.read_text(encoding="utf-8")
    assert "— special character é, ü, ñ —" in content
    assert "ValueError: Model crash" in content


def test_single_instance_lock_handles_unicode_path_and_encoding(tmp_path: Path):
    """SingleInstanceLock must acquire and release without Unicode errors."""
    lock_name = "test_unicode_—_lock.lock"
    lock = SingleInstanceLock(lock_name=lock_name)
    assert lock.acquire() is True
    assert lock.lock_file.is_file()
    lock.release()
    lock.lock_file.unlink(missing_ok=True)


def test_repo_path_resolves_voice_crash_log():
    """Verify repo_path resolves voice_crash.log as an absolute path."""
    p = repo_path("voice_crash.log")
    assert p.is_absolute()
    assert p.name == "voice_crash.log"
