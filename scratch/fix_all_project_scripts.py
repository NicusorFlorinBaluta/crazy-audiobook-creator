import json
import re
from pathlib import Path

script_dir = Path("brain/projects/sample_book-7/script")
quote_pattern = re.compile(r'^[\"“”\'‘].*[\"“”\'’]$', re.DOTALL)

total_fixed = 0
for s_file in sorted(script_dir.glob("chapter_*.json")):
    data = json.loads(s_file.read_text(encoding="utf-8"))
    lines = data.get("lines", [])
    ch_fixed = 0
    
    for line in lines:
        orig_spk = line.get("speaker", "narrator")
        text_trimmed = line.get("text", "").strip()
        is_quote = bool(quote_pattern.match(text_trimmed))
        
        if not is_quote and orig_spk != "narrator":
            line["speaker"] = "narrator"
            ch_fixed += 1
            total_fixed += 1
            
    if ch_fixed > 0:
        s_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"Fixed {s_file.name}: {ch_fixed} narrative lines corrected to 'narrator'")

print(f"\nTOTAL NARRATIVE LINES FIXED ACROSS ALL CHAPTER SCRIPTS: {total_fixed}")
