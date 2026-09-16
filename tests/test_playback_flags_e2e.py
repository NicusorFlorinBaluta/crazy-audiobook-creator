#!/usr/bin/env python3
"""End-to-end integration test for the Playback Flags workflow against the live dashboard."""

import json
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

BASE_URL = "http://127.0.0.1:8000"
PROJECT_ID = "sample_book"
PROJECT_DIR = Path("e:/Projects/crazy-audiobook-creator/brain/projects") / PROJECT_ID
FLAGS_JSON = PROJECT_DIR / "playback_flags.json"
DB_PATH = Path("e:/Projects/crazy-audiobook-creator/brain/projects/pipeline_state.db")


def run_test():
    print(f"=== [1/6] Submitting Flag via Live HTTP API ({BASE_URL}) ===")
    url = f"{BASE_URL}/api/mobile/v1/books/{PROJECT_ID}/flags"
    payload = {
        "chapter_number": 1,
        "position_ms": 2500,
        "issue_type": "wrong_speaker",
        "user_note": "E2E Test Flag: speaker mismatch reported from car",
        "source": "android_auto",
        "line_id": "ch01_0000"
    }
    
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status in (200, 201), f"Expected 200 or 201, got {resp.status}"
        data = json.loads(resp.read().decode("utf-8"))
    
    assert data["status"] == "flagged"
    flag = data["flag"]
    flag_id = flag["flag_id"]
    print(f"  -> Flag created successfully: ID = {flag_id}")
    print(f"  -> Attributed speaker: {flag.get('speaker_attributed')}")
    print(f"  -> Active line text: {flag.get('active_line', {}).get('text')[:60]}...")
    assert flag["chapter_number"] == 1
    assert flag["source"] == "android_auto"
    assert flag["active_line"]["line_id"] == "ch01_0000"
    assert flag["speaker_attributed"] == "narrator"

    print("\n=== [2/6] Verifying SQLite Database & JSON Sync ===")
    # Check JSON file on disk
    assert FLAGS_JSON.is_file(), f"{FLAGS_JSON} was not created!"
    with open(FLAGS_JSON, "r", encoding="utf-8") as f:
        disk_data = json.load(f)
    disk_flags = disk_data.get("flags", []) if isinstance(disk_data, dict) else disk_data
    matching = [f for f in disk_flags if f.get("flag_id") == flag_id]
    assert len(matching) == 1, "Flag not found in playback_flags.json!"
    print(f"  -> playback_flags.json verified with {len(disk_flags)} flag(s)")

    # Check GET HTTP endpoint
    get_url = f"{BASE_URL}/api/mobile/v1/books/{PROJECT_ID}/flags"
    with urllib.request.urlopen(get_url) as resp:
        get_data = json.loads(resp.read().decode("utf-8"))
    assert get_data["total_flags"] >= 1
    assert any(f["flag_id"] == flag_id for f in get_data["flags"])
    print(f"  -> GET /api/mobile/v1/books/{PROJECT_ID}/flags verified (total: {get_data['total_flags']})")

    print("\n=== [3/6] Running Investigation CLI (--auto-diagnose & --export-prompt) ===")
    # Test --auto-diagnose
    cli_cmd = [
        sys.executable,
        "tools/investigate_playback_flags.py",
        PROJECT_ID,
        "--flag-id", flag_id,
        "--auto-diagnose",
        "--json"
    ]
    diag_res = subprocess.run(cli_cmd, capture_output=True, text=True, check=True)
    diag_data = json.loads(diag_res.stdout)
    assert len(diag_data) == 1
    print("  -> Auto-diagnose completed:")
    print(f"     Diagnosis: {diag_data[0].get('diagnosis')}")

    # Test --export-prompt
    prompt_cmd = [
        sys.executable,
        "tools/investigate_playback_flags.py",
        PROJECT_ID,
        "--flag-id", flag_id,
        "--export-prompt"
    ]
    prompt_res = subprocess.run(prompt_cmd, capture_output=True, text=True, check=True)
    assert "Playback Issue Investigation" in prompt_res.stdout
    assert flag_id in prompt_res.stdout
    print("  -> Export-prompt generated AI investigation prompt successfully.")

    print("\n=== [4/6] Testing Agent Veto Flow via CLI ===")
    veto_reason = "Manuscript speech tags confirm narrator perspective; speaker is correct."
    veto_cmd = [
        sys.executable,
        "tools/investigate_playback_flags.py",
        PROJECT_ID,
        "--veto", flag_id,
        "--reason", veto_reason
    ]
    veto_res = subprocess.run(veto_cmd, capture_output=True, text=True, check=True)
    assert "vetoed flag" in veto_res.stdout.lower()
    print(f"  -> Veto recorded: {veto_reason}")

    # Verify status changed to vetoed via HTTP GET
    with urllib.request.urlopen(get_url) as resp:
        updated_data = json.loads(resp.read().decode("utf-8"))
    vflag = next(f for f in updated_data["flags"] if f["flag_id"] == flag_id)
    assert vflag["status"] == "vetoed", f"Expected status 'vetoed', got '{vflag['status']}'"
    assert vflag["agent_veto"] == veto_reason
    print(f"  -> Verified via HTTP GET: status={vflag['status']}, agent_veto='{vflag['agent_veto']}'")

    print("\n=== [5/6] Testing Dashboard PATCH Endpoint (Reopen / Fix) ===")
    patch_url = f"{BASE_URL}/api/mobile/v1/books/{PROJECT_ID}/flags/{flag_id}"
    patch_body = {
        "status": "fixed",
        "resolution_notes": "Tested and verified resolution."
    }
    patch_req = urllib.request.Request(
        patch_url,
        data=json.dumps(patch_body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PATCH"
    )
    with urllib.request.urlopen(patch_req) as resp:
        assert resp.status == 200
        patch_resp = json.loads(resp.read().decode("utf-8"))
    
    assert patch_resp["status"] == "updated"
    assert patch_resp["flag"]["status"] == "fixed"
    assert patch_resp["flag"]["resolution_notes"] == "Tested and verified resolution."
    print("  -> PATCH endpoint successfully updated flag to 'fixed'")

    print("\n=== [6/6] Cleaning Up Test Flag ===")
    # Remove test flag from SQLite and JSON
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM playback_flags WHERE flag_id = ?", (flag_id,))
        conn.commit()

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
    print("  -> Cleaned up test flag from database and playback_flags.json.")

    print("\n>>> ALL E2E FLOW CHECKS PASSED PERFECTLY! <<<")


if __name__ == "__main__":
    run_test()
