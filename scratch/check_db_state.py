import sqlite3
import json

conn = sqlite3.connect('brain/projects/pipeline_state.db')
c = conn.cursor()
row = c.execute("SELECT state FROM jobs WHERE project_id='sample_book-7'").fetchone()
if row:
    state = json.loads(row[0])
    print("DB state keys:", list(state.keys()))
    print("total_chapters:", state.get('total_chapters'))
    print("title:", state.get('title'))
    print("status:", state.get('status'))
