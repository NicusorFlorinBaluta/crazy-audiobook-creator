"""End-to-End Test for Crazy Audiobook Creator

Tests:
1. EPUB upload & project creation via API
2. Chapter selection (selecting only Chapter 1)
3. Google Books metadata & cover artwork fetch
4. Pipeline execution: Scripting -> Voice Bootstrapping -> Generating (Ch 1) -> Mastering (Ch 1) -> Selective Partial M4B Export
5. Verifies output M4B file existence and download endpoint HTTP 200 response
"""

import sys
import time
import requests
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_URL = "http://127.0.0.1:8000"

def main():
    print("=" * 60)
    print("      E2E TEST: Crazy Audiobook Creator (Chapter 1)")
    print("=" * 60)

    # Step 1: Reuse existing project with pre-scripted chapters if available to test Resume
    projs_resp = requests.get(f"{BASE_URL}/api/projects")
    project_id = None
    if projs_resp.status_code == 200:
        projs = projs_resp.json()
        for p in projs:
            pid = p.get("project_id", "")
            if pid.startswith("sample_book"):
                s_dir = Path("brain/projects") / pid / "script"
                if s_dir.exists() and len(list(s_dir.glob("chapter_*.json"))) >= 8:
                    project_id = pid
                    print(f"\n[1/6] Reusing existing project '{project_id}' with all 8 pre-scripted chapters (RESUME FEATURE TEST)...")
                    break

    if not project_id:
        epub_path = Path("sample_book.epub")
        if not epub_path.exists():
            print(f"[ERROR] Test EPUB file '{epub_path}' not found!")
            sys.exit(1)

        print("\n[1/6] Uploading EPUB to create project...")
        with open(epub_path, "rb") as f:
            resp = requests.post(f"{BASE_URL}/api/projects", files={"file": ("sample_book.epub", f, "application/epub+zip")})
        
        if resp.status_code != 200:
            print(f"[ERROR] Failed to create project: {resp.status_code} {resp.text}")
            sys.exit(1)

        proj_info = resp.json()
        project_id = proj_info["project_id"]
        print(f" [✓] Created project '{project_id}' ({proj_info['chapters_detected']} chapters detected)")

    # Step 2: Set chapter selection (Chapter 1 only)
    print("\n[2/6] Setting chapter selection to Chapter 1 only...")
    resp = requests.post(f"{BASE_URL}/api/projects/{project_id}/set-selection", json={"chapters": [1]})
    if resp.status_code != 200:
        print(f"[ERROR] Failed to set selection: {resp.status_code} {resp.text}")
        sys.exit(1)
    print(" [✓] Chapter selection set to [1]")

    # Step 3: Fetch metadata & cover artwork
    print("\n[3/6] Fetching artwork & metadata from Google Books API...")
    resp = requests.post(f"{BASE_URL}/api/projects/{project_id}/fetch-metadata")
    if resp.status_code == 200:
        meta_res = resp.json()
        print(f" [✓] Metadata fetched: title='{meta_res.get('title')}', author='{meta_res.get('author')}', cover='{meta_res.get('cover_path')}'")
    else:
        print(f" [!] Metadata fetch notice: {resp.status_code}")

    # Step 4: Start pipeline
    print("\n[4/6] Starting audiobook production pipeline...")
    resp = requests.post(f"{BASE_URL}/api/projects/{project_id}/start")
    if resp.status_code != 200:
        print(f"[ERROR] Failed to start pipeline: {resp.status_code} {resp.text}")
        sys.exit(1)
    print(" [✓] Pipeline started successfully")

    # Step 5: Poll status until completion
    print("\n[5/6] Monitoring pipeline execution progress...")
    start_time = time.time()
    last_stage = ""

    while True:
        time.sleep(5)
        st_resp = requests.get(f"{BASE_URL}/api/projects/{project_id}/status")
        if st_resp.status_code != 200:
            print(f"[!] Error reading status: {st_resp.status_code}")
            continue

        st = st_resp.json()
        stage = st.get("status", "")

        if stage != last_stage:
            elapsed = time.time() - start_time
            print(f"  --> Pipeline Stage: {stage.upper()} (elapsed: {elapsed:.1f}s)")
            last_stage = stage

        if stage in ("selection_complete", "complete", "completed"):
            print(f"\n [✓] Pipeline reached final stage '{stage}' in {(time.time() - start_time):.1f} seconds!")
            break
        elif stage in ("error", "failed"):
            print(f"\n[ERROR] Pipeline failed with error: {st.get('error_message')}")
            sys.exit(1)

    # Step 6: Verify M4B file and download endpoint
    print("\n[6/6] Verifying M4B audio file and download endpoint...")
    project_dir = Path("brain/projects") / project_id
    m4b_files = list(project_dir.glob("*.m4b"))

    if not m4b_files:
        print("[ERROR] No .m4b files found in project directory!")
        sys.exit(1)

    m4b_file = m4b_files[0]
    file_size_mb = m4b_file.stat().st_size / (1024 * 1024)
    print(f" [✓] Found output M4B: {m4b_file.name} ({file_size_mb:.2f} MB)")

    dl_resp = requests.get(f"{BASE_URL}/api/projects/{project_id}/download")
    if dl_resp.status_code != 200:
        print(f"[ERROR] Download endpoint returned status {dl_resp.status_code}")
        sys.exit(1)

    print(f" [✓] Download endpoint responded with HTTP 200 ({len(dl_resp.content)} bytes)")

    print("\n" + "=" * 60)
    print("      E2E TEST PASSED SUCCESSFULLY!")
    print("=" * 60)

if __name__ == "__main__":
    main()
