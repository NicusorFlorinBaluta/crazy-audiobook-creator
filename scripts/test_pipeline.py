import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared import paths as shared_paths  # noqa: E402

API_URL = "http://localhost:8000/api/projects"

# Upload can take a while for a large EPUB; status polls should fail fast so a
# dead dashboard is reported rather than hanging this script indefinitely.
UPLOAD_TIMEOUT_SECONDS = 120
REQUEST_TIMEOUT_SECONDS = 30


def run_test():
    # Resolved from the repository root, not the working directory, so this
    # script works from anywhere rather than only from the repo root.
    epub_path = shared_paths.REPO_ROOT / "tests" / "fixtures" / "sample_book.epub"
    if not epub_path.exists():
        print(f"Error: {epub_path} not found.")
        sys.exit(1)

    print("1. Uploading EPUB...")
    with open(epub_path, "rb") as f:
        files = {"file": (epub_path.name, f, "application/epub+zip")}
        resp = requests.post(API_URL, files=files, timeout=UPLOAD_TIMEOUT_SECONDS)

    if not resp.ok:
        print(f"Failed to upload: {resp.text}")
        sys.exit(1)

    data = resp.json()
    project_id = data["project_id"]
    print(f"Project created! ID: {project_id}")
    print(f"Title: {data.get('title')}, Chapters: {data.get('chapters_detected')}")

    print("\n2. Starting Pipeline...")
    start_resp = requests.post(f"{API_URL}/{project_id}/start", timeout=REQUEST_TIMEOUT_SECONDS)
    if not start_resp.ok:
        print(f"Failed to start pipeline: {start_resp.text}")
        sys.exit(1)

    print("Pipeline started! Polling status...")

    # Poll status
    while True:
        status_resp = requests.get(f"{API_URL}/{project_id}/status", timeout=REQUEST_TIMEOUT_SECONDS)
        if not status_resp.ok:
            print("Failed to get status")
            break

        status_data = status_resp.json()
        stage = status_data.get("current_stage")
        state = status_data.get("status")

        print(f"Status: {state.upper()} | Stage: {stage} | Lines: {status_data.get('total_lines', 0)}")

        if state in ("completed", "error", "failed"):
            print(f"\nPipeline finished with state: {state}")
            break

        time.sleep(10)


if __name__ == "__main__":
    run_test()
