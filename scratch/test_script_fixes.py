import json
import re
from pathlib import Path

script_path = Path("brain/projects/sample_book-7/script/chapter_004.json")
if not script_path.exists():
    print("Script file not found.")
    exit(0)

data = json.loads(script_path.read_text(encoding="utf-8"))
lines = data.get("lines", [])

quote_pattern = re.compile(r'^[\"“”\'‘].*[\"“”\'’]$')

fixed_lines = 0
for l in lines:
    orig_spk = l["speaker"]
    text = l["text"].strip()
    is_quote = bool(quote_pattern.match(text))
    
    if not is_quote and orig_spk != "narrator":
        fixed_lines += 1
        print(f"FIXED [{l['line_id']}]: '{orig_spk}' -> 'narrator' | Text: {text}")

print(f"\nTotal lines in Chapter 4: {len(lines)}")
print(f"Total non-quote lines incorrectly assigned to characters: {fixed_lines}")
