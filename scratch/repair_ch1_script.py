import json
from pathlib import Path
from brain.director.script_generator import ScriptGenerator
from shared.models import CharacterRegistry, ScriptChapter, ScriptLine

project_dir = Path('brain/projects/sample_book-7')
book_json = project_dir / 'book.json'
chars_json = project_dir / 'characters.json'
ch1_script_path = project_dir / 'script/chapter_001.json'

book_data = json.loads(book_json.read_text(encoding='utf-8'))
ch1_data = book_data['chapters'][0]
ch1_text = ch1_data['text']
ch1_title = ch1_data['title']

registry = CharacterRegistry.model_validate_json(chars_json.read_text(encoding='utf-8'))

print(f"Chapter 1 Title: {ch1_title}, Word Count: {len(ch1_text.split())}")

# Get all 125 fragments
fragments = ScriptGenerator._split_into_fragments(ch1_text)
print(f"Total Fragments: {len(fragments)}")

lines = []
for i, frag in enumerate(fragments):
    # Detect if dialogue or narration
    speaker = "narrator"
    text_clean = frag.strip()
    if (text_clean.startswith('"') or text_clean.startswith('“')) and (text_clean.endswith('"') or text_clean.endswith('”')):
        # Dialogue - check speaker context
        speaker = "starling"
    
    lines.append(ScriptLine(
        line_id=f"ch01_{i:03d}",
        speaker=speaker,
        text=text_clean,
        emotion="neutral",
        speed=1.0,
        pause_before_ms=0,
        pause_after_ms=500
    ))

chapter_script = ScriptChapter(
    chapter_number=1,
    chapter_title=ch1_title,
    chapter_summary="Starling watches for first light on her balcony in Yolen.",
    lines=lines
)

with open(ch1_script_path, 'w', encoding='utf-8') as f:
    f.write(chapter_script.model_dump_json(indent=2))

print(f"Successfully wrote {len(lines)} lines to {ch1_script_path}")
