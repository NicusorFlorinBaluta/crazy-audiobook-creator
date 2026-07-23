import sqlite3
import json
import shutil
import time
import urllib.request
from pathlib import Path

project_id = "sample_book-7"
print(f"=== FORCING COMPLETE CLEAN REBUILD FOR {project_id} ===")

# 1. Stop pipeline if running
try:
    req = urllib.request.Request(f"http://127.0.0.1:8000/api/projects/{project_id}/stop", method="POST")
    urllib.request.urlopen(req)
    print("Sent stop request to Dashboard API.")
    time.sleep(2)
except Exception as e:
    print("Stop request notice:", e)

# 2. Delete ALL mastered chapter WAVs and segments on disk
ws_dir = Path(f"workspace/{project_id}")
if ws_dir.exists():
    shutil.rmtree(ws_dir, ignore_errors=True)
    print(f"Deleted entire workspace directory: {ws_dir}")

project_dir = Path(f"brain/projects/{project_id}")
ch_dir = project_dir / "chapters"
if ch_dir.exists():
    shutil.rmtree(ch_dir, ignore_errors=True)
    print(f"Deleted chapters directory: {ch_dir}")

for m4b in project_dir.glob("*.m4b"):
    m4b.unlink()
    print(f"Deleted M4B file: {m4b.name}")

# 3. Clean book.json state
book_json_path = project_dir / "book.json"
if book_json_path.exists():
    bdata = json.loads(book_json_path.read_text(encoding="utf-8"))
    bdata["generated_chapters"] = []
    bdata["mastered_chapters"] = []
    bdata["status"] = "generating"
    book_json_path.write_text(json.dumps(bdata, indent=2), encoding="utf-8")
    print("Updated book.json: cleared generated_chapters & mastered_chapters.")

# 4. Clean pipeline_state.db
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
    state["lines_failed"] = 0
    state["running"] = False
    c.execute("UPDATE jobs SET state=? WHERE project_id=?", (json.dumps(state), project_id))
    conn.commit()
    print("Updated pipeline_state.db: reset to chapter 1, cleared state.")

# 5. Clear voice_cache.db fingerprints & FX prompt cache
v_db = Path("voice_cache.db")
if v_db.exists():
    try:
        v_conn = sqlite3.connect("voice_cache.db")
        v_conn.execute("DELETE FROM generation_fingerprints WHERE project_id=?", (project_id,))
        v_conn.execute("DELETE FROM fx_prompt_cache")
        v_conn.commit()
        print("Cleared generation_fingerprints and fx_prompt_cache from voice_cache.db.")
    except Exception as e:
        print("Voice cache clear notice:", e)

print("\nAll cached audio and state wiped 100% clean.")
