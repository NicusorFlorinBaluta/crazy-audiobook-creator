import httpx
import subprocess
import time
import os
from shared.models import GenerateChapterRequest, ScriptLine

env = os.environ.copy()
env['PYTHONPATH'] = os.getcwd()

print("Starting server...")
with open("voice_stderr.log", "w") as ferr:
    proc = subprocess.Popen(
        ["E:\\PyTorch env\\my_venv\\Scripts\\python.exe", "-m", "voice.tts_server.main"],
        stdout=subprocess.DEVNULL,
        stderr=ferr,
        env=env
    )

time.sleep(15)  # Wait for it to start

print("Sending request...")
req = GenerateChapterRequest(
    project_id="sample_book-12",
    chapter_number=1,
    lines=[ScriptLine(line_id="L1", speaker="narrator", text="Test")]
)
try:
    resp = httpx.post("http://127.0.0.1:8100/generate/chapter", json=req.model_dump(), timeout=60)
    print("STATUS:", resp.status_code)
    print("TEXT:", resp.text)
except Exception as e:
    print("ERR:", e)

proc.terminate()
