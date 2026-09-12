"""Re-stamp chapter script fingerprints after an out-of-band repair.

`Pipeline._script_artifacts_current` fingerprints each chapter against the
registry -- including every dependency character's alias list. So a repair that
legitimately edits `characters.json` (alias pruning, a `dialogue_count` resync)
marks **every chapter stale**, and the next run schedules a book-wide re-script
before it will generate any more audio.

That is the right default: a changed dependency usually does mean the cached
script no longer reflects its inputs. It is the wrong answer when the scripts
have already been brought forward by the repair itself -- which is what the
repair scripts in this directory do. On `the-finest-edge-of-twilight` the alias
prune removed 19 aliases and stranded 32 of 32 chapters, with 8 chapters
generated, 5 mastered and Part 01 already published.

This re-stamps the fingerprint (and the `speaker_dependency_ids` it is computed
over) from the scripts as they now stand, so "current" means what it says.

Only run this when the scripts really are current -- i.e. re-running the script
director would be redundant, not merely inconvenient. Verify first with a dry
run of the repair scripts: if they still propose changes, apply those instead.

    python scripts/refresh_script_fingerprints.py --project <name>
    python scripts/refresh_script_fingerprints.py --project <name> --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import yaml

from brain.director.ollama_client import OllamaClient
from brain.director.script_generator import ScriptGenerator
from shared.artifacts import atomic_write_json
from shared.constants import DEFAULT_OLLAMA_MODEL
from shared.models import CharacterRegistry, ExtractedBook, ScriptChapter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("FingerprintRefresh")

PROJECTS = Path("brain/projects")


def _script_generator() -> ScriptGenerator:
    """Built the way `Pipeline.__init__` builds it -- the fingerprint includes the model name."""
    config = yaml.safe_load(Path("brain/config.yaml").read_text(encoding="utf-8"))
    ollama_cfg = config.get("ollama", {})
    script_cfg = dict(config.get("script", {}))
    ollama = OllamaClient(
        host=ollama_cfg.get("host", "http://localhost:11434"),
        model=ollama_cfg.get("model", DEFAULT_OLLAMA_MODEL),
        timeout=ollama_cfg.get("timeout", 600),
    )
    accepted = set(ScriptGenerator.__init__.__code__.co_varnames)
    return ScriptGenerator(ollama=ollama, **{k: v for k, v in script_cfg.items() if k in accepted})


def refresh(project_dir: Path, *, apply: bool) -> int:
    generator = _script_generator()
    book = ExtractedBook.model_validate_json((project_dir / "book.json").read_text(encoding="utf-8"))
    registry = CharacterRegistry.model_validate_json((project_dir / "characters.json").read_text(encoding="utf-8"))
    scripts_dir = project_dir / "script"

    restamped = 0
    for chapter in book.chapters:
        script_path = scripts_dir / f"chapter_{chapter.number:03d}.json"
        meta_path = scripts_dir / f"chapter_{chapter.number:03d}.meta.json"
        if not script_path.exists() or not meta_path.exists():
            logger.warning("  chapter %d: no cached script; leaving it stale", chapter.number)
            continue

        script = ScriptChapter.model_validate_json(script_path.read_text(encoding="utf-8"))
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        # Recomputed from the script as it now stands, not carried over: a
        # repair can move a line to a speaker the original dependency set never
        # contained, and the fingerprint has to cover who actually speaks.
        speaker_ids = sorted({line.speaker for line in script.lines if line.speaker})
        expected = generator.chapter_fingerprint(chapter, registry, set(speaker_ids))
        if metadata.get("fingerprint") == expected and metadata.get("speaker_dependency_ids") == speaker_ids:
            continue

        restamped += 1
        added = set(speaker_ids) - set(metadata.get("speaker_dependency_ids") or [])
        removed = set(metadata.get("speaker_dependency_ids") or []) - set(speaker_ids)
        detail = ""
        if added or removed:
            detail = f"  speakers +{sorted(added)} -{sorted(removed)}"
        logger.info("  chapter %d: re-stamping%s", chapter.number, detail)
        if apply:
            metadata["fingerprint"] = expected
            metadata["speaker_dependency_ids"] = speaker_ids
            atomic_write_json(meta_path, metadata)

    return restamped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=True, help="Project directory name inside brain/projects")
    parser.add_argument("--apply", action="store_true", help="Write the fingerprints (default is a dry run)")
    args = parser.parse_args()

    project_dir = Path(args.project)
    if not project_dir.exists():
        project_dir = PROJECTS / args.project
    if not project_dir.exists():
        logger.error("No such project: %s", args.project)
        return 1

    logger.info("%s %s", "Re-stamping" if args.apply else "Dry run over", project_dir)
    count = refresh(project_dir, apply=args.apply)
    logger.info("%d chapter(s) %s", count, "re-stamped" if args.apply else "would be re-stamped")
    if not args.apply:
        logger.info("Dry run -- nothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
