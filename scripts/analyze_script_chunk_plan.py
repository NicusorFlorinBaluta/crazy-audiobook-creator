"""Compare script chunk plans without loading or calling an LLM."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain.director.script_generator import ScriptGenerator


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--plans", default="350:40,550:60")
    args = parser.parse_args()
    book = json.loads(
        (ROOT / "brain" / "projects" / args.project / "book.json").read_text(encoding="utf-8")
    )
    result = []
    for raw in args.plans.split(","):
        words, fragments = (int(value) for value in raw.split(":"))
        generator = ScriptGenerator(
            ollama=None, chunk_size_words=words,
            max_fragments_per_chunk=fragments,
        )
        chapter_calls = []
        chunk_words = []
        chunk_fragments = []
        for chapter in book["chapters"]:
            spans = generator._split_into_fragment_spans(chapter["text"])
            chunks = generator._chunk_fragments(spans)
            chapter_calls.append(len(chunks))
            for chunk in chunks:
                chunk_fragments.append(len(chunk))
                chunk_words.append(sum(len(item.text.split()) for item in chunk))
        result.append({
            "chunk_size_words": words,
            "max_fragments": fragments,
            "total_calls": sum(chapter_calls),
            "calls_by_chapter": chapter_calls,
            "maximum_chunk_words": max(chunk_words),
            "maximum_chunk_fragments": max(chunk_fragments),
            "average_chunk_words": sum(chunk_words) / len(chunk_words),
        })
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
