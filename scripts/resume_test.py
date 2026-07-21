import requests
import time
import sys

API_URL = "http://localhost:8000/api/projects"
project_id = "sample_book"

print("\n1. Resuming Pipeline...")
start_resp = requests.post(f"{API_URL}/{project_id}/start")
if not start_resp.ok:
    print(f"Failed to resume pipeline: {start_resp.text}")
    sys.exit(1)
    
print("Pipeline resumed! Polling status...")

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
    
    if state in ("complete", "error", "failed"):
        print(f"\nPipeline finished with state: {state}")
        break
        
    time.sleep(5)
