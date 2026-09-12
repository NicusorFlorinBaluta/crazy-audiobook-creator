"""Running elevated silently poisons future writes; the guard makes it loud.

A run started from an elevated shell leaves every artifact it creates owned by
`BUILTIN\\Administrators`. `os.replace` over an existing file needs the DELETE
right on the target, and `BUILTIN\\Users` does not grant it, so the normal
unelevated service can no longer rewrite those files.

Nothing about the file looks wrong: it reports as writable, `os.access(W_OK)`
returns True, and it opens exclusively without error. Only the rename fails,
much later and usually in an unrelated part of the pipeline, as a bare
`[WinError 5] Access is denied` naming a temp file.

Measured on the workstation 2026-09-04: 259 of 500 sampled files under
`brain/projects` were Administrators-owned and `atomic_write_json` against one
of them raised. After `takeown` + `icacls` every sampled file was user-owned
and the same writes succeeded -- which is what confirmed the cause.
"""

from __future__ import annotations

import logging
import unittest
from pathlib import Path
from unittest.mock import patch

from shared import paths as shared_paths

REPO_ROOT = Path(__file__).resolve().parents[1]


class _CapturingLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str, *args) -> None:
        self.warnings.append(message % args if args else message)


class ElevationGuardTests(unittest.TestCase):
    def test_an_elevated_process_is_warned_about(self) -> None:
        log = _CapturingLogger()
        with patch.object(shared_paths, "running_elevated", return_value=True):
            self.assertTrue(shared_paths.warn_if_elevated(log))
        self.assertEqual(len(log.warnings), 1)
        text = log.warnings[0]
        # The warning has to carry the consequence, not just the fact, or it
        # reads as pedantry and gets ignored.
        self.assertIn("ELEVATED", text)
        self.assertIn("Administrators", text)
        self.assertIn("WinError 5", text)
        self.assertIn("docs/setup-windows.md", text)

    def test_a_normal_process_is_not_warned(self) -> None:
        log = _CapturingLogger()
        with patch.object(shared_paths, "running_elevated", return_value=False):
            self.assertFalse(shared_paths.warn_if_elevated(log))
        self.assertEqual(log.warnings, [])

    def test_the_check_is_a_no_op_off_windows(self) -> None:
        with patch("os.name", "posix"):
            self.assertFalse(shared_paths.running_elevated())

    def test_a_real_logger_accepts_the_warning(self) -> None:
        """The message contains literal backslashes; pin that %-formatting is safe."""
        logger = logging.getLogger("elevation-guard-test")
        with (
            patch.object(shared_paths, "running_elevated", return_value=True),
            self.assertLogs(logger, level="WARNING") as captured,
        ):
            shared_paths.warn_if_elevated(logger)
        self.assertTrue(any("ELEVATED" in line for line in captured.output))

    def test_the_dashboard_calls_the_guard_at_startup(self) -> None:
        """A guard nothing invokes is not a guard."""
        source = (REPO_ROOT / "brain" / "dashboard" / "api" / "main.py").read_text(encoding="utf-8")
        self.assertIn("warn_if_elevated", source)


class ReplaceDenialHintTests(unittest.TestCase):
    def test_an_administrators_owned_target_names_the_cause_and_the_fix(self) -> None:
        from shared import artifacts

        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "BUILTIN\\Administrators\n"
            with patch("os.name", "nt"):
                hint = artifacts._replace_denial_hint(Path("x.json"))
        self.assertIn("BUILTIN\\Administrators", hint)
        self.assertIn("DELETE right", hint)
        self.assertIn("docs/setup-windows.md", hint)

    def test_a_user_owned_target_gets_a_plain_note(self) -> None:
        """Owner is still worth reporting, but do not blame elevation for it."""
        from shared import artifacts

        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "CRAZY-HOME\\nicus\n"
            with patch("os.name", "nt"):
                hint = artifacts._replace_denial_hint(Path("x.json"))
        self.assertIn("nicus", hint)
        self.assertNotIn("takeown", hint)

    def test_the_hint_never_masks_the_original_error(self) -> None:
        """If ownership cannot be read, the caller must still get the real error."""
        from shared import artifacts

        with patch("subprocess.run", side_effect=OSError("no powershell")):
            with patch("os.name", "nt"):
                self.assertEqual(artifacts._replace_denial_hint(Path("x.json")), "")


if __name__ == "__main__":
    unittest.main()
