import sqlite3
import json

def fix():
    conn = sqlite3.connect('brain/projects/pipeline_state.db')
    row = conn.execute("SELECT state FROM jobs WHERE project_id='sample_book'").fetchone()
    if row and row[0]:
        state = json.loads(row[0])
        state['status'] = 'scripting'
        state['error_message'] = None
        conn.execute("UPDATE jobs SET state = ? WHERE project_id='sample_book'", (json.dumps(state),))
        conn.commit()
        print("Fixed!")

if __name__ == "__main__":
    fix()
