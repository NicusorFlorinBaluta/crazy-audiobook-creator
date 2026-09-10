"""Does more context alone fix low-confidence attribution? Yes -- answered 2026-09-10.

This is the evidence base for `TieredAttributionAdjudicator._retry_with_wide_context`.
Re-run it if the model, the prompt or the auto-accept bar changes.

The constrained-choice tier changed four things at once: a closed candidate
set, the refutation stated as fact, a much wider window, and unanimity across
runs. So it said nothing about whether *context alone* would help. This isolates
the window: same prompt, same code path, only the radii change.

Result on `the-finest-edge-of-twilight-book`, 16 sub-threshold lines, three runs
each, on a free GPU (an earlier attempt was abandoned mid-game -- 96 calls of a
4.4x-prefill prompt is not a reasonable thing to put on a card someone is
playing on, and the timings would have been noise):

        agrees_with_stored  stable_3of3  mean_conf  above_0.85  wall
narrow        16/16            16/16       0.917      13/16     5.3s/line
wide          15/16            16/16       0.954      16/16     6.1s/line

Two readings. Context buys **confidence, not stability** -- every line was
already unanimous at both widths, so the stability the constrained-choice tier
gained came from closing the question, not from the wider window. And the single
disagreement, `ch11_0222`, was the wide window catching a real error.

Caveat on the wall column: it compares one narrow call against one wide call.
It is *not* the cost of the cascade, which pays for a second call rather than
for its size. Measure that with a straight pass, as the 2026-09-10 record does.

Usage:  python scripts/experiment_attribution_context_ab.py <project-dir> [sample]

Sub-threshold lines are found rather than supplied: an adjudicated book's lines
sit at high confidence, so the detector no longer flags them. The script samples
dialogue lines, runs the narrow path over them, and A/Bs whichever ones escalate.
"""

import logging
import random
import sys
import time
from collections import Counter
from pathlib import Path

import yaml

logging.basicConfig(level=logging.ERROR, stream=sys.stdout, force=True)
from brain.director.attribution_detector import build_turn_window
from brain.director.ollama_client import OllamaClient
from brain.validators.tiered_adjudicator import TieredAttributionAdjudicator
from shared.constants import DEFAULT_OLLAMA_MODEL
from shared.models import CharacterRegistry, ScriptChapter

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "brain/projects/the-finest-edge-of-twilight-book")
SAMPLE = int(sys.argv[2]) if len(sys.argv) > 2 else 200
NARROW = (5, 8)  # the detector's defaults, i.e. what production sends
WIDE = (20, 30)
RUNS = 3

cfg = yaml.safe_load(Path("brain/config.yaml").read_text(encoding="utf-8"))
oc = cfg["ollama"]
tiered = cfg.get("external_validation", {}).get("tiered_attribution", {})
auto_accept = float(tiered.get("local_auto_accept_confidence", 0.85))

registry = CharacterRegistry.model_validate_json((ROOT / "characters.json").read_text(encoding="utf-8"))
chapters = {}
for path in sorted((ROOT / "script").glob("chapter_*.json")):
    if not path.name.endswith(".meta.json"):
        chapter = ScriptChapter.model_validate_json(path.read_text(encoding="utf-8"))
        chapters[chapter.chapter_number] = chapter

ollama = OllamaClient(
    host=oc["host"],
    model=oc.get("model", DEFAULT_OLLAMA_MODEL),
    timeout=oc.get("timeout", 600),
    context_window=int(oc.get("context_window", 16384)),
    max_output_tokens=int(oc.get("max_output_tokens", 8192)),
    think=oc.get("think"),
)
if not ollama.check_health(quiet=True):
    sys.exit(f"ollama is not answering at {ollama.host}")

# `wide_context_retry=False` throughout: the cascade this experiment produced
# must not run inside it, or every condition becomes both conditions.
adj = TieredAttributionAdjudicator(
    ollama=ollama,
    external_validator=None,
    registry=registry,
    local_auto_accept=auto_accept,
    ollama_temperature=float(tiered.get("ollama_temperature", 0.1)),
    wide_context_retry=False,
)


def build(chapter, idx, radii):
    window_radius, scene_radius = radii
    return build_turn_window(
        chapter, idx, reason="context A/B", pattern="ab", window_radius=window_radius, scene_radius=scene_radius
    )


candidates = [
    (chapter, idx)
    for chapter in chapters.values()
    for idx, line in enumerate(chapter.lines)
    if line.speaker and line.speaker != "narrator"
]
random.Random(20260910).shuffle(candidates)  # noqa: S311 - a fixed seed, so the sample is reproducible
candidates = candidates[:SAMPLE]
print(f"screening {len(candidates)} dialogue lines for sub-threshold answers...", flush=True)

targets = []
for chapter, idx in candidates:
    try:
        result = adj._adjudicate_turn_tier1(build(chapter, idx, NARROW), chapter)
    except Exception as exc:  # noqa: BLE001 - one bad line must not end the screen
        print(f"  {chapter.lines[idx].line_id}: {type(exc).__name__}: {exc}")
        continue
    if result.resolver_tier == "gemini_api":
        targets.append((chapter, idx))
print(f"sub-threshold lines: {len(targets)} of {len(candidates)}\n")

summary = {"narrow": [], "wide": []}
elapsed = {"narrow": 0.0, "wide": 0.0}
for chapter, idx in targets:
    line = chapter.lines[idx]
    out = {}
    for label, radii in (("narrow", NARROW), ("wide", WIDE)):
        answers = []
        started = time.perf_counter()
        for _ in range(RUNS):
            try:
                result = adj._adjudicate_turn_tier1(build(chapter, idx, radii), chapter)
                answers.append((result.resolved_speaker or "?", result.confidence))
            except Exception as exc:  # noqa: BLE001 - a failed run is a data point
                answers.append((f"ERR:{type(exc).__name__}", 0.0))
        elapsed[label] += time.perf_counter() - started
        ids = [a for a, _ in answers]
        top, n = Counter(ids).most_common(1)[0]
        confidence = sum(c for i, c in answers if i == top) / max(1, n)
        out[label] = (top, n == RUNS, confidence)
        summary[label].append((top == line.speaker, n == RUNS, confidence))
    print(
        f"  {line.line_id}  stored={line.speaker:16} "
        f"narrow={out['narrow'][0]:16}{'S' if out['narrow'][1] else 'u'}{out['narrow'][2]:.2f}  "
        f"wide={out['wide'][0]:16}{'S' if out['wide'][1] else 'u'}{out['wide'][2]:.2f}",
        flush=True,
    )

print()
for label in ("narrow", "wide"):
    v = summary[label]
    if not v:
        continue
    print(
        f"{label:7} agrees_with_stored={sum(1 for a, _, _ in v if a):2}/{len(v)}  "
        f"stable_{RUNS}of{RUNS}={sum(1 for _, s, _ in v if s):2}/{len(v)}  "
        f"mean_conf={sum(c for _, _, c in v) / len(v):.3f}  "
        f"above_{auto_accept}={sum(1 for _, _, c in v if c >= auto_accept):2}/{len(v)}  "
        f"wall={elapsed[label] / RUNS:6.1f}s ({elapsed[label] / RUNS / len(v):.1f}s per line)"
    )
