"""One-time, spoiler-free authentication for the Gemini web fallback profile."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="brain/config.yaml")
    args = parser.parse_args()
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    browser = config.get("external_validation", {}).get("browser", {})
    profile = Path(browser.get("profile_dir", "brain/projects/.gemini-browser-profile"))
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
