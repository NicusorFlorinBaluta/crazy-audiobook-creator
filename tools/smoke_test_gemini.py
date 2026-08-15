"""Spoiler-free live connectivity checks for Gemini API and browser fallbacks."""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import tempfile
import wave
from pathlib import Path

import yaml

from brain.validators.gemini_validation import GeminiApiClient, GeminiWebClient


ROOT = Path(__file__).resolve().parents[1]


def _load_env() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _config() -> dict:
    return yaml.safe_load((ROOT / "brain/config.yaml").read_text(encoding="utf-8"))[
        "external_validation"
    ]


def inspect_browser_controls(browser: dict) -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(ROOT / browser["profile_dir"]),
            channel=browser.get("channel", "chrome"),
            headless=True,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(
                "https://gemini.google.com/app",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            page.wait_for_timeout(3000)
            controls = page.locator("button,[role=button]").evaluate_all(
                """elements => elements.map(element => ({
                    aria: element.getAttribute('aria-label') || '',
                    title: element.getAttribute('title') || '',
                    text: (element.innerText || '').trim().slice(0, 80),
                    tag: element.tagName,
                    classes: String(element.className || '').slice(0, 160)
                })).filter(item => /model|pro|flash|gemini|mode|upload|attach|add file/i.test(
                    `${item.aria} ${item.title} ${item.text}`
                )).slice(0, 30)"""
            )
            editors = page.locator('textarea,[contenteditable="true"]').evaluate_all(
                """elements => elements.map(element => {
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    return {
                        aria: element.getAttribute('aria-label') || '',
                        placeholder: element.getAttribute('placeholder') || '',
                        tag: element.tagName,
                        classes: String(element.className || '').slice(0, 160),
                        visible: rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none'
                    };
                }).filter(item => item.visible)"""
            )
            upload_tools = page.get_by_role("button", name="Upload & tools")
            if upload_tools.count():
                upload_tools.last.click()
                page.wait_for_timeout(500)
            attachment_controls = page.locator("button,[role=button],[role=menuitem]").evaluate_all(
                """elements => elements.map(element => ({
                    aria: element.getAttribute('aria-label') || '',
                    title: element.getAttribute('title') || '',
                    text: (element.innerText || '').trim().slice(0, 80),
                    role: element.getAttribute('role') || '',
                    tag: element.tagName,
                    classes: String(element.className || '').slice(0, 160)
                })).filter(item => /upload|attach|file|computer/i.test(
                    `${item.aria} ${item.title} ${item.text}`
                )).slice(0, 30)"""
            )
            file_inputs = page.locator('input[type="file"]').evaluate_all(
                """elements => elements.map(element => ({
                    aria: element.getAttribute('aria-label') || '',
                    accept: element.getAttribute('accept') || '',
                    multiple: element.multiple,
                    classes: String(element.className || '').slice(0, 160)
                }))"""
            )
            print(json.dumps({
                "url_host": page.url.split("/", 3)[:3],
                "controls": controls,
                "visible_editors": editors,
                "file_inputs": file_inputs,
                "attachment_controls": attachment_controls,
            }, indent=2))
        finally:
            context.close()
    return 0


def smoke_api(config: dict) -> None:
    api = config["api"]
    schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["ok"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["status", "confidence"],
    }
    with tempfile.TemporaryDirectory(prefix="gemini-api-smoke-") as directory:
        candidate = Path(directory) / "candidate.wav"
        reference = Path(directory) / "reference.wav"
        _write_tone(candidate, 440.0)
        _write_tone(reference, 440.0)
        client = GeminiApiClient(api, ROOT / "brain/projects")
        triage = client.generate_json(
            model=api["triage_model"],
            prompt=(
                "Spoiler-free multimodal connectivity test using synthetic matching tones. "
                "Return status ok and confidence 1."
            ),
            schema=schema,
            audio_path=candidate,
            reference_audio_path=reference,
        )
        adjudication = client.generate_json(
            model=api["adjudication_model"],
            prompt="Spoiler-free adjudication connectivity test. Return status ok and confidence 1.",
            schema=schema,
        )
    print("API:", json.dumps({
        "triage_audio": triage,
        "adjudication": adjudication,
    }))


def smoke_browser(config: dict) -> None:
    with tempfile.TemporaryDirectory(prefix="gemini-smoke-") as directory:
        project_dir = Path(directory)
        candidate = project_dir / "candidate.wav"
        reference = project_dir / "reference.wav"
        _write_tone(candidate, 440.0)
        _write_tone(reference, 440.0)
        client = GeminiWebClient(config["browser"])
        first = client.generate_json(
            project_dir,
            "connectivity_smoke_test",
            'Spoiler-free connectivity test. Return only JSON exactly as {"status":"ok","confidence":1}.',
        )
        first_state = json.loads(
            (project_dir / "external_validation/browser_state.json").read_text(encoding="utf-8")
        )["conversations"]["connectivity_smoke_test"]
        second = client.generate_json(
            project_dir,
            "connectivity_smoke_test",
            'These are synthetic matching tones, not audiobook audio. Return only JSON exactly as {"status":"ok","confidence":1}.',
            audio_path=candidate,
            reference_audio_path=reference,
        )
        second_state = json.loads(
            (project_dir / "external_validation/browser_state.json").read_text(encoding="utf-8")
        )["conversations"]["connectivity_smoke_test"]
        persistent = (
            first_state["url"] == second_state["url"]
            and int(second_state["turns"]) == 2
        )
        if not persistent:
            raise RuntimeError("Browser smoke test did not reuse its saved conversation")
    print("Browser:", json.dumps({
        "first": first,
        "audio": second,
        "persistent_conversation": persistent,
        "turns": 2,
    }))


def _write_tone(path: Path, frequency: float) -> None:
    sample_rate = 16_000
    frames = bytearray()
    for index in range(sample_rate // 2):
        sample = int(0.12 * 32767 * math.sin(2 * math.pi * frequency * index / sample_rate))
        frames.extend(struct.pack("<h", sample))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(frames)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect-controls", action="store_true")
    parser.add_argument("--api-only", action="store_true")
    parser.add_argument("--browser-only", action="store_true")
    args = parser.parse_args()
    os.chdir(ROOT)
    _load_env()
    config = _config()
    if args.inspect_controls:
        return inspect_browser_controls(config["browser"])
    if not args.browser_only:
        smoke_api(config)
    if not args.api_only:
        smoke_browser(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
