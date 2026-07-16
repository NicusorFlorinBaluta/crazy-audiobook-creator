import requests
import time
import sys
from pathlib import Path

API_URL = "http://localhost:8000/api/projects"

def run_test():
    epub_path = Path("sample_book.epub")
    if not epub_path.exists():
        print(f"Error: {epub_path} not found.")
        sys.exit(1)

    print("1. Uploading EPUB...")
    with open(epub_path, "rb") as f:
        files = {"file": (epub_path.name, f, "application/epub+zip")}
        resp = requests.post(API_URL, files=files)
    
    if not resp.ok:
        print(f"Failed to upload: {resp.text}")
        sys.exit(1)
        
    data = resp.json()
    project_id = data["project_id"]
    print(f"Project created! ID: {project_id}")
    print(f"Title: {data.get('title')}, Chapters: {data.get('chapters_detected')}")
    
    print("\n2. Starting Pipeline...")
    start_resp = requests.post(f"{API_URL}/{project_id}/start")
    if not start_resp.ok:
        print(f"Failed to start pipeline: {start_resp.text}")
        sys.exit(1)
        
    print("Pipeline started! Polling status...")
    
    # Poll status
    while True:
        status_resp = requests.get(f"{API_URL}/{project_id}/status")
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
