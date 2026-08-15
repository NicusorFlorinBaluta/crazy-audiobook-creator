from pathlib import Path
import datetime

print("=== ALL MASTERED CHAPTER WAV TIMESTAMPS ===")
for root in [Path("brain/projects/sample_book-7"), Path("workspace/sample_book-7")]:
    if root.exists():
        for f in sorted(root.rglob("*.wav")):
            if "segment" not in str(f):
                mtime = datetime.datetime.fromtimestamp(f.stat().st_mtime)
                print(f"  {f}: mtime = {mtime}")
