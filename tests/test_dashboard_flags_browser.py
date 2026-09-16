#!/usr/bin/env python3
"""Playwright browser end-to-end test for Playback Flags in the Crazy Audiobook Creator dashboard."""

import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

BASE_URL = "http://127.0.0.1:8000"
PROJECT_ID = "the-finest-edge-of-twilight-book"
PROJECT_DIR = Path("e:/Projects/crazy-audiobook-creator/brain/projects") / PROJECT_ID
FLAGS_JSON = PROJECT_DIR / "playback_flags.json"
DB_PATH = Path("e:/Projects/crazy-audiobook-creator/brain/projects/pipeline_state.db")
SCREENSHOT_PATH = Path("e:/Projects/crazy-audiobook-creator/scratch/dashboard_flags_e2e.png")
SCREENSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)


def run_browser_e2e():
    print(f"=== [1/7] Creating Test Playback Flag via Mobile API ({PROJECT_ID}) ===")
    post_url = f"{BASE_URL}/api/mobile/v1/books/{PROJECT_ID}/flags"
    payload = {
        "chapter_number": 3,
        "position_ms": 15000,
        "issue_type": "wrong_speaker",
        "user_note": "Playwright Browser E2E: driving flag test",
        "source": "android_auto"
    }
    req = urllib.request.Request(
        post_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status in (200, 201), f"Expected 200/201, got {resp.status}"
        create_resp = json.loads(resp.read().decode("utf-8"))

    flag = create_resp["flag"]
    flag_id = flag["flag_id"]
    print(f"  -> Flag created: {flag_id}, status = '{flag['status']}'")
    assert flag["status"] == "open", f"Expected default status 'open', got '{flag['status']}'"
    
    # Verify candidate lines in 20s reaction delay window
    candidates = flag.get("candidate_lines") or flag.get("enriched_data", {}).get("candidate_lines", [])
    print(f"  -> Found {len(candidates)} candidate lines in reaction window")
    assert len(candidates) > 0, "Expected candidate lines in the 20s reaction delay window!"
    for c in candidates:
        rel = c.get("relative_sec")
        print(f"     * [{c['line_id']}] {c['speaker']}: {c['text'][:40]}... (offset: {rel}s, is_at_tap={c.get('is_at_tap')})")

    try:
        print("\n=== [2/7] Launching Headless Browser via Playwright (Edge) ===")
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()

            # Navigate to project detail view
            target_url = f"{BASE_URL}/#project/{PROJECT_ID}"
            print(f"  -> Navigating to {target_url}")
            page.goto(target_url, wait_until="networkidle")
            page.wait_for_timeout(1500)

            print("\n=== [3/7] Opening '🚩 Playback Flags' Tab ===")
            flags_tab_btn = page.locator("#tab-button-flags")
            flags_tab_btn.wait_for(state="visible", timeout=5000)
            flags_tab_btn.click()
            page.wait_for_timeout(1000)

            # Check open badge count
            badge = page.locator("#flag-tab-badge")
            badge_text = badge.inner_text()
            print(f"  -> Playback Flags badge count: {badge_text}")
            assert int(badge_text) >= 1, f"Badge should show >= 1 open flags, got {badge_text}"

            print("\n=== [4/7] Verifying 'Open Only' Filter Displays Flag ===")
            filter_select = page.locator("#flags-filter-status")
            filter_val = filter_select.input_value()
            print(f"  -> Filter select current value: '{filter_val}'")
            assert filter_val == "open", f"Default filter should be 'open', got '{filter_val}'"

            # Find flag card in DOM
            card = page.locator(f".flag-card[data-flag-id='{flag_id}']").first
            assert card.is_visible(), f"Flag card {flag_id} must be visible under 'Open Only' filter!"
            print(f"  -> Flag card {flag_id} successfully rendered in 'Open Only' view!")

            # Verify candidate lines rendered on the card
            cand_chips = card.locator(".flag-candidate-chip")
            cand_count = cand_chips.count()
            print(f"  -> Card displays {cand_count} candidate line chips")
            assert cand_count >= 1, "Card must display candidate chips in the reaction window"

            print("\n=== [5/7] Testing '🎯 Focus Line' Retargeting in Browser UI ===")
            # Find a candidate line different from current active or the first focus line button
            focus_btn = card.locator("button:has-text('Focus Line')").first
            target_line_match = focus_btn.get_attribute("onclick")
            print(f"  -> Clicking focus line button: {target_line_match}")
            focus_btn.click()
            page.wait_for_timeout(1500)

            # Verify through API that line_id was updated
            get_req = urllib.request.Request(f"{BASE_URL}/api/mobile/v1/books/{PROJECT_ID}/flags")
            with urllib.request.urlopen(get_req) as resp:
                flags_data = json.loads(resp.read().decode("utf-8"))
            live_flag = next(f for f in flags_data["flags"] if f["flag_id"] == flag_id)
            print(f"  -> Updated flag active_line ID: {live_flag.get('line_id')}")
            assert live_flag.get("line_id") is not None, "Flag active line must be updated"

            print("\n=== [6/7] Testing Interactive Status Transitions in Browser UI ===")
            status_select = card.locator(".flag-status-select")
            assert status_select.is_visible(), "Status select dropdown must be visible on flag card"

            # Change status to 'investigating' via dropdown
            print("  -> Selecting 'investigating' in status dropdown...")
            status_select.select_option("investigating")
            page.wait_for_timeout(1500)

            # Verify via API
            with urllib.request.urlopen(get_req) as resp:
                flags_data = json.loads(resp.read().decode("utf-8"))
            live_flag = next(f for f in flags_data["flags"] if f["flag_id"] == flag_id)
            assert live_flag["status"] == "investigating", f"Expected status 'investigating', got '{live_flag['status']}'"
            print("  -> Confirmed status updated to 'investigating'!")

            # Under 'open' filter, the card is now filtered out
            print("  -> Verifying flag is removed from 'Open Only' view after moving to investigating...")
            assert not card.is_visible(), "Investigating flag should not appear under 'Open Only'!"

            # Switch filter to 'investigating' to view it
            print("  -> Switching filter to 'investigating'...")
            filter_select.select_option("investigating")
            page.wait_for_timeout(1000)

            card_inv = page.locator(f".flag-card[data-flag-id='{flag_id}']").first
            assert card_inv.is_visible(), "Flag card must be visible under 'investigating' filter!"

            # Test Quick Action button: 'Mark Fixed'
            print("  -> Clicking '✅ Mark Fixed' quick action button...")
            fixed_btn = card_inv.locator("button:has-text('Mark Fixed')")
            assert fixed_btn.is_visible(), "'Mark Fixed' button should be visible"
            fixed_btn.click()
            page.wait_for_timeout(1500)

            # Under 'investigating' filter, the fixed card should now be hidden
            print("  -> Verifying flag is removed from 'investigating' view after marking fixed...")
            assert not card_inv.is_visible(), "Fixed flag should disappear from 'investigating' filter!"

            # Switch filter to 'fixed' and verify it appears with 'FIXED' badge
            print("  -> Switching filter to 'fixed'...")
            filter_select.select_option("fixed")
            page.wait_for_timeout(1000)

            card_fixed = page.locator(f".flag-card[data-flag-id='{flag_id}']").first
            assert card_fixed.is_visible(), "Flag must be visible in 'fixed' view!"
            badge_fixed = card_fixed.locator("span:has-text('FIXED')")
            assert badge_fixed.is_visible(), "Card must display 'FIXED' badge!"
            print("  -> Verified flag appears in 'fixed' view with FIXED status!")

            # Switch filter to 'all' to capture full screenshot
            filter_select.select_option("all")
            page.wait_for_timeout(1000)

            # Capture screenshot
            page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
            print(f"  -> Screenshot captured: {SCREENSHOT_PATH}")

            browser.close()
            print("\n>>> PLAYWRIGHT BROWSER E2E TEST PASSED COMPLETELY! <<<")

    finally:
        print("\n=== [7/7] Cleanup Test Flag ===")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM playback_flags WHERE flag_id = ?", (flag_id,))
            conn.commit()

        if FLAGS_JSON.is_file():
            with open(FLAGS_JSON, "r", encoding="utf-8") as f:
                disk_data = json.load(f)
            if isinstance(disk_data, dict):
                disk_data["flags"] = [f for f in disk_data.get("flags", []) if f.get("flag_id") != flag_id]
                disk_data["total_flags"] = len(disk_data["flags"])
                out_content = disk_data
            else:
                out_content = [f for f in disk_data if f.get("flag_id") != flag_id]
            with open(FLAGS_JSON, "w", encoding="utf-8") as f:
                json.dump(out_content, f, indent=2)
        print(f"  -> Removed test flag {flag_id} from database and JSON.")


if __name__ == "__main__":
    run_browser_e2e()
