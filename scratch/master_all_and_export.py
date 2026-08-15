import sqlite3
import json
import urllib.request

project_id = "sample_book-7"
print(f"=== CLEARING SELECTION & TRIGGERING MASTERING/EXPORT FOR ALL 8 CHAPTERS ===")

conn = sqlite3.connect("brain/projects/pipeline_state.db")
c = conn.cursor()
row = c.execute("SELECT state FROM jobs WHERE project_id=?", (project_id,)).fetchone()
if row:
    state = json.loads(row[0])
    state["generation_chapter_selection"] = None  # None means ALL chapters
    state["status"] = "mastering"
    c.execute("UPDATE jobs SET state=? WHERE project_id=?", (json.dumps(state), project_id))
    conn.commit()
    print("Cleared generation_chapter_selection -> set to None for full 8 chapters.")

# Also update book.json
from pathlib import Path
b_file = Path(f"brain/projects/{project_id}/book.json")
if b_file.exists():
    bdata = json.loads(b_file.read_text(encoding="utf-8"))
    bdata["generation_chapter_selection"] = None
    b_file.write_text(json.dumps(bdata, indent=2), encoding="utf-8")

# Trigger pipeline start
try:
    req = urllib.request.Request(f"http://127.0.0.1:8000/api/projects/{project_id}/start", method="POST")
    res = urllib.request.urlopen(req)
    print("Pipeline Start Response:", json.loads(res.read().decode("utf-8")))
except Exception as e:
    print("Start notice:", e)
