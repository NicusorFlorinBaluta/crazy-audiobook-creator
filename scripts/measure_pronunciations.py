"""Score a project's pronunciations against what Whisper heard.

Reads the lexicon and the ASR transcripts already stored by validation, and
reports which entries the audio actually justifies -- including the ones
already shipped.

    python scripts/measure_pronunciations.py <project_id> [--apply]

Without `--apply` nothing is written. With it, the recommendation file is
rewritten so that only terms measured as `mispronounced` carry an active
respelling; everything else is reset to its own spelling, which the loader
treats as "no substitution". Verified entries in `pronunciation_dict.json` are
never rewritten -- those are human decisions. The evidence is written to
`pronunciation_measurement_audit.json` beside it either way.

Why this exists: `spoken_text` participates in the segment manifest's
dependency hash, so an active recommendation regenerates audio with no human
gate. On 2026-09-12 the LLM proposed 28 respellings for one book and 11 of
them were for names the engine already said correctly.

Why it measures the whole lexicon and not just proposals: on 2026-09-16 a
listener heard `Drizzt` said two ways. It had no entry, and the two entries
around it told the same story from the other side -- `Catti-brie` had shipped
a respelling that never worked and nothing looked at it again. A respelling is
not finished when it is applied; it is finished when the audio agrees.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.artifacts import atomic_write_json
from shared.pronunciation import (
    get_english_dictionary,
    load_pronunciation_dictionary,
    normalize_phonetic_text,
)
from shared.pronunciation_evidence import measure_terms, transcripts_for_project

STATE_DB = ROOT / "brain" / "projects" / "pipeline_state.db"

#: An unrespelled candidate is only worth measuring if the book says it enough
#: for a wobble to be audible. Terms with an entry are measured regardless.
MIN_OCCURRENCES = 5


def _entries_to_measure(
    project_dir: Path,
    recs: dict[str, Any],
    proposed: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Every term whose pronunciation this book has a stake in.

    Measuring only `proposed` was the gap that let `Catti-brie` ship a
    respelling and stay broken: once a respelling moves into the project
    dictionary it stops being a proposal, and nothing looked at it again. Three
    groups matter, and the report has to tell them apart:

    * **active** -- a substitution is being applied on every matching line.
      Measuring these is how a respelling that did not work becomes visible.
    * **keep_original** -- a verified entry equal to its own term. It overrides
      any recommendation, so a generated one silently outranks the evidence.
      That is how Emberdark's `Xisis` kept being read as "Jesus".
    * **unrespelled** -- a candidate with no entry at all. `Drizzt` lived here.
    """
    # Keyed by casefold throughout: the same name reaches this from three
    # files in three spellings -- "Catti-brie" from the project dictionary,
    # "catti-brie" from the recommendations, "Catti-brie" from the inventory --
    # and measuring it three times says nothing three times.
    entries: dict[str, dict[str, Any]] = {}

    def add(term: str, **fields: Any) -> None:
        term = term.strip()
        if term:
            entries.setdefault(term.casefold(), {"term": term, **fields})

    active, sources = load_pronunciation_dictionary(project_dir)
    for term, replacement in active.items():
        add(term, applied=replacement, source=sources.get(term), kind="active")

    verified_path = project_dir / "pronunciation_dict.json"
    if verified_path.is_file():
        try:
            verified = json.loads(verified_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            verified = {}
        for term, replacement in verified.items():
            if isinstance(replacement, str) and term.casefold() == replacement.casefold():
                add(term, applied=None, source="project", kind="keep_original")

    # Candidates carry occurrence counts, so an unrespelled name the book
    # actually says often is measured while one-off capitalisations are not.
    inventory_path = project_dir / "pronunciation_inventory.json"
    candidates: list[dict[str, Any]] = []
    if inventory_path.is_file():
        try:
            candidates = json.loads(inventory_path.read_text(encoding="utf-8")).get("candidates", [])
        except (OSError, ValueError):
            candidates = []
    english = get_english_dictionary()
    for candidate in candidates:
        term = str(candidate.get("term", "")).strip()
        if not term or candidate.get("occurrences", 0) < MIN_OCCURRENCES:
            continue
        # Cast aliases bypass the inventory's noise filter, so speaker labels
        # like "old man" arrive looking like terms. Nothing here needs a
        # respelling and the engine reads them as the English they are.
        if all(word.casefold() in english for word in re.findall(r"[A-Za-z']+", term)):
            continue
        add(term, applied=None, source=None, kind="unrespelled")

    # Deliberately not seeded from the recommendation file's keys. It
    # accumulates fragments -- "brie" is in there, left over from splitting
    # "Catti-brie", and it matches all 179 of that name's lines and tops the
    # report with a term nobody will ever respell. A proposed respelling that
    # is actually being applied arrives above via `active`; one that is not is
    # not a pronunciation this book has a stake in.
    return {fields.pop("term"): fields for fields in entries.values()}


def _evidence_is_current(project_dir: Path, connection: Any, project_id: str) -> tuple[bool, str]:
    """Is the stored audio new enough to say anything about the current lexicon?"""
    from shared.staleness import check_pronunciation_evidence_staleness

    res = check_pronunciation_evidence_staleness(project_dir)
    return res["evidence_current"], res["evidence_freshness"]


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
    entries = _entries_to_measure(project_dir, recs, proposed)

    script = json.loads(script_path.read_text(encoding="utf-8"))
    line_texts = {
        line["line_id"]: line.get("text", "")
        for chapter in script.get("chapters", [])
        for line in chapter.get("lines", [])
    }
    with sqlite3.connect(STATE_DB) as connection:
        transcripts = transcripts_for_project(connection, args.project_id)
        evidence_current, freshness = _evidence_is_current(project_dir, connection, args.project_id)

    evidence = measure_terms(list(entries), line_texts, transcripts)

    buckets: dict[str, list[str]] = {}
    kinds = {
        kind: sum(1 for e in entries.values() if e["kind"] == kind)
        for kind in ("active", "keep_original", "unrespelled")
    }
    print(
        f"{args.project_id}: {len(entries)} terms "
        f"({kinds['active']} active, {kinds['keep_original']} keep-original, {kinds['unrespelled']} unrespelled), "
        f"{len(transcripts)} transcribed lines\n"
    )
    print(f"{'term':<22} {'entry':<14} {'n':>4} {'out':>4} {'snd':>5} {'spell':>6}  {'verdict':<17} heard as")
    print("-" * 120)
    # Worst first: a term nothing enforces and a term whose respelling failed
    # are the same problem to a listener, and both hide at the bottom of an
    # alphabetical list.
    for term, item in sorted(evidence.items(), key=lambda kv: (-kv[1].outliers, kv[0])):
        buckets.setdefault(item.verdict, []).append(term)
        entry = entries[term]
        # Show what the engine is actually handed, not what the file stores:
        # `home-eye-uls` is normalised to `homeeyeuls` before synthesis, and
        # the hyphens are the whole reason that entry is worth a second look.
        applied = normalize_phonetic_text(entry["applied"]) if entry["applied"] else None
        applied = applied or ("keep" if entry["kind"] == "keep_original" else "--")
        heard = ", ".join(f"{value}x{count}" for value, count in item.heard.most_common(3))
        print(
            f"{term:<22} {str(applied)[:13]:<14} {item.samples:>4} {item.outliers:>4}"
            f" {item.stability:>4.0%} {item.spelling_stability:>5.0%}  {item.verdict:<17} {heard[:42]}"
        )

    audit = {
        "schema": 2,
        "project_id": args.project_id,
        "transcribed_lines": len(transcripts),
        "evidence_current": evidence_current,
        "evidence_freshness": freshness,
        "terms": {
            term: item.as_dict()
            | {
                "entry_kind": entries[term]["kind"],
                "entry_source": entries[term]["source"],
                "applied_respelling": entries[term]["applied"],
                "proposed": proposed[term]["default"] if term in proposed else None,
                "outlier_lines": item.outlier_lines,
            }
            for term, item in evidence.items()
        },
    }
    atomic_write_json(project_dir / "pronunciation_measurement_audit.json", audit)

    print()
    for verdict in ("mispronounced", "unstable", "undecided", "spoken_correctly", "insufficient"):
        names = buckets.get(verdict, [])
        print(f"{verdict:<17} {len(names):>3}  {names}")

    failed = [t for t in buckets.get("mispronounced", []) + buckets.get("unstable", []) if entries[t]["applied"]]
    if not evidence_current:
        print(f"\nEVIDENCE PREDATES THE LEXICON -- {freshness}.")
        print("Verdicts on unrespelled terms stand; no active entry has been heard yet, so")
        print(f"these are not failures, they are ungenerated: {failed}")
    elif failed:
        print(f"\nactive respellings that did not work: {failed}")

    if not args.apply:
        print("\n(dry run; pass --apply to rewrite the recommendation file)")
        return 0

    # `--apply` only ever touches the recommendation file. A verified entry is
    # a human decision and an unrespelled candidate has nothing to reset, so
    # widening the measured set must not widen what gets written.
    kept = 0
    reset = 0
    for term, item in evidence.items():
        if term not in recs:
            continue
        if item.verdict == "mispronounced":
            kept += 1
            continue
        # Anything not measured as wrong is reset to itself, which the loader
        # reads as "no substitution". The suggestion is not lost -- it stays in
        # the audit beside the evidence that refused it.
        recs[term] = {"default": term, "alternate": term}
        reset += 1
    atomic_write_json(recs_path, recs)
    print(f"\napplied: {kept} active respellings, {reset} reset to no-op")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
