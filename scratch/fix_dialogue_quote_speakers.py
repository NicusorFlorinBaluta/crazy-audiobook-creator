import json
import re
from pathlib import Path

script_dir = Path("brain/projects/sample_book-7/script")
quote_pattern = re.compile(r'^[\"“”\'‘].*[\"“”\'’]$', re.DOTALL)

char_file = Path("brain/projects/sample_book-7/characters.json")
char_data = {}
char_genders = {}
if char_file.exists():
    cdata = json.loads(char_file.read_text(encoding="utf-8"))
    char_data = cdata.get("characters", {})
    for cid, cinfo in char_data.items():
        char_genders[cid.lower()] = cinfo.get("gender", "unknown").lower()

print("Character Genders:", char_genders)

# Map names in tags to character IDs
name_to_cid = {
    "dusk": "dusk",
    "vathi": "vathi",
    "starling": "starling",
    "frost": "uncle_frost",
    "uncle frost": "uncle_frost",
    "frond": "frond",
    "mother frond": "frond",
    "tuka": "tuka",
    "kokerlii": "kokerlii",
    "soil": "second_of_the_soil",
    "second of the soil": "second_of_the_soil",
}

quote_fixes = 0

for s_file in sorted(script_dir.glob("chapter_*.json")):
    data = json.loads(s_file.read_text(encoding="utf-8"))
    lines = data.get("lines", [])
    ch_fixes = 0
    
    for idx, line in enumerate(lines):
        text_trimmed = line.get("text", "").strip()
        is_quote = bool(quote_pattern.match(text_trimmed))
        spk = line.get("speaker", "narrator").lower()
        
        if is_quote:
            next_text = lines[idx + 1]["text"].lower() if idx + 1 < len(lines) else ""
            prev_text = lines[idx - 1]["text"].lower() if idx > 0 else ""
            adjacent_narrative = (next_text if not quote_pattern.match(lines[idx + 1]["text"].strip()) else "") + " " + (prev_text if not quote_pattern.match(lines[idx - 1]["text"].strip()) else "")
            
            # Check 1: Explicit character name in adjacent narration (e.g. "Vathi said", "Dusk called")
            found_cid = None
            for name_key, cid in name_to_cid.items():
                if re.search(r'\b' + re.escape(name_key) + r'\b\s*(said|whispered|asked|replied|cried|smiled|called|nodded|thought|spoke)', adjacent_narrative):
                    found_cid = cid
                    break
            
            if found_cid and found_cid != spk:
                line["speaker"] = found_cid
                ch_fixes += 1
                quote_fixes += 1
                print(f"Name Tag Fix [{s_file.name} / {line['line_id']}]: '{spk}' -> '{found_cid}' (tag: '{adjacent_narrative.strip()}')")
                continue
                
            # Check 2: Gender mismatch correction ("he said" for female speaker or "she said" for male speaker)
            spk_gender = char_genders.get(spk, "unknown")
            
            if spk_gender == "male" and re.search(r'\bshe (said|whispered|asked|replied|cried|smiled|called)\b', adjacent_narrative):
                # Quote tag says female ("she said"), but speaker was male!
                # Infer female speaker in scene (e.g. vathi or starling)
                female_chars = [cid for cid, g in char_genders.items() if g == "female"]
                target_spk = female_chars[0] if female_chars else "vathi"
                line["speaker"] = target_spk
                ch_fixes += 1
                quote_fixes += 1
                print(f"Gender Tag Fix [{s_file.name} / {line['line_id']}]: '{spk}' -> '{target_spk}' (tag says 'she ...')")
                
            elif spk_gender == "female" and re.search(r'\bhe (said|whispered|asked|replied|cried|smiled|called)\b', adjacent_narrative):
                # Quote tag says male ("he said"), but speaker was female!
                male_chars = [cid for cid, g in char_genders.items() if g == "male" and cid != "narrator"]
                target_spk = male_chars[0] if male_chars else "dusk"
                line["speaker"] = target_spk
                ch_fixes += 1
                quote_fixes += 1
                print(f"Gender Tag Fix [{s_file.name} / {line['line_id']}]: '{spk}' -> '{target_spk}' (tag says 'he ...')")

    if ch_fixes > 0:
        s_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"Saved {s_file.name} with {ch_fixes} quote speaker fixes.\n")

print(f"TOTAL QUOTE DIALOGUE SPEAKER FIXES APPLIED: {quote_fixes}")
