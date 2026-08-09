import json; from pathlib import Path
def count(name):
    p = Path(f'brain/projects/{name}/book_script.json')
    if not p.exists(): return 'Not found'
    data = json.loads(p.read_text('utf-8'))
    return sum(len(c['lines']) for c in data['chapters'])
print('sample_book-v14b-e2e-val:', count('sample_book-v14b-e2e-val'))
print('sample_book-opt14b:', count('sample_book-opt14b'))
print('sample_book-e2e:', count('sample_book-e2e'))
print('sample_book-v32b-prod-e2e:', count('sample_book-v32b-prod-e2e'))
