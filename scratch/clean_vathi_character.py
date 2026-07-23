import json
from pathlib import Path

# 1. Update characters.json for sample_book-7
char_file = Path("brain/projects/sample_book-7/characters.json")
if char_file.exists():
    cdata = json.loads(char_file.read_text(encoding="utf-8"))
    chars = cdata.get("characters", {})
    if "vathi" in chars:
        del chars["vathi"]
        char_file.write_text(json.dumps(cdata, indent=2), encoding="utf-8")
        print("Removed 'vathi' (non-speaking island) from characters.json.")

# 2. Update any script files where speaker was 'vathi'
script_dir = Path("brain/projects/sample_book-7/script")
vathi_fixed = 0
for s_file in sorted(script_dir.glob("chapter_*.json")):
    data = json.loads(s_file.read_text(encoding="utf-8"))
    lines = data.get("lines", [])
    ch_fixed = 0
    
    for line in lines:
        if line.get("speaker") == "vathi":
            line["speaker"] = "narrator"
            ch_fixed += 1
            vathi_fixed += 1
            
    if ch_fixed > 0:
        s_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"Fixed {s_file.name}: converted {ch_fixed} 'vathi' lines to 'narrator'.")

print(f"Total 'vathi' lines converted to narrator: {vathi_fixed}")
