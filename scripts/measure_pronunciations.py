"""Score a project's proposed respellings against what Whisper heard.

Reads the LLM's recommendations and the ASR transcripts already stored by
validation, and reports which respellings the audio actually justifies.

    python scripts/measure_pronunciations.py <project_id> [--apply]

Without `--apply` nothing is written. With it, the recommendation file is
rewritten so that only terms measured as `mispronounced` carry an active
respelling; everything else is reset to its own spelling, which the loader
treats as "no substitution". The evidence is written to
`pronunciation_measurement_audit.json` beside it either way.

Why this exists: `spoken_text` participates in the segment manifest's
dependency hash, so an active recommendation regenerates audio with no human
gate. On 2026-09-12 the LLM proposed 28 respellings for one book and 11 of
them were for names the engine already said correctly.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.artifacts import atomic_write_json
from shared.pronunciation_evidence import measure_terms, transcripts_for_project

STATE_DB = ROOT / "brain" / "projects" / "pipeline_state.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id")
    parser.add_argument("--apply", action="store_true", help="rewrite the recommendation file")
    args = parser.parse_args()

    project_dir = ROOT / "brain" / "projects" / args.project_id
    recs_path = project_dir / "pronunciation_recommendations.json"
    script_path = project_dir / "book_script.json"
    if not recs_path.is_file() or not script_path.is_file():
        print(f"nothing to measure in {project_dir}", file=sys.stderr)
        return 1

    recs = json.loads(recs_path.read_text(encoding="utf-8"))
    proposed = {
        term: value
        for term, value in recs.items()
        if isinstance(value, dict) and str(value.get("default", "")).casefold() != term.casefold()
    }

    script = json.loads(script_path.read_text(encoding="utf-8"))
    line_texts = {
        line["line_id"]: line.get("text", "")
        for chapter in script.get("chapters", [])
        for line in chapter.get("lines", [])
    }
    with sqlite3.connect(STATE_DB) as connection:
        transcripts = transcripts_for_project(connection, args.project_id)

    evidence = measure_terms(list(proposed), line_texts, transcripts)

    buckets: dict[str, list[str]] = {}
    print(f"{args.project_id}: {len(proposed)} proposed, {len(transcripts)} transcribed lines\n")
    print(f"{'term':<22} {'n':>4} {'dom':>5} {'spell':>6}  {'verdict':<16} heard as")
    print("-" * 108)
    for term, item in sorted(evidence.items(), key=lambda kv: kv[0]):
        buckets.setdefault(item.verdict, []).append(term)
        heard = ", ".join(f"{value}x{count}" for value, count in item.heard.most_common(3))
        print(
            f"{term:<22} {item.samples:>4} {'yes' if item.dominant_matches else 'no':>5}"
            f" {item.spelling_stability:>5.0%}  {item.verdict:<16} {heard[:48]}"
        )

    audit = {
        "schema": 1,
        "project_id": args.project_id,
        "transcribed_lines": len(transcripts),
        "terms": {term: item.as_dict() | {"proposed": proposed[term]["default"]} for term, item in evidence.items()},
    }
    atomic_write_json(project_dir / "pronunciation_measurement_audit.json", audit)

    print()
    for verdict in ("mispronounced", "undecided", "spoken_correctly", "insufficient"):
        names = buckets.get(verdict, [])
        print(f"{verdict:<16} {len(names):>3}  {names}")

    if not args.apply:
        print("\n(dry run; pass --apply to rewrite the recommendation file)")
        return 0

    kept = 0
    for term, item in evidence.items():
        if item.verdict == "mispronounced":
            kept += 1
            continue
        # Anything not measured as wrong is reset to itself, which the loader
        # reads as "no substitution". The suggestion is not lost -- it stays in
        # the audit beside the evidence that refused it.
        recs[term] = {"default": term, "alternate": term}
    atomic_write_json(recs_path, recs)
    print(f"\napplied: {kept} active respellings, {len(evidence) - kept} reset to no-op")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
