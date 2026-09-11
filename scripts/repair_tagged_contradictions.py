"""Run the deterministic refutation repairs against a finished script.

Since 2026-09-11 the pipeline runs this pass itself
(`brain/director/attribution_audit.apply_refutation_repairs`, wired into
`_run_script_director`). This script is the way to apply it to a book that was
scripted *before* that, or to re-apply it after a rule changes.

It deliberately holds no attribution logic of its own. It loads the chapters,
calls the same function the pipeline calls, and saves what changed -- because
two copies of attribution logic drift, and the drift is invisible until a book
ships with it.

What the pass does, cheapest layer first:

* a tag that **names** someone is the answer -- the stored speaker is replaced
  at confidence 1.0, exactly as the live path does;
* a tag that yields only a gender, or a descriptor that contradicts the stored
  speaker, cannot name a winner but is decisive about who did *not* speak;
* where a refutation leaves exactly one candidate in the scene, that is the
  answer, and nobody has to read the book to reach it;
* where it leaves several, `--llm` asks the local model to choose from the
  list -- the only layer that costs a call.

Usage:
    python scripts/repair_tagged_contradictions.py --project the-finest-edge-of-twilight-book
    python scripts/repair_tagged_contradictions.py --project the-finest-edge-of-twilight-book --apply --llm
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from brain.director.attribution_audit import apply_refutation_repairs, refresh_attribution_audit
from brain.director.script_generator import ScriptGenerator
from shared.artifacts import atomic_write_text
from shared.models import CharacterRegistry, ScriptChapter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("TagRepair")

PROJECTS = Path("brain/projects")


def _ollama_client() -> Any | None:
    """The pipeline's own client, built from the pipeline's own config.

    Built exactly as `Pipeline.__init__` builds it. A previous run of a
    neighbouring script built one with default options instead, leaving `think`
    at the model default -- `brain/config.yaml` sets `think: false` precisely to
    "avoid hidden reasoning-token overhead" -- and cost 2h13m to a median 566
    generated tokens per call against the ~150 a decision needs.
    """
    from brain.director.ollama_client import OllamaClient
    from shared.constants import DEFAULT_OLLAMA_MODEL

    config = yaml.safe_load(Path("brain/config.yaml").read_text(encoding="utf-8"))
    ollama_cfg = config.get("ollama", {})
    ollama = OllamaClient(
        host=ollama_cfg.get("host", "http://localhost:11434"),
        model=ollama_cfg.get("model", DEFAULT_OLLAMA_MODEL),
        timeout=ollama_cfg.get("timeout", 600),
        max_retries=ollama_cfg.get("max_retries", 3),
        context_window=int(ollama_cfg.get("context_window", 16384)),
        max_output_tokens=int(ollama_cfg.get("max_output_tokens", 8192)),
        think=ollama_cfg.get("think"),
    )
    if not ollama.check_health(quiet=True):
        logger.error("Ollama is not answering at %s; skipping the constrained-choice tier", ollama.host)
        return None
    return ollama


def repair(project_dir: Path, *, apply: bool, use_llm: bool = False) -> dict[str, int]:
    registry = CharacterRegistry.model_validate_json((project_dir / "characters.json").read_text(encoding="utf-8"))

    chapter_paths: dict[int, Path] = {}
    chapters: list[ScriptChapter] = []
    for path in sorted((project_dir / "script").glob("chapter_*.json")):
        if path.name.endswith(".meta.json"):
            continue
        chapter = ScriptChapter.model_validate_json(path.read_text(encoding="utf-8"))
        chapters.append(chapter)
        chapter_paths[chapter.chapter_number] = path

    before = {line.line_id: (line.speaker, line.attribution_review_required) for c in chapters for line in c.lines}

    result = apply_refutation_repairs(
        chapters,
        registry,
        ollama=_ollama_client() if use_llm else None,
        apply=apply,
    )
    counts: dict[str, int] = dict(result["counts"])
    records: list[dict[str, Any]] = result["records"]

    for record in records:
        action = record.get("action")
        if action == "renamed":
            logger.info("    %s  %s -> %s   %r", record["line_id"], record["from"], record["to"], record["tag"])
        elif action == "auto_resolved":
            logger.info(
                "    %s  %s -> %s   (%s: %s)",
                record["line_id"], record["from"], record["to"], record["source"], record["reason"][:90],
            )
        elif action == "unflagged":
            logger.info("    %s  %s stays, stale review flag retracted   %r", record["line_id"], record["speaker"], record["tag"])
        else:
            logger.info(
                "    %s  %s stays, flagged for review (%s)   %r",
                record["line_id"], record["speaker"], record["tag_says"], record["tag"],
            )

    counts["chapters_written"] = 0
    if apply:
        for chapter in chapters:
            if any(
                before.get(line.line_id) != (line.speaker, line.attribution_review_required) for line in chapter.lines
            ):
                atomic_write_text(chapter_paths[chapter.chapter_number], chapter.model_dump_json(indent=2))
                counts["chapters_written"] += 1

        if counts["renamed"] or counts["auto_resolved"]:
            # A rename moves a line between characters, and `dialogue_count` is
            # what `cast_identity.choose_primary` uses to decide which side of a
            # merge survives. Leaving it stale would be a quiet second bug.
            ScriptGenerator.sync_dialogue_counts(chapters, registry)
            atomic_write_text(project_dir / "characters.json", registry.model_dump_json(indent=2))
            logger.info("    resynced dialogue_count and rewrote characters.json")

        if records:
            atomic_write_text(
                project_dir / "tag_contradiction_repair_audit.json",
                json.dumps(
                    {"applied_at": datetime.now().astimezone().isoformat(), "records": records},
                    indent=2,
                    ensure_ascii=False,
                ),
            )

        if counts["chapters_written"]:
            # Rewriting a chapter invalidates `attribution_audit.json`, and a
            # report nobody refreshes is worse than no report -- it reads as
            # current. Emberdark's sat a week stale, reporting 10 issues where
            # the scripts had 17.
            report = refresh_attribution_audit(project_dir)
            logger.info(
                "    refreshed attribution_audit.json: passed=%s issues=%d",
                report.get("passed"),
                len(report.get("issues", [])),
            )
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=True, help="Project directory name inside brain/projects")
    parser.add_argument("--apply", action="store_true", help="Write changes (default is a dry run)")
    parser.add_argument(
        "--llm",
        action="store_true",
        help="Also ask the local model to choose, for refuted lines with two or more candidates",
    )
    args = parser.parse_args()

    project_dir = Path(args.project)
    if not project_dir.exists():
        project_dir = PROJECTS / args.project
    if not project_dir.exists():
        logger.error("No such project: %s", args.project)
        return 1

    logger.info("%s %s", "Applying to" if args.apply else "Dry run over", project_dir)
    counts = repair(project_dir, apply=args.apply, use_llm=args.llm)
    logger.info(
        "renamed=%d auto_resolved=%d flagged=%d unflagged=%d chapters_written=%d",
        counts["renamed"], counts["auto_resolved"], counts["flagged"], counts["unflagged"],
        counts["chapters_written"],
    )
    if not args.apply:
        logger.info("Dry run -- nothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
