import sqlite3
import json

dbs = [
    "e:/Projects/crazy-audiobook-creator/brain/jobs.db",
    "e:/Projects/crazy-audiobook-creator/pipeline_state.db",
    "e:/Projects/crazy-audiobook-creator/brain/projects/pipeline_state.db"
]

for db_path in dbs:
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT state FROM jobs WHERE project_id = 'sample_book'")
        row = cursor.fetchone()
        if row:
            state = json.loads(row[0])
            state['completed_gen_chapters'] = []
            state['completed_master_chapters'] = []
            state['status'] = 'bootstrapping'
            state['bootstrapping_completed'] = False
            
            new_state = json.dumps(state)
            cursor.execute("UPDATE jobs SET state = ? WHERE project_id = 'sample_book'", (new_state,))
            conn.commit()
            print(f"Successfully reset sample_book in {db_path}")
        else:
            print(f"Project not found in {db_path}")
        conn.close()
    except Exception as e:
        print(f"Error checking {db_path}: {e}")
