"""Rollout step 3 of the block-adjudication plan, run after the fact.

  "Dry-run both paths over the same book and diff the assignments. Inspect
   every line where they disagree -- that set is small enough to read."

The flag shipped enabled, so 616 lines of `the-finest-edge-of-twilight-book`
were decided by the block path without that diff ever being run. The per-line
path cannot simply be re-run over the current scripts: block adjudication left
those lines at high confidence, so the detector no longer flags them. Each one
is therefore rebuilt into the `SuspiciousTurn` the detector would have produced
and handed to `_adjudicate_turn_tier1` on its own.

Read-only with respect to the project. Results are appended to a JSONL
checkpoint as they are produced, so a killed run resumes instead of restarting.

The first attempt at this run took 2h13m and was abandoned: it built its own
`OllamaClient` with default options instead of the pipeline's, so `think` was
left at the model's default. `brain/config.yaml` sets `think: false` precisely
to "avoid hidden reasoning-token overhead", and without it qwen3.8 emitted a
reasoning block on every call -- a median of 566 generated tokens against the
~150 a decision actually needs, and up to 4,366. The client is now built exactly
as `Pipeline.__init__` builds it.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path

import yaml

from brain.director.attribution_detector import SuspiciousTurn
from brain.director.ollama_client import OllamaClient
from brain.validators.tiered_adjudicator import TieredAttributionAdjudicator
from shared.constants import DEFAULT_OLLAMA_MODEL
from shared.models import CharacterRegistry, ScriptChapter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
    force=True,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("BlockDiff")

WINDOW_RADIUS = 4
SCENE_RADIUS = 6
ROOT = Path("brain/projects/the-finest-edge-of-twilight-book")
CHECKPOINT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("block_diff.jsonl")
# Run with PYTHONIOENCODING=utf-8; the Ollama client logs a non-cp1252 glyph.
SUMMARY = CHECKPOINT.with_suffix(".json")


def build_turn(chapter: ScriptChapter, idx: int) -> SuspiciousTurn:
    lines = chapter.lines
    total = len(lines)
    target = lines[idx]
    w_start, w_end = max(0, idx - WINDOW_RADIUS), min(total, idx + WINDOW_RADIUS + 1)
    s_start, s_end = max(0, idx - SCENE_RADIUS), min(total, idx + SCENE_RADIUS + 1)
    return SuspiciousTurn(
        line_id=target.line_id,
        chapter_number=chapter.chapter_number,
        text=target.text,
        current_speaker=target.speaker,
        detection_reason="re-examination of a block-adjudicated line",
        detection_pattern="block_diff",
        surrounding_lines=[
            {
                "line_id": n.line_id,
                "text": n.text,
                "speaker": n.speaker,
                "speaker_confidence": n.speaker_confidence,
                "dialogue_kind": n.dialogue_kind,
                "is_target": n.line_id == target.line_id,
            }
            for n in lines[w_start:w_end]
        ],
        scene_text=" ".join(n.text.strip() for n in lines[s_start:s_end]),
    )


def main() -> int:
    config = yaml.safe_load(Path("brain/config.yaml").read_text(encoding="utf-8"))
    ollama_cfg = config.get("ollama", {})
    registry = CharacterRegistry.model_validate_json((ROOT / "characters.json").read_text(encoding="utf-8"))

    chapters: list[ScriptChapter] = []
    for path in sorted((ROOT / "script").glob("chapter_*.json")):
        if path.name.endswith(".meta.json"):
            continue
        chapters.append(ScriptChapter.model_validate_json(path.read_text(encoding="utf-8")))

    targets: list[tuple[ScriptChapter, int]] = []
    for chapter in chapters:
        for idx, line in enumerate(chapter.lines):
            if line.attribution_resolver == "local_qwen_block":
                targets.append((chapter, idx))

    done: dict[str, dict] = {}
    if CHECKPOINT.is_file():
        for raw in CHECKPOINT.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                row = json.loads(raw)
                done[row["line_id"]] = row
        logger.info("resuming: %d line(s) already in %s", len(done), CHECKPOINT.name)

    pending = [(c, i) for c, i in targets if c.lines[i].line_id not in done]
    logger.info("block-adjudicated lines: %d total, %d still to do", len(targets), len(pending))

    # Exactly as Pipeline.__init__ builds it. `think: false` is the one that
    # matters here; the first attempt omitted it and paid for a hidden reasoning
    # block on all 1,639 calls it managed in 2h13m.
    ollama = OllamaClient(
        host=ollama_cfg.get("host", "http://localhost:11434"),
        model=ollama_cfg.get("model", DEFAULT_OLLAMA_MODEL),
        fallback_models=ollama_cfg.get("fallback_models", []),
        timeout=ollama_cfg.get("timeout", 120),
        max_retries=ollama_cfg.get("max_retries", 3),
        max_retry_seconds=ollama_cfg.get("max_retry_seconds", 900),
        context_window=int(ollama_cfg.get("context_window", 8192)),
        max_output_tokens=int(ollama_cfg.get("max_output_tokens", 8192)),
        max_generation_seconds=int(ollama_cfg.get("max_generation_seconds", 600)),
        repetition_window_chars=int(ollama_cfg.get("repetition_window_chars", 512)),
        repetition_count=int(ollama_cfg.get("repetition_count", 4)),
        think=ollama_cfg.get("think"),
    )
    logger.info("ollama: %s model=%s think=%r", ollama.host, ollama.model, ollama.think)
    if not ollama.check_health(quiet=True):
        logger.error("Ollama is not answering at %s -- start it first", ollama.host)
        return 1

    tiered = dict(config.get("external_validation", {}).get("tiered_attribution", {}))
    adjudicator = TieredAttributionAdjudicator(
        ollama=ollama,
        external_validator=None,
        registry=registry,
        local_auto_accept=float(tiered.get("local_auto_accept_confidence", 0.85)),
        ollama_temperature=float(tiered.get("ollama_temperature", 0.1)),
        block_adjudication_enabled=False,  # the per-line path, deliberately
    )

    started = time.time()
    with CHECKPOINT.open("a", encoding="utf-8", buffering=1) as sink:
        for n, (chapter, idx) in enumerate(pending, start=1):
            line = chapter.lines[idx]
            call_started = time.time()
            try:
                result = adjudicator._adjudicate_turn_tier1(build_turn(chapter, idx), chapter)
                per_line, tier = result.resolved_speaker, result.resolver_tier
                reason, confidence = result.reason, result.confidence
            except Exception as exc:  # noqa: BLE001 - one bad line must not end the sweep
                per_line, tier, reason, confidence = None, "exception", f"{type(exc).__name__}: {exc}", 0.0

            if tier == "exception":
                verdict = "failed"
            elif tier not in ("local_qwen", "deterministic_tag", "deterministic_attached_tag"):
                verdict = "escalated"
            elif per_line == line.speaker:
                verdict = "agree"
            else:
                verdict = "DISAGREE"

            sink.write(
                json.dumps(
                    {
                        "line_id": line.line_id,
                        "chapter": chapter.chapter_number,
                        "text": line.text[:200],
                        "block_said": line.speaker,
                        "per_line_said": per_line,
                        "per_line_tier": tier,
                        "per_line_confidence": confidence,
                        "verdict": verdict,
                        "reason": reason[:300],
                        "seconds": round(time.time() - call_started, 1),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            if n % 10 == 0 or n == len(pending):
                rate = n / max(1e-9, time.time() - started)
                logger.info(
                    "%d/%d done (%.1f lines/min, ~%.0f min left)",
                    n, len(pending), rate * 60, (len(pending) - n) / max(1e-9, rate) / 60,
                )

    rows = [json.loads(x) for x in CHECKPOINT.read_text(encoding="utf-8").splitlines() if x.strip()]
    tally = Counter(r["verdict"] for r in rows)
    SUMMARY.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info("=" * 60)
    logger.info("re-examined : %d of %d", len(rows), len(targets))
    for verdict in ("agree", "DISAGREE", "escalated", "failed"):
        logger.info("  %-10s: %d", verdict, tally.get(verdict, 0))
    disagreements = [r for r in rows if r["verdict"] == "DISAGREE"]
    if disagreements:
        logger.info("disagreements by chapter: %s", dict(sorted(Counter(r["chapter"] for r in disagreements).items())))
        for r in disagreements[:20]:
            logger.info("  %s  block=%s  per-line=%s (%.2f)  %r",
                        r["line_id"], r["block_said"], r["per_line_said"], r["per_line_confidence"], r["text"][:70])
    logger.info("written to %s", SUMMARY)
    return 0


if __name__ == "__main__":
    sys.exit(main())
