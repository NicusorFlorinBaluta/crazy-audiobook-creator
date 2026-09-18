r"""Generate a candidate respelling and listen back, before shipping it.

    python scripts/trial_respelling.py <project_id> <term> <candidate> [...] \
        [--lines N] [--repeats N] [--line-ids ch01_0001,...]

A respelling is a guess until audio exists for it. The dashboard's preview
endpoint synthesizes one unseeded take, which answers "does this sound right
once" -- not "does the engine hold it across a book", which is the question
that matters. This runs a candidate over real lines from the book, transcribes
each take with the same Whisper pass validation uses, and reports how often the
name lands in the right sound group.

The first argument after the term is always the control: pass the text the book
currently synthesizes (the term itself when it has no entry) so every candidate
is measured against what shipping nothing would do.

Requires the project's voice references and the voice server on port 8100
(`python -m voice.tts_server.main`), which the dashboard does not start.
Segments are written as `trial-*`, which the pipeline's `^ch\d+_` segment scan
ignores, and deleted on the way out.

Why the control matters, from the 2026-09-16 run on
`the-finest-edge-of-twilight-book`: `Drizzt` had 12 lines a listener heard as
"driz-ZIT". Regenerating those same 12 lines with the *unchanged* text put 11
of them right, while both proposed respellings scored worse and invented new
failures ("dris", "driss"). The name never needed a respelling; twelve takes
needed redrawing. Without a control arm this would have shipped a book-wide
substitution to fix a per-draw sampling artifact.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain.orchestrator.voice_client import VoiceClient
from shared.models import GenerateLineRequest, ScriptLine, ValidateRequest
from shared.pronunciation_evidence import _words, best_matching_span, phonetic_key
from shared.voice_casting import get_speaker_voice_mapping

#: Long enough to carry natural prosody, short enough that one take is quick.
MIN_LINE_CHARS = 40
MAX_LINE_CHARS = 220


def _script_lines(project_dir: Path) -> dict[str, dict[str, Any]]:
    script = json.loads((project_dir / "book_script.json").read_text(encoding="utf-8"))
    return {line["line_id"]: line for chapter in script.get("chapters", []) for line in chapter.get("lines", [])}


def _sample(lines: dict[str, dict[str, Any]], term: str, count: int, line_ids: list[str]) -> list[dict[str, Any]]:
    """Lines to synthesize: named ones, else spread across the book.

    Prefer naming the lines a measurement already found wrong
    (`pronunciation_measurement_audit.json` -> `outlier_lines`). At a base
    failure rate near 8% a random sample of a dozen lines contains about one
    failure, which cannot separate a good candidate from a lucky draw.
    """
    if line_ids:
        missing = [line_id for line_id in line_ids if line_id not in lines]
        if missing:
            print(f"warning: no such line(s) in the script: {missing}", file=sys.stderr)
        return [lines[line_id] for line_id in line_ids if line_id in lines]

    probe = _words(term)[0].casefold()
    matching = [
        line
        for line in lines.values()
        if probe in {word.casefold() for word in _words(line.get("text", ""))}
        and MIN_LINE_CHARS <= len(line.get("text", "")) <= MAX_LINE_CHARS
    ]
    step = max(1, len(matching) // count) if count else 1
    return matching[::step][:count]


def _trial(
    client: VoiceClient,
    project_id: str,
    term: str,
    variant: str,
    sample: list[dict[str, Any]],
    repeats: int,
    segments_dir: Path,
    speaker_to_voice: dict[str, str] | None = None,
) -> tuple[int, int, collections.Counter[str], list[tuple[str, str]]]:
    target = phonetic_key(term)
    heard: collections.Counter[str] = collections.Counter()
    misses: list[tuple[str, str]] = []
    on_target = total = 0

    for index, line in enumerate(sample):
        spoken = re.sub(re.escape(term), variant, line["text"], flags=re.IGNORECASE)
        speaker = line["speaker"]
        voice_id = (speaker_to_voice or {}).get(speaker) or line.get("voice_id") or speaker
        for repeat in range(repeats):
            try:
                generated = client.generate_line(
                    GenerateLineRequest(
                        project_id=project_id,
                        line=ScriptLine(
                            line_id=f"trial-{index:02d}-{repeat}",
                            speaker=speaker,
                            voice_id=voice_id,
                            text=spoken,
                            emotion=line.get("emotion") or "normal",
                        ),
                    )
                )
                validated = client.validate_segment(
                    ValidateRequest(audio_file=generated.audio_file, expected_text=spoken)
                )
            except (OSError, ValueError, KeyError) as exc:
                print(f"   !! {line['line_id']}: {exc}", file=sys.stderr)
                continue
            span, _score = best_matching_span(term, _words((validated.transcribed_text or "").strip()))
            heard[span.casefold()] += 1
            total += 1
            if phonetic_key(span) == target:
                on_target += 1
            else:
                misses.append((line["line_id"], span.casefold()))

    for stale in segments_dir.glob("trial-*.wav"):
        stale.unlink()
    return on_target, total, heard, misses


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project_id")
    parser.add_argument("term", help="the name as the book spells it")
    parser.add_argument("variants", nargs="+", help="control first, then candidates")
    parser.add_argument("--lines", type=int, default=12)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--line-ids", default="", help="comma-separated ids to use instead of a spread sample")
    args = parser.parse_args()

    project_dir = ROOT / "brain" / "projects" / args.project_id
    if not (project_dir / "book_script.json").is_file():
        print(f"no book_script.json in {project_dir}", file=sys.stderr)
        return 1
    segments_dir = ROOT / "workspace" / args.project_id / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)

    line_ids = [value.strip() for value in args.line_ids.split(",") if value.strip()]
    sample = _sample(_script_lines(project_dir), args.term, args.lines, line_ids)
    if not sample:
        print(f"no lines found containing {args.term!r}", file=sys.stderr)
        return 1

    print(f"{args.term}: {len(sample)} lines x {args.repeats} repeat(s) x {len(args.variants)} variants")
    print(f"{'variant':16} {'on target':>12}   heard as")
    print("-" * 96)

    scores: dict[str, float] = {}
    client = VoiceClient()
    speaker_to_voice = get_speaker_voice_mapping(project_dir)
    for position, variant in enumerate(args.variants):
        started = time.time()
        on_target, total, heard, misses = _trial(
            client,
            args.project_id,
            args.term,
            variant,
            sample,
            args.repeats,
            segments_dir,
            speaker_to_voice=speaker_to_voice,
        )
        if not total:
            print(f"{variant:16} {'no results':>12}")
            continue
        scores[variant] = on_target / total
        label = f"{variant} (control)" if position == 0 else variant
        print(
            f"{label:16} {f'{on_target}/{total} ({on_target / total:.0%})':>12}   "
            f"{heard.most_common(4)}  [{time.time() - started:.0f}s]"
        )
        if misses:
            print(f"{'':16} {'off:':>12}   {misses}")

    if len(scores) > 1:
        control = args.variants[0]
        best = max(scores, key=lambda variant: scores[variant])
        print()
        if best == control or scores[best] <= scores.get(control, 0):
            print(f"No candidate beat the control ({control} at {scores.get(control, 0):.0%}).")
            print("Ship nothing: the name does not need a respelling, and the failing takes need redrawing.")
        else:
            print(f"Best: {best} at {scores[best]:.0%} against {control} at {scores.get(control, 0):.0%}.")
            print("Confirm on a larger sample before writing it to the lexicon; this is a small-n result.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
