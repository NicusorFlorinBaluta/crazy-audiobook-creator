import json
with open('e:/Projects/crazy-audiobook-creator/voice_library/sample_book-12/voices.json') as f:
    data = json.load(f)
    nm = data['voices'].get('narrator_male', {})
    print('narrator_male file:', nm.get('file'))
    print('narrator_male keys:', list(nm.keys()))
