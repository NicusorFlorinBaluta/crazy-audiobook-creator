"""One-time, spoiler-free authentication for the Gemini web fallback profile."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _project_python() -> Path | None:
    """Return the dashboard interpreter where browser escalation actually runs."""
    dashboard_python = REPO_ROOT / "venv" / "Scripts" / "python.exe"
    if dashboard_python.is_file():
        return dashboard_python

    # Older installations may not have the lightweight dashboard venv. The
    # recorded runtime is a safe fallback, but is normally the separate voice
    # environment and therefore must not take precedence over ``venv``.
    runtime_path = REPO_ROOT / "runtime-environment.json"
    if runtime_path.is_file():
        try:
            executable = json.loads(runtime_path.read_text(encoding="utf-8")).get("python", {}).get("executable")
            if executable and Path(executable).is_file():
                return Path(executable)
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    return None


def _handoff_to_project_python() -> int | None:
    """Relaunch through the environment where Playwright was installed."""
    project_python = _project_python()
    if project_python is None:
        return None
    try:
        current = Path(sys.executable).resolve()
        target = project_python.resolve()
    except OSError:
        return None
    if current == target:
        return None
    completed = subprocess.run(
        [str(target), str(Path(__file__).resolve()), *sys.argv[1:]],
        cwd=REPO_ROOT,
        check=False,
    )
    return int(completed.returncode)


def _resolve_repo_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def _find_chrome(configured: str = "") -> Path | None:
    """Locate a normal Chrome installation for interactive authentication."""
    candidates = []
    if configured:
        candidates.append(Path(configured))
    discovered = shutil.which("chrome") or shutil.which("chrome.exe")
    if discovered:
        candidates.append(Path(discovered))
    for environment_name, suffix in (
        ("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
        ("PROGRAMFILES(X86)", "Google/Chrome/Application/chrome.exe"),
        ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
    ):
        import os

        base = os.getenv(environment_name)
        if base:
            candidates.append(Path(base) / suffix)
    return next((path for path in candidates if path.is_file()), None)


def main() -> int:
    handoff_result = _handoff_to_project_python()
    if handoff_result is not None:
        return handoff_result

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="brain/config.yaml")
    parser.add_argument("--chrome", default="", help="Optional path to chrome.exe")
    args = parser.parse_args()
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit(
            "PyYAML is unavailable and no usable project interpreter was found in runtime-environment.json."
        ) from exc
    config_path = _resolve_repo_path(args.config)
    if not config_path.is_file():
        raise SystemExit(f"Brain configuration not found: {config_path}")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    browser = config.get("external_validation", {}).get("browser", {})
    profile = _resolve_repo_path(browser.get("profile_dir", "brain/projects/.gemini-browser-profile"))
    profile.mkdir(parents=True, exist_ok=True)
    chrome = _find_chrome(args.chrome or str(browser.get("chrome_executable", "")))
    if chrome is None:
        raise SystemExit("Google Chrome was not found. Pass its location with --chrome C:\\path\\to\\chrome.exe")

    print("A normal, non-automated Chrome window will open with a dedicated Gemini profile.")
    print("Sign in and select the premium Pro model, then close that Chrome window.")
    print("No audiobook text or audio is sent during this setup.")
    process = subprocess.Popen(
        [
            str(chrome),
            f"--user-data-dir={profile}",
            "--profile-directory=Default",
            "--no-first-run",
            "--disable-background-mode",
            "https://gemini.google.com/app",
        ],
        cwd=REPO_ROOT,
    )
    input("After sign-in, Pro selection, and closing the dedicated Chrome window, press Enter here: ")
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired as exc:
            raise SystemExit(
                "The dedicated Chrome profile is still in use. Close it from the Chrome tray icon, then run setup again."
            ) from exc
    cookie_candidates = (
        profile / "Default" / "Network" / "Cookies",
        profile / "Default" / "Cookies",
    )
    if not any(path.is_file() for path in cookie_candidates):
        raise SystemExit(
            "Chrome closed, but the dedicated profile has no cookie store. Sign-in was not saved; run setup again."
        )
    print(f"Gemini browser profile saved at {profile.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
