"""One-time, spoiler-free authentication for the Gemini web fallback profile."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]


def _project_python() -> Path | None:
    """Return the recorded project interpreter without importing project dependencies."""
    runtime_path = REPO_ROOT / "runtime-environment.json"
    if runtime_path.is_file():
        try:
            executable = json.loads(runtime_path.read_text(encoding="utf-8")).get(
                "python", {}
            ).get("executable")
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


def main() -> int:
    handoff_result = _handoff_to_project_python()
    if handoff_result is not None:
        return handoff_result

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="brain/config.yaml")
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
    profile = _resolve_repo_path(
        browser.get("profile_dir", "brain/projects/.gemini-browser-profile")
    )
    profile.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "Playwright is not installed. Install brain/requirements.txt in the brain virtual environment."
        ) from exc

    print("A dedicated Gemini window will open. Sign in and select the premium Pro model.")
    print("No audiobook text or audio is sent during this setup.")
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile),
            channel=str(browser.get("channel", "chrome")),
            headless=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://gemini.google.com/app", wait_until="domcontentloaded")
        input("After Gemini is signed in and Pro is selected, press Enter here to save the profile: ")
        context.close()
    print(f"Gemini browser profile saved at {profile.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
