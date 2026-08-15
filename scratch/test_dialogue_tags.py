import json
import re
from pathlib import Path

script_dir = Path("brain/projects/sample_book-7/script")
quote_pattern = re.compile(r'^[\"“”\'‘].*[\"“”\'’]$', re.DOTALL)

# Load characters to get genders
char_file = Path("brain/projects/sample_book-7/characters.json")
char_genders = {}
if char_file.exists():
    cdata = json.loads(char_file.read_text(encoding="utf-8"))
    for cid, cinfo in cdata.get("characters", {}).items():
        char_genders[cid.lower()] = cinfo.get("gender", "unknown").lower()

print("Loaded Character Genders:", char_genders)

mismatches = 0
for s_file in sorted(script_dir.glob("chapter_*.json")):
    data = json.loads(s_file.read_text(encoding="utf-8"))
    lines = data.get("lines", [])
    
    for idx, line in enumerate(lines):
        text_trimmed = line.get("text", "").strip()
        is_quote = bool(quote_pattern.match(text_trimmed))
        spk = line.get("speaker", "narrator").lower()
        
        if is_quote and spk != "narrator":
            # Check next line for dialogue tag pronoun
            next_text = lines[idx + 1]["text"].lower() if idx + 1 < len(lines) else ""
            prev_text = lines[idx - 1]["text"].lower() if idx > 0 else ""
            
            spk_gender = char_genders.get(spk, "unknown")
            
            # Check for "she said" / "she whispered" when speaker is male
            if spk_gender == "male" and (re.search(r'\bshe (said|whispered|asked|replied|cried|smiled)\b', next_text) or re.search(r'\bshe (said|whispered|asked|replied|cried|smiled)\b', prev_text)):
                mismatches += 1
                print(f"MISMATCH in {s_file.name} [{line['line_id']}]: Male speaker '{spk}' assigned to quote '{text_trimmed[:40]}...', but tag says 'she said/whispered': '{next_text or prev_text}'")
                
            # Check for "he said" / "he replied" when speaker is female
            elif spk_gender == "female" and (re.search(r'\bhe (said|whispered|asked|replied|cried|smiled)\b', next_text) or re.search(r'\bhe (said|whispered|asked|replied|cried|smiled)\b', prev_text)):
                mismatches += 1
                print(f"MISMATCH in {s_file.name} [{line['line_id']}]: Female speaker '{spk}' assigned to quote '{text_trimmed[:40]}...', but tag says 'he said/replied': '{next_text or prev_text}'")

print(f"\nTotal Dialogue Quote Gender Mismatches Found: {mismatches}")
