import sqlite3
import json
import time
import urllib.request
from pathlib import Path

project_id = "sample_book-7"
print(f"=== STOPPING & RESETTING ALL CHAPTERS FOR FULL BOOK REGENERATION: {project_id} ===")

# 1. Stop running pipeline first
try:
    req = urllib.request.Request(f"http://127.0.0.1:8000/api/projects/{project_id}/stop", method="POST")
    urllib.request.urlopen(req)
    print("Sent stop request.")
    time.sleep(2)
except Exception as e:
    print("Stop request notice:", e)

# 2. Wipe ALL audio segment files in workspace/sample_book-7/segments/
segments_dir = Path(f"workspace/{project_id}/segments")
if segments_dir.exists():
    for f in segments_dir.glob("*.wav"):
        try:
            f.unlink()
        except Exception:
            pass
    print("Wiped ALL segment WAV files.")

# 3. Reset database state in pipeline_state.db
conn = sqlite3.connect("brain/projects/pipeline_state.db")
c = conn.cursor()
row = c.execute("SELECT state FROM jobs WHERE project_id=?", (project_id,)).fetchone()
if row:
    state = json.loads(row[0])
    state["status"] = "generating"
    state["current_gen_chapter"] = 1
    state["scripted_chapters"] = list(range(1, (state.get("total_chapters") or 8) + 1))
    state["generated_chapters"] = []
    state["mastered_chapters"] = []
    state["lines_generated"] = 0
    state["lines_failed"] = 0
    state["generation_chapter_selection"] = None
    c.execute("UPDATE jobs SET state=? WHERE project_id=?", (json.dumps(state), project_id))
    conn.commit()
    print("Reset pipeline_state.db: current_gen_chapter=1, generated_chapters=[].")

# 4. Clear ALL fingerprints in voice_cache.db
v_db = Path("voice_cache.db")
if v_db.exists():
    try:
        v_conn = sqlite3.connect("voice_cache.db")
        v_conn.execute("DELETE FROM generation_fingerprints WHERE project_id=?", (project_id,))
        v_conn.commit()
        print("Wiped ALL line fingerprints from voice_cache.db.")
    except Exception as e:
        print("Voice cache clear notice:", e)

# 5. Trigger pipeline start API endpoint
try:
    req = urllib.request.Request(f"http://127.0.0.1:8000/api/projects/{project_id}/start", method="POST")
    res = urllib.request.urlopen(req)
    data = json.loads(res.read().decode("utf-8"))
    print("Pipeline Start Response:", data)
except Exception as e:
    print("Pipeline Start API Error:", e)
