import sqlite3
conn = sqlite3.connect('brain/projects/pipeline_state.db')
c = conn.cursor()
c.execute("DELETE FROM jobs WHERE project_id='sample_book'")
conn.commit()
conn.close()
print("Job deleted!")
