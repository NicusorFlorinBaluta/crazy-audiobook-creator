"""Apply the alias-fragment prune to a project analysed before 2026-09-10.

`_derive_character_aliases` splits a multi-word name and its id into words and
records each as an alias, so "White-Haired Being" arrives carrying `Being`,
`White` and `Haired`, and `first_company_vice_president_of_supply` carries
`Supply`, `First`, `Vice` and `President`. Since 2026-09-10 the analyser prunes
those as part of `_consolidate_accumulated_characters`, but a book analysed
before that keeps them in `characters.json` until it is re-analysed.

Re-analysis costs a full LLM pass. This applies the same deterministic prune to
an existing cast instead, reading the book text the analyser would have read.

The rule and its guarantees live in
`cast_identity.prune_ambiguous_fragment_aliases`; the important one here is that
nothing removable is ever a name, so a character the book only calls "The
Master" or "The Dark One" is untouched.

Usage:
    python scripts/repair_alias_fragments.py --project the-finest-edge-of-twilight-book
    python scripts/repair_alias_fragments.py --project the-finest-edge-of-twilight-book --apply
    python scripts/repair_alias_fragments.py --all --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from brain.director.cast_identity import prune_ambiguous_fragment_aliases
from shared.artifacts import atomic_write_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("AliasRepair")

PROJECTS = Path("brain/projects")


def _load_source_text(project_dir: Path) -> str:
    book_path = project_dir / "book.json"
    if not book_path.is_file():
        return ""
    book = json.loads(book_path.read_text(encoding="utf-8"))
    return "\n".join(str(chapter.get("text", "")) for chapter in book.get("chapters", []))


def _speaking_counts(project_dir: Path) -> dict[str, int]:
    """Attributed lines per character, so the report can show what is at stake."""
    counts: dict[str, int] = {}
    script_dir = project_dir / "script"
    files = sorted(script_dir.glob("chapter_*.json")) if script_dir.is_dir() else []
    files = [f for f in files if not f.name.endswith(".meta.json")]
    if not files and (project_dir / "book_script.json").is_file():
        files = [project_dir / "book_script.json"]
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Could not read %s: %s", path.name, exc)
            continue
        chapters = data.get("chapters", [data]) if isinstance(data, dict) else data
        for chapter in chapters:
            for line in chapter.get("lines", []):
                speaker = line.get("speaker")
                if speaker and speaker != "narrator":
                    counts[speaker] = counts.get(speaker, 0) + 1
    return counts


def repair(project_dir: Path, *, apply: bool) -> int:
    chars_path = project_dir / "characters.json"
    if not chars_path.is_file():
        logger.info("%s: no characters.json, skipping", project_dir.name)
        return 0

    source_text = _load_source_text(project_dir)
    if not source_text:
        logger.warning("%s: no book text, skipping (the rule needs it)", project_dir.name)
        return 0

    registry: dict[str, Any] = json.loads(chars_path.read_text(encoding="utf-8"))
    characters = registry.get("characters", {})
    before = {cid: list(entry.get("aliases") or []) for cid, entry in characters.items()}

    removed = prune_ambiguous_fragment_aliases(characters, source_text)
    if not removed:
        logger.info("%s: nothing to prune", project_dir.name)
        return 0

    counts = _speaking_counts(project_dir)
    logger.info("%s: %d alias(es) to remove", project_dir.name, len(removed))
    for record in removed:
        cid = record["character_id"]
        logger.info(
            "    %-16r from %-34s (%r, %d attributed lines)",
            record["alias"],
            cid,
            record["name"],
            counts.get(cid, 0),
        )

    # The prune never empties an entry, but this is the invariant that matters
    # most, so assert it against the file rather than trusting the function.
    emptied = [
        cid for cid, entry in characters.items() if before[cid] and not (entry.get("aliases") or [])
    ]
    if emptied:
        logger.error("%s: REFUSING -- these entries would lose every alias: %s", project_dir.name, emptied)
        return 0

    if not apply:
        logger.info("%s: dry run, nothing written (pass --apply to write)", project_dir.name)
        return len(removed)

    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    backup = project_dir / f"characters.json.pre-alias-repair-{stamp}"
    backup.write_text(chars_path.read_text(encoding="utf-8"), encoding="utf-8")
    atomic_write_text(chars_path, json.dumps(registry, indent=2, ensure_ascii=False))

    audit = project_dir / "alias_repair_audit.json"
    atomic_write_text(
        audit,
        json.dumps(
            {
                "applied_at": datetime.now().astimezone().isoformat(),
                "backup": backup.name,
                "removed": removed,
            },
            indent=2,
            ensure_ascii=False,
        ),
    )
    logger.info("%s: written. Backup %s, audit %s", project_dir.name, backup.name, audit.name)
    return len(removed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", help="Project directory name inside brain/projects")
    parser.add_argument("--all", action="store_true", help="Every project that has a cast and a book")
    parser.add_argument("--apply", action="store_true", help="Write changes (default is a dry run)")
    args = parser.parse_args()

    if not args.project and not args.all:
        parser.error("pass --project NAME or --all")

    if args.all:
        targets = sorted(p for p in PROJECTS.iterdir() if p.is_dir() and (p / "characters.json").is_file())
    else:
        targets = [PROJECTS / args.project]

    total = 0
    for target in targets:
        if not target.is_dir():
            logger.error("no such project: %s", target)
            continue
        total += repair(target, apply=args.apply)

    logger.info("%s: %d alias(es) across %d project(s)", "removed" if args.apply else "would remove", total, len(targets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
