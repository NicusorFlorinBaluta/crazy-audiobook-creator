from pathlib import Path
import json

for ch_num in [1, 2, 3]:
    ch_path = Path(f'brain/projects/sample_book-7/script/chapter_{ch_num:03d}.json')
    if ch_path.exists():
        data = json.loads(ch_path.read_text(encoding='utf-8'))
        print(f"Chapter {ch_num}: {data.get('total_lines')} lines, title='{data.get('chapter_title')}'")
    else:
        print(f"Chapter {ch_num}: SCRIPT NOT FOUND")

# Check chapter WAV files
print("\n=== Chapter WAV files ===")
for d in [Path('workspace/sample_book-7/chapters'), Path('brain/projects/sample_book-7/chapters')]:
    if d.exists():
        for f in sorted(d.glob('chapter_*.wav')):
            size_mb = f.stat().st_size / 1024 / 1024
            print(f"  {f}: {size_mb:.1f} MB")

# Count segment WAVs per chapter
print("\n=== Segment WAV counts ===")
seg_dir = Path('workspace/sample_book-7/segments')
if seg_dir.exists():
    from collections import Counter
    c = Counter()
    for f in seg_dir.glob('ch*.wav'):
        ch = f.name[:4]  # ch01, ch02, ch03
        c[ch] += 1
    for ch, count in sorted(c.items()):
        print(f"  {ch}: {count} segment files")
