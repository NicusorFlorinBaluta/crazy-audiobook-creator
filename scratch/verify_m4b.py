import json
import subprocess
from pathlib import Path

m4b = Path('sample_book-7_chapters_1-3.m4b')
print("M4B File Path:", m4b.absolute())
print(f"M4B File Size: {m4b.stat().st_size / 1024 / 1024:.2f} MB")

res = subprocess.run(['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_chapters', str(m4b)], capture_output=True, text=True)
data = json.loads(res.stdout)

print("\n=== EMBEDDED CHAPTER MARKERS & DURATIONS ===")
for ch in data.get('chapters', []):
    title = ch.get('tags', {}).get('title', 'Unknown')
    start_s = float(ch.get('start_time', 0))
    end_s = float(ch.get('end_time', 0))
    dur_m = (end_s - start_s) / 60
    print(f"  Chapter {ch.get('id') + 1}: '{title}' | {dur_m:.1f} minutes ({start_s:.1f}s -> {end_s:.1f}s)")
