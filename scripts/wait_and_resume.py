import requests
import time
import subprocess

print("Waiting for Voice Server to become healthy...")
while True:
    try:
        r = requests.get("http://192.168.50.180:8100/health", timeout=2)
        if r.status_code == 200:
            print("Voice Server is healthy! Resuming pipeline...")
            subprocess.run(["python", "scripts/resume_test.py"])
            break
    except Exception:
        pass
    time.sleep(5)
