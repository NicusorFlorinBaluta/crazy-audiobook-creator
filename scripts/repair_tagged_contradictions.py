"""Reconcile stored speakers with the speech tags the author attached.

The guardrail in `tiered_adjudicator` only sees a tag while attribution is
running. A book scripted before 2026-09-10 was checked against the narrower
gate -- narrator lines beginning lower-case only -- so roughly half its tags
were never consulted, and a contradiction between a stored speaker and its own
tag can sit in a finished script with `attribution_review_required = False`.

This applies the 2026-09-06 rule to a finished script, and nothing more:

* a tag that **names** someone is the answer. The stored speaker is replaced,
  recorded under `deterministic_attached_tag` at confidence 1.0, exactly as the
  live path does.
* a tag that yields only a **gender** cannot name a winner, but is decisive
  about who did *not* speak. Those lines are flagged for review rather than
  guessed at.

Deliberately no LLM: everything here is the author's own words against the
stored label.

Usage:
    python scripts/repair_tagged_contradictions.py --project the-finest-edge-of-twilight-book
    python scripts/repair_tagged_contradictions.py --project the-finest-edge-of-twilight-book --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from brain.director.script_generator import ScriptGenerator
from brain.validators.tiered_adjudicator import (
    _HE_SPEECH_TAG,
    _SHE_SPEECH_TAG,
    _reads_as_attached_tag,
)
from shared.artifacts import atomic_write_text
from shared.constants import Gender
from shared.models import CharacterRegistry, ScriptChapter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("TagRepair")

PROJECTS = Path("brain/projects")


def _tag_evidence(tag: str, registry: CharacterRegistry) -> tuple[str | None, Gender | None]:
    named, kind, gender = ScriptGenerator._dialogue_tag_evidence(tag, registry)
    if (
        gender is not None
        and kind == "pronoun_gender"
        and not (_HE_SPEECH_TAG.search(tag) or _SHE_SPEECH_TAG.search(tag))
    ):
        # A lone pronoun elsewhere in the sentence is not the speaker.
        gender = None
    return named, gender


def _names_a_proper_noun(tag: str, resolved: str, registry: CharacterRegistry) -> bool:
    """Did the tag reach `resolved` through an actual name, or a descriptor?

    "Gregory replied with a blank stare." names Gregory. "the man said to Dusk."
    reaches `minor_male` through a generic descriptor -- decisive about who did
    *not* speak, silent about who did. Only the first may rename a line.
    """
    character = registry.characters.get(resolved)
    if character is None:
        return False
    candidates = [character.name or "", *(character.aliases or [])]
    for candidate in candidates:
        token = candidate.strip()
        if not token or not token[:1].isupper():
            continue
        # Articles and lower-case descriptors never qualify, so a capitalised
        # first character is the test, applied to the form found in the tag.
        if re.search(rf"(?<!\w){re.escape(token)}(?!\w)", tag):
            return True
    return False


def _descriptor_gender(resolved: str, registry: CharacterRegistry) -> Gender | None:
    """The gender a descriptor-only match still establishes, if any."""
    character = registry.characters.get(resolved)
    if character is None:
        return None
    return character.gender if character.gender in (Gender.MALE, Gender.FEMALE) else None


def repair(project_dir: Path, *, apply: bool) -> dict[str, int]:
    registry = CharacterRegistry.model_validate_json(
        (project_dir / "characters.json").read_text(encoding="utf-8")
    )
    counts = {"renamed": 0, "flagged": 0, "chapters_written": 0}
    records: list[dict[str, Any]] = []

    for path in sorted((project_dir / "script").glob("chapter_*.json")):
        if path.name.endswith(".meta.json"):
            continue
        chapter = ScriptChapter.model_validate_json(path.read_text(encoding="utf-8"))
        lines = chapter.lines
        dirty = False

        for index, line in enumerate(lines):
            if not line.speaker or line.speaker == "narrator" or index + 1 >= len(lines):
                continue
            following = lines[index + 1]
            if following.speaker != "narrator":
                continue
            tag = str(following.text or "").strip()
            if not _reads_as_attached_tag(tag):
                continue

            named, gender = _tag_evidence(tag, registry)
            descriptor_match: str | None = None
            if named and named != line.speaker and not _names_a_proper_noun(tag, named, registry):
                # "the man said to Dusk." resolves to `minor_male` through a
                # generic descriptor, not through the author naming anybody.
                # It is decisive that Dusk did not speak -- he is the addressee
                # -- and silent about who did. Renaming a character to a
                # placeholder on a descriptor match would be a downgrade
                # dressed up as a correction, so it is flagged instead. The
                # signal is kept either way: dropping it because the descriptor
                # happens to share the stored speaker's gender would discard a
                # contradiction the author wrote down.
                descriptor_match, named = named, None

            if named and named != line.speaker:
                record = {
                    "line_id": line.line_id,
                    "action": "renamed",
                    "from": line.speaker,
                    "to": named,
                    "tag": tag[:160],
                }
                counts["renamed"] += 1
                if apply:
                    line.speaker = named
                    line.speaker_confidence = 1.0
                    line.speaker_evidence = f"Attached speech tag: {tag}"[:4000]
                    line.attribution_resolver = "deterministic_attached_tag"
                    line.attribution_review_required = False
                    line.attribution_review_reason = ""
                    dirty = True
                records.append(record)
                continue

            reason = ""
            detail = ""
            if descriptor_match:
                reason = (
                    f"The attached speech tag describes the speaker in terms that fit "
                    f"{descriptor_match!r}, not {line.speaker!r}. A descriptor cannot name "
                    "who did speak, only who did not."
                )
                detail = f"descriptor -> {descriptor_match}"
            elif named is None and gender is not None:
                candidate = registry.characters.get(line.speaker)
                if candidate and candidate.gender in (Gender.MALE, Gender.FEMALE) and candidate.gender != gender:
                    reason = (
                        f"The attached speech tag identifies a {gender.value} speaker; "
                        f"{line.speaker!r} is {candidate.gender.value}. The tag cannot name "
                        "who did speak, only who did not."
                    )
                    detail = f"gender -> {gender.value}"

            if reason:
                record = {
                    "line_id": line.line_id,
                    "action": "flagged",
                    "speaker": line.speaker,
                    "tag_says": detail,
                    "tag": tag[:160],
                }
                counts["flagged"] += 1
                if apply and not line.attribution_review_required:
                    line.attribution_review_required = True
                    line.attribution_review_reason = reason
                    dirty = True
                records.append(record)

        if apply and dirty:
            atomic_write_text(path, chapter.model_dump_json(indent=2))
            counts["chapters_written"] += 1

    for record in records:
        if record["action"] == "renamed":
            logger.info("    %s  %s -> %s   %r", record["line_id"], record["from"], record["to"], record["tag"])
        else:
            logger.info(
                "    %s  %s stays, flagged for review (%s)   %r",
                record["line_id"], record["speaker"], record["tag_says"], record["tag"],
            )

    if apply and records:
        atomic_write_text(
            project_dir / "tag_contradiction_repair_audit.json",
            json.dumps(
                {"applied_at": datetime.now().astimezone().isoformat(), "records": records},
                indent=2,
                ensure_ascii=False,
            ),
        )
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=True, help="Project directory name inside brain/projects")
    parser.add_argument("--apply", action="store_true", help="Write changes (default is a dry run)")
    args = parser.parse_args()

    project_dir = PROJECTS / args.project
    if not (project_dir / "characters.json").is_file():
        logger.error("no cast at %s", project_dir)
        return 1

    counts = repair(project_dir, apply=args.apply)
    logger.info(
        "%s: %d renamed by a naming tag, %d flagged for review by a gendering tag%s",
        args.project,
        counts["renamed"],
        counts["flagged"],
        f", {counts['chapters_written']} chapter file(s) written" if args.apply else " (dry run)",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
