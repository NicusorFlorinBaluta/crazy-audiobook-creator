import sqlite3
import json
from pathlib import Path

conn = sqlite3.connect("brain/projects/pipeline_state.db")
c = conn.cursor()
row = c.execute("SELECT state FROM jobs WHERE project_id='sample_book-7'").fetchone()
if row:
    state = json.loads(row[0])
    print("=== FINAL PIPELINE STATE ===")
    print("Status:", state.get("status"))
    print("Running:", state.get("running"))
    print("Mastered Chapters:", state.get("mastered_chapters"))
    print("Generated Chapters:", state.get("generated_chapters"))

print("\n=== PROJECT FILES ===")
p = Path("brain/projects/sample_book-7")
if p.exists():
    for f in p.glob("*"):
        if f.is_file():
            print(f"  {f.name} ({f.stat().st_size / (1024*1024):.2f} MB)")

ch_p = p / "chapters"
if ch_p.exists():
    print("\n=== MASTERED CHAPTER WAVS ===")
    for f in sorted(ch_p.glob("*.wav")):
        print(f"  {f.name} ({f.stat().st_size / (1024*1024):.2f} MB)")
