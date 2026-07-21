from pathlib import Path
import sqlite3
import json

project_root = Path(__file__).resolve().parent.parent
db_path = project_root / "brain" / "projects" / "pipeline_state.db"

if not db_path.exists():
    raise FileNotFoundError(f"Database not found at expected location: {db_path}")

conn = sqlite3.connect(str(db_path))
cursor = conn.cursor()
cursor.execute("SELECT project_id, state FROM jobs")
rows = cursor.fetchall()

if rows:
    for project_id, state_str in rows:
        state = json.loads(state_str)
        state['status'] = 'scripting'
        state['bootstrapping_completed'] = False
        state['script_completed'] = False
        state['scripted_chapters'] = []
        state['generated_chapters'] = []
        state['mastered_chapters'] = []
        state['completed_script_chapters'] = []
        state['completed_gen_chapters'] = []
        state['completed_master_chapters'] = []
        state['current_script_chapter'] = None
        state['current_gen_chapter'] = None
        
        new_state = json.dumps(state)
        cursor.execute("UPDATE jobs SET state = ? WHERE project_id = ?", (new_state, project_id))
    conn.commit()
    print(f"Successfully reset {len(rows)} projects in DB to scripting.")
else:
    print("No projects found in DB.")
conn.close()
