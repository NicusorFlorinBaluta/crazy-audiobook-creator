import json
from pathlib import Path

ch1_script = Path('brain/projects/sample_book-7/script/chapter_001.json')
if ch1_script.exists():
    data = json.loads(ch1_script.read_text(encoding='utf-8'))
    print("Title:", data.get('chapter_title'))
    print("Lines count:", len(data.get('lines', [])))
    for i, line in enumerate(data.get('lines', [])):
        print(f"  Line {i} ({line['line_id']}): {line['text']}")

print("\n=== BOOK.JSON CHAPTER 1 ===")
book_json = Path('brain/projects/sample_book-7/book.json')
if book_json.exists():
    bdata = json.loads(book_json.read_text(encoding='utf-8'))
    ch1 = bdata['chapters'][0]
    print("Book JSON Chapter 1 title:", ch1.get('title'))
    print("Book JSON Chapter 1 word count:", ch1.get('word_count'))
    print("Book JSON Chapter 1 text preview:", ch1.get('text')[:300])
