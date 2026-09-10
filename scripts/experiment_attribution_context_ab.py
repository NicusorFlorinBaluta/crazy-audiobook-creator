"""Does more context alone fix low-confidence attribution? (UNANSWERED)

Run this on a free GPU. A first attempt on 2026-09-10 was abandoned: the
machine was running a game, and 96 calls of a 4.4x-prefill prompt is not a
reasonable thing to put on a card someone is playing on. No result yet.

The constrained-choice tier changed four things at once: a closed candidate
set, the refutation stated as fact, a much wider window, and unanimity across
runs. So it says nothing about whether *context alone* would help.

These 16 lines are the ones the per-line path resolved at 0.80-0.83 -- just
under the 0.85 auto-accept bar -- with no refutation and no candidate set.
They are exactly the "low confidence autofix" case. Same prompt, same code
path, only the window changes.
"""
import json
import logging
import sys
from collections import Counter
from pathlib import Path

import yaml

logging.basicConfig(level=logging.ERROR, stream=sys.stdout, force=True)
from brain.director.attribution_detector import SuspiciousTurn
from brain.director.ollama_client import OllamaClient
from brain.validators.tiered_adjudicator import TieredAttributionAdjudicator
from shared.constants import DEFAULT_OLLAMA_MODEL
from shared.models import CharacterRegistry, ScriptChapter

ROOT = Path("brain/projects/the-finest-edge-of-twilight-book")
SP = Path(sys.argv[1])  # directory holding block_diff.jsonl from
# scripts/diff_block_vs_per_line_attribution.py
rows = [json.loads(l) for l in (SP / "block_diff.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
targets = [r for r in rows if r["verdict"] == "escalated"]
print(f"low-confidence lines: {len(targets)}")

cfg = yaml.safe_load(Path("brain/config.yaml").read_text(encoding="utf-8"))
oc = cfg["ollama"]
registry = CharacterRegistry.model_validate_json((ROOT / "characters.json").read_text(encoding="utf-8"))
chapters = {}
for p in sorted((ROOT / "script").glob("chapter_*.json")):
    if not p.name.endswith(".meta.json"):
        c = ScriptChapter.model_validate_json(p.read_text(encoding="utf-8"))
        chapters[c.chapter_number] = c

ollama = OllamaClient(host=oc["host"], model=oc.get("model", DEFAULT_OLLAMA_MODEL), timeout=oc.get("timeout", 600),
                      context_window=int(oc.get("context_window", 16384)),
                      max_output_tokens=int(oc.get("max_output_tokens", 8192)), think=oc.get("think"))
adj = TieredAttributionAdjudicator(ollama=ollama, external_validator=None, registry=registry,
                                   local_auto_accept=0.85, ollama_temperature=0.1,
                                   block_adjudication_enabled=False)

def build(chapter, idx, wr, sr):
    lines = chapter.lines
    n = len(lines)
    t = lines[idx]
    a, b = max(0, idx - wr), min(n, idx + wr + 1)
    c, d = max(0, idx - sr), min(n, idx + sr + 1)
    return SuspiciousTurn(
        line_id=t.line_id, chapter_number=chapter.chapter_number, text=t.text,
        current_speaker=t.speaker, detection_reason="context A/B", detection_pattern="ab",
        surrounding_lines=[{"line_id": x.line_id, "text": x.text, "speaker": x.speaker,
                            "speaker_confidence": x.speaker_confidence, "dialogue_kind": x.dialogue_kind,
                            "is_target": x.line_id == t.line_id} for x in lines[a:b]],
        scene_text=" ".join(x.text.strip() for x in lines[c:d]))

RUNS = 3
summary = {"narrow": [], "wide": []}
for r in targets:
    ch = chapters[r["chapter"]]
    idx = next(i for i, l in enumerate(ch.lines) if l.line_id == r["line_id"])
    line = ch.lines[idx]
    out = {}
    for label, (wr, sr) in (("narrow", (4, 6)), ("wide", (20, 30))):
        answers = []
        for _ in range(RUNS):
            try:
                res = adj._adjudicate_turn_tier1(build(ch, idx, wr, sr), ch)
                answers.append((res.resolved_speaker or "?", res.confidence))
            except Exception as exc:
                answers.append((f"ERR:{type(exc).__name__}", 0.0))
        ids = [a for a, _ in answers]
        top, n = Counter(ids).most_common(1)[0]
        conf = sum(c for i, c in answers if i == top) / max(1, n)
        out[label] = (top, n == RUNS, conf)
        summary[label].append((top == line.speaker, n == RUNS, conf))
    print(f"  {r['line_id']}  stored={line.speaker:16} "
          f"narrow={out['narrow'][0]:16}{'S' if out['narrow'][1] else 'u'}{out['narrow'][2]:.2f}  "
          f"wide={out['wide'][0]:16}{'S' if out['wide'][1] else 'u'}{out['wide'][2]:.2f}")

print()
for label in ("narrow", "wide"):
    v = summary[label]
    print(f"{label:7} agrees_with_stored={sum(1 for a,_,_ in v if a):2}/{len(v)}  "
          f"stable_3of3={sum(1 for _,s,_ in v if s):2}/{len(v)}  "
          f"mean_conf={sum(c for _,_,c in v)/len(v):.3f}  "
          f"above_0.85={sum(1 for _,_,c in v if c>=0.85):2}/{len(v)}")
