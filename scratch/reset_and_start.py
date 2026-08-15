import sqlite3
import json
import urllib.request
from pathlib import Path

project_id = "sample_book-7"
print(f"=== RESETTING PIPELINE FOR {project_id} ===")

# 1. Clear old segments for Chapter 1 and Chapter 2 so they re-synthesize cleanly
segments_dir = Path(f"workspace/{project_id}/segments")
if segments_dir.exists():
    for f in segments_dir.glob("ch01_*.wav"):
        f.unlink()
    for f in segments_dir.glob("ch02_*.wav"):
        f.unlink()
    print("Cleared ch01 and ch02 segment files.")

# 2. Reset database state for sample_book-7 in pipeline_state.db
conn = sqlite3.connect("brain/projects/pipeline_state.db")
c = conn.cursor()
row = c.execute("SELECT state FROM jobs WHERE project_id=?", (project_id,)).fetchone()
if row:
    state = json.loads(row[0])
    state["status"] = "generating"
    state["current_gen_chapter"] = 1
    state["generated_chapters"] = []
    state["mastered_chapters"] = []
    state["lines_generated"] = 0
    c.execute("UPDATE jobs SET state=? WHERE project_id=?", (json.dumps(state), project_id))
    conn.commit()
    print("Updated pipeline_state.db job state: current_gen_chapter=1.")

# 3. Clear fingerprints in voice_cache.db if it exists
v_db = Path("voice_cache.db")
if v_db.exists():
    try:
        v_conn = sqlite3.connect("voice_cache.db")
        v_conn.execute("DELETE FROM generation_fingerprints WHERE project_id=? AND (line_id LIKE 'ch01_%' OR line_id LIKE 'ch02_%')", (project_id,))
        v_conn.commit()
        print("Cleared ch01 and ch02 fingerprints from voice_cache.db.")
    except Exception as e:
        print("Voice cache clear notice:", e)

# 4. Trigger start API endpoint on Dashboard API (port 8000)
try:
    req = urllib.request.Request(f"http://127.0.0.1:8000/api/projects/{project_id}/start", method="POST")
    res = urllib.request.urlopen(req)
    data = json.loads(res.read().decode("utf-8"))
    print("Pipeline Start Response:", data)
except Exception as e:
    print("Pipeline Start API Error:", e)
