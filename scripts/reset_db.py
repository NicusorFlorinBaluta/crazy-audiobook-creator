import sqlite3
import json

db_path = "e:/Projects/crazy-audiobook-creator/brain/jobs.db"
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
    print("Successfully reset sample_book in DB.")
else:
    print("Project not found in DB!")
conn.close()
