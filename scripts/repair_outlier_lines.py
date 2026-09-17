r"""Redraw the individual takes where a name came out wrong.

    python scripts/repair_outlier_lines.py <project_id> [--term Drizzt]
        [--attempts 4] [--apply] [--limit N]

Most pronunciation failures are not a property of the name. The engine samples
per line, so an unfamiliar spelling is re-guessed on every line and loses about
one time in ten -- measured 2026-09-16, where twelve `Drizzt` lines redrawn
with the *unchanged* text came back eleven correct. The fix for those is twelve
new takes, not a respelling applied to 157 lines.

The pipeline cannot do this. Its only regeneration granularity is the chapter,
which would redraw every line at the same failure rate, clearing old errors
while introducing new ones. Validation cannot find them either: "drizzit" for
"Drizzt" passes WER comfortably.

So this reads `pronunciation_measurement_audit.json` -> `outlier_lines`,
listens to each one as it currently stands, redraws only those still wrong, and
**keeps a new take only when it is measurably better**: the name lands in the
right sound group and the take still passes the hard quality gates. A line that
will not come good in `--attempts` tries is left exactly as it was.

## What a kept take has to update

Four stores agree about a segment, and all four must move together or the
repair is either undone by the next run or invisible to the next measurement:

1. the segment wav itself;
2. `manifests/chapter_NNN.segments.json` -- the segment's `output_hash` and the
   manifest's `manifest_hash`. The reconciler drops a chapter out of
   `generated` when any stored `output_hash` stops matching the file, and would
   regenerate the whole chapter. `dependency_hash` deliberately excludes
   `output_hash`, so it does not change and the chapter is not re-derived;
3. `voice_cache.db` -> `generation_fingerprints.output_hash`, which the
   generation cache compares against the file before deciding a line can be
   reused;
4. `quality_logs` -- a row carrying the new transcript. Measurement reads the
   *last* row per line, so without this the repair is invisible to it and the
   line stays on the outlier list forever.

The chapter's **master becomes stale on purpose** -- its
`segment_manifest_hash` no longer matches -- so the chapter must be re-mastered
(`remaster_chapters.py`) **and its delivery re-exported**
(`reexport_deliveries.py`). Stopping after the re-master leaves the repair in
the workspace and absent from the M4B anyone plays, with every intermediate
check passing. Both steps are assembly, not synthesis.

The take being replaced is copied to `segments/repair-backup/` first.

Requires the voice server on port 8100, which the dashboard does not start.
Without `--apply` nothing is written and the takes are only reported.
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import sys
from pathlib import Path
from typing import Any

print = functools.partial(print, flush=True)
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain.orchestrator.voice_client import VoiceClient
from shared.models import GenerateLineRequest, ScriptLine, ValidateRequest
from shared.pronunciation import apply_pronunciations, load_pronunciation_dictionary
from shared.pronunciation_evidence import (
    _words,
    best_matching_span,
    is_pronunciation_candidate_better,
    phonetic_key,
    same_spoken_form,
    terms_in_text,
)
from shared.segment_repair import parse_chapter_number, replace_segment

CACHE_DB = ROOT / "voice_cache.db"
STATE_DB = ROOT / "brain" / "projects" / "pipeline_state.db"

#: Verdicts whose `outlier_lines` are worth redrawing. `mispronounced` is
#: excluded: when the *dominant* rendering is wrong the name needs a respelling
#: or a listener, and redrawing every line would only reshuffle the failure.
REPAIRABLE = {"unstable"}


_chapter_of = parse_chapter_number


def _load_audit(
    project_dir: Path,
    terms: list[str] | None = None,
    min_stability: float | None = None,
    max_stability: float | None = None,
) -> dict[str, list[tuple[str, str]]]:
    path = project_dir / "pronunciation_measurement_audit.json"
    if not path.is_file():
        raise SystemExit(f"no measurement audit in {project_dir}; run measure_pronunciations.py first")
    audit = json.loads(path.read_text(encoding="utf-8"))
    if not audit.get("evidence_current", True):
        print(f"warning: {audit.get('evidence_freshness')}", file=sys.stderr)
    targets: dict[str, list[tuple[str, str]]] = {}
    term_set = {t.casefold() for t in terms} if terms else None
    for name, item in audit.get("terms", {}).items():
        if term_set and name.casefold() not in term_set:
            continue
        stab = item.get("sound_stability", 0.0)
        if min_stability is not None and stab < min_stability:
            continue
        if max_stability is not None and stab >= max_stability:
            continue
        if not terms and min_stability is None and max_stability is None and item.get("verdict") not in REPAIRABLE:
            continue
        lines = [(line_id, heard) for line_id, heard in item.get("outlier_lines", [])]
        if lines:
            targets[name] = lines
    return targets


def _script_lines(project_dir: Path) -> dict[str, dict[str, Any]]:
    script = json.loads((project_dir / "book_script.json").read_text(encoding="utf-8"))
    return {line["line_id"]: line for chapter in script.get("chapters", []) for line in chapter.get("lines", [])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project_id")
    parser.add_argument("--term", action="append", default=[], help="repair specific term(s)")
    parser.add_argument("--min-stability", type=float, default=None, help="minimum sound stability (e.g. 0.80 for Group A)")
    parser.add_argument("--max-stability", type=float, default=None, help="maximum sound stability (e.g. 0.80 for Group B)")
    parser.add_argument("--attempts", type=int, default=4, help="redraws per line before giving up")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many lines")
    parser.add_argument("--apply", action="store_true", help="write the repaired takes")
    args = parser.parse_args()

    project_dir = ROOT / "brain" / "projects" / args.project_id
    segments_dir = ROOT / "workspace" / args.project_id / "segments"
    targets = _load_audit(
        project_dir,
        terms=args.term,
        min_stability=args.min_stability,
        max_stability=args.max_stability,
    )
    if not targets:
        print("nothing to repair")
        return 0

    script_lines = _script_lines(project_dir)
    mappings, _ = load_pronunciation_dictionary(project_dir)
    audit_path = project_dir / "pronunciation_measurement_audit.json"
    audit_terms = set()
    if audit_path.is_file():
        try:
            audit_terms = set(json.loads(audit_path.read_text(encoding="utf-8")).get("terms", {}).keys())
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Failed to read audit terms from %s: %s", audit_path, exc)
    glossary = audit_terms | set(targets) | {str(key) for key in mappings} | {str(value) for value in mappings.values()}
    client = VoiceClient()

    fixed: list[str] = []
    stubborn: list[tuple[str, str]] = []
    touched_chapters: set[int] = set()
    seen = 0

    for term, outliers in targets.items():
        print(f"\n=== {term}: {len(outliers)} line(s) outside the dominant sound")
        target_key = phonetic_key(term)
        for line_id, was_heard in outliers:
            if args.limit and seen >= args.limit:
                break
            seen += 1
            line = script_lines.get(line_id)
            segment_path = segments_dir / f"{line_id}.wav"
            if not line or not segment_path.is_file():
                print(f"  {line_id}  !! no script line or segment on disk; skipped")
                continue
            line_text = line.get("text", "")
            spoken = apply_pronunciations(line_text, mappings)
            line_terms = terms_in_text(f"{line_text} {spoken}", glossary) | {term}

            # The audit is a snapshot, and a line repaired by an earlier run is
            # still listed in it. Without this check a second pass redraws work
            # that is already done -- replacing good takes with other good
            # takes, and reporting "gave up" for lines that are in fact fine.
            # Listening to the audio first makes the run idempotent and makes
            # its report true.
            current = client.validate_segment(
                ValidateRequest(
                    audio_file=str(segment_path),
                    expected_text=spoken,
                    validation_terms=sorted(line_terms),
                )
            )
            heard_now, _score = best_matching_span(term, _words((current.transcribed_text or "").strip()))
            if phonetic_key(heard_now) == target_key:
                print(f"  {line_id}  already correct: heard {heard_now!r}")
                continue

            outcome = ""
            said_it_right = False
            for attempt in range(1, args.attempts + 1):
                generated = client.generate_line(
                    GenerateLineRequest(
                        project_id=args.project_id,
                        line=ScriptLine(
                            line_id=f"repair-{line_id}",
                            speaker=line["speaker"],
                            text=spoken,
                            emotion=line.get("emotion") or "normal",
                        ),
                    )
                )
                candidate = Path(generated.audio_file)
                validated = client.validate_segment(
                    ValidateRequest(
                        audio_file=str(candidate),
                        expected_text=spoken,
                        validation_terms=sorted(line_terms),
                    )
                )
                span, _score = best_matching_span(term, _words((validated.transcribed_text or "").strip()))
                on_target = phonetic_key(span) == target_key
                better = is_pronunciation_candidate_better(validated, current, line_terms)

                if on_target and better:
                    if args.apply:
                        chapter = _chapter_of(line_id)
                        rep_result = replace_segment(
                            args.project_id,
                            line_id,
                            candidate,
                            validated,
                            project_dir=project_dir,
                            segments_dir=segments_dir,
                            cache_db=CACHE_DB,
                            state_db=STATE_DB,
                            chapter=chapter,
                            repaired_by="scripts/repair_outlier_lines.py",
                        )
                        if rep_result.success:
                            touched_chapters.add(chapter)
                            current = validated
                            outcome = f"repaired on attempt {attempt}: {was_heard!r} -> {span!r}"
                        else:
                            candidate.unlink(missing_ok=True)
                            outcome = f"repair refused ({rep_result.error}); audio left alone"
                    else:
                        candidate.unlink(missing_ok=True)
                        outcome = f"would repair on attempt {attempt}: {was_heard!r} -> {span!r}"
                    fixed.append(line_id)
                    break

                candidate.unlink(missing_ok=True)
                if on_target:
                    said_it_right = True
                    # Check if another term regressed
                    cand_words = _words(validated.transcribed_text or "")
                    curr_words = _words(current.transcribed_text or "")
                    regressed = []
                    for t in line_terms:
                        c_span, _ = best_matching_span(t, curr_words)
                        v_span, _ = best_matching_span(t, cand_words)
                        c_ok = bool(c_span and (same_spoken_form(t, c_span) or phonetic_key(c_span) == phonetic_key(t)))
                        v_ok = bool(v_span and (same_spoken_form(t, v_span) or phonetic_key(v_span) == phonetic_key(t)))
                        if c_ok and not v_ok:
                            regressed.append(t)
                    if regressed:
                        outcome = f"attempt {attempt}: said {term!r} right, but broke other names: {regressed}"
                    else:
                        outcome = f"attempt {attempt}: said it right, but failed quality gates"
                else:
                    outcome = f"attempt {attempt}: still {span!r}"
            else:
                stubborn.append((line_id, was_heard))
                if not outcome.startswith("gave up"):
                    outcome = f"gave up after {args.attempts} ({outcome})"
            print(f"  {line_id}  {outcome}")

    print(f"\n{len(fixed)} repaired, {len(stubborn)} left alone")
    if stubborn:
        print(f"still wrong: {stubborn}")
    if not args.apply:
        print("\n(dry run; pass --apply to write the repaired takes)")
    elif touched_chapters:
        print(f"re-master these chapters: {sorted(touched_chapters)}")
        print("  python scripts/remaster_chapters.py <project> " + " ".join(str(c) for c in sorted(touched_chapters)))
        print("then re-export, or the repair never reaches a listener:")
        print("  python scripts/reexport_deliveries.py <project> --stale")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
