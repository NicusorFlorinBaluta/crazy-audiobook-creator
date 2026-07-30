"""Update meta fingerprints for sample_book-3 to match current config."""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from brain.orchestrator.pipeline import Pipeline
from shared.artifacts import atomic_write_json, fingerprint
from brain.director.character_analyzer import _SYSTEM_PROMPT as CHARACTER_SYSTEM_PROMPT
from shared.models import CharacterRegistry, ExtractedBook

def main():
    pipeline = Pipeline()
    project_dir = Path("brain/projects/sample_book-3")
    book_path = project_dir / "book.json"
    chars_path = project_dir / "characters.json"
    chars_meta_path = project_dir / "characters.meta.json"
    
    book = ExtractedBook.model_validate_json(book_path.read_text(encoding="utf-8"))
    registry = CharacterRegistry.model_validate_json(chars_path.read_text(encoding="utf-8"))
    
    expected_character_fingerprint = fingerprint(
        {
            "book": book.model_dump(mode="json"),
            "model": pipeline.ollama.model,
            "prompt": CHARACTER_SYSTEM_PROMPT,
            "max_unique_voices": pipeline.character_analyzer.max_unique_voices,
        }
    )
    chars_meta_path.write_text(json.dumps({"fingerprint": expected_character_fingerprint}, indent=2), encoding="utf-8")
    print(f"Updated characters.meta.json fingerprint to {expected_character_fingerprint}")
    
    # Update script meta files
    scripts_dir = project_dir / "script"
    for chapter in book.chapters:
        meta_file = scripts_dir / f"chapter_{chapter.number:03d}.meta.json"
        script_file = scripts_dir / f"chapter_{chapter.number:03d}.json"
        if script_file.exists():
            dep_hash = pipeline.script_generator.chapter_fingerprint(
                chapter,
                registry,
            )
            meta_file.write_text(json.dumps({"fingerprint": dep_hash}, indent=2), encoding="utf-8")
            print(f"Updated {meta_file.name} dependency_hash")

    # Set job queue state
    job_queue = pipeline.job_queue
    job_queue.update_job("sample_book-3", {
        "script_completed": True,
        "bootstrapping_completed": True,
        "scripted_chapters": [c.number for c in book.chapters],
        "generation_chapter_selection": [2, 3],
        "status": "selection_complete",
        "active_stage": "selection_complete",
        "pause_reason": None,
    })
    print("Updated job state for sample_book-3: script_completed=True, selection=[2, 3]")

if __name__ == "__main__":
    main()
