import urllib.request
import json
from pathlib import Path

print("=== VOICE SERVER HEALTH ===")
try:
    v = json.loads(urllib.request.urlopen('http://127.0.0.1:8100/health').read().decode('utf-8'))
    print(f"Status: {v.get('status')} | GPU: {v.get('gpu')} | Model: {v.get('model_loaded')} | VRAM: {v.get('vram_used_gb', 0):.2f} GB")
except Exception as e:
    print("Voice Server Error:", e)

print("\n=== PIPELINE STATUS & CHAPTER BREAKDOWN ===")
try:
    res = urllib.request.urlopen('http://127.0.0.1:8000/api/projects/sample_book-7/status')
    data = json.loads(res.read().decode('utf-8'))
    print(f"Status: {data.get('status')} | Running: {data.get('running')} | Current Gen Chapter: {data.get('current_gen_chapter')}")
    print(f"Mastered Chapters: {data.get('mastered_chapters')}")
    print(f"Generated Chapters: {data.get('generated_chapters')}")
    print("\nChapter Details:")
    for cd in data.get('chapter_details', []):
        print(f"  Ch {cd['number']}: {cd['title']} -> {cd['lines_generated']}/{cd['total_lines']} lines ({cd['progress_percent']}%)")
except Exception as e:
    print("Status API Error:", e)
