import json
from pathlib import Path
import sys

# Simulate exactly what _prepare_generation_lines does now
project_dir = Path('e:/Projects/crazy-audiobook-creator/brain/projects/sample_book-12')

speaker_to_voice = {}
cast_file = project_dir / 'voice_cast.json'
if cast_file.exists():
    cast_data = json.loads(cast_file.read_text(encoding='utf-8'))
    for voice_id, profile in cast_data.get('voices', {}).items():
        for assigned_speaker in profile.get('assigned_characters', []):
            speaker_to_voice[assigned_speaker] = voice_id

print('narrator maps to:', speaker_to_voice.get('narrator', 'NOT FOUND'))
print('All mappings:', speaker_to_voice)
