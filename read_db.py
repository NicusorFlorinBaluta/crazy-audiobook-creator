import sqlite3
import json
conn = sqlite3.connect('e:/Projects/crazy-audiobook-creator/brain/projects/pipeline_state.db')
c = conn.cursor()
c.execute("SELECT * FROM jobs")
for row in c.fetchall():
    print("Row:", row)
