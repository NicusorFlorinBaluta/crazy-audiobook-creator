import sqlite3
from pathlib import Path

for db_file in Path('brain/projects').glob('*.db'):
    print(f"=== DB: {db_file} ===")
    conn = sqlite3.connect(str(db_file))
    c = conn.cursor()
    tables = [t[0] for t in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print("Tables:", tables)
    for t in tables:
        count = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  Table {t}: {count} rows")
        if count > 0:
            cols = [col[1] for col in c.execute(f"PRAGMA table_info({t})").fetchall()]
            rows = c.execute(f"SELECT * FROM {t} LIMIT 5").fetchall()
            print(f"  Columns: {cols}")
            print(f"  Sample rows: {rows}")
