import sqlite3
import json

conn = sqlite3.connect('brain/projects/pipeline_state.db')
c = conn.cursor()

row = c.execute("SELECT state FROM jobs WHERE project_id='sample_book-7'").fetchone()
if row:
    state = json.loads(row[0])
    state['status'] = 'paused'
    state['running'] = False
    state['mastered_chapters'] = [1, 2, 3]
    state['generated_chapters'] = [1, 2, 3]
    state['current_gen_chapter'] = None
    state['current_chapter'] = 3
    
    c.execute("UPDATE jobs SET state=? WHERE project_id='sample_book-7'", (json.dumps(state),))
    conn.commit()
    print("Updated pipeline_state.db successfully! State updated to reflect Chapters 1-3 Completed.")
