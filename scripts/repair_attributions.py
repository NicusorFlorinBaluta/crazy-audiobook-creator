"""Standalone dialogue attribution repair script.

Runs the Tiered Attribution Detector and Adjudicator on any project
without requiring a full pipeline re-run.

Usage:
    python scratch/repair_emberdark_attributions.py --project isles-of-the-emberdark-a-cosmere-novel-secret-projects-book-5 --dry-run
    python scratch/repair_emberdark_attributions.py --project isles-of-the-emberdark-a-cosmere-novel-secret-projects-book-5 --chapter 39 --dry-run
    python scratch/repair_emberdark_attributions.py --project isles-of-the-emberdark-a-cosmere-novel-secret-projects-book-5 --apply
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys
import yaml

from brain.director.attribution_detector import detect_suspicious_turns
from brain.director.ollama_client import OllamaClient
from brain.validators.gemini_validation import GeminiValidationService
from brain.validators.tiered_adjudicator import TieredAttributionAdjudicator
from shared.models import CharacterRegistry, ScriptChapter
from shared.artifacts import atomic_write_json, atomic_write_text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("AttributionRepair")


def load_config() -> dict:
    cfg_path = Path("brain/config.yaml")
    if not cfg_path.exists():
        cfg_path = Path(__file__).resolve().parent.parent / "brain" / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(description="Repair dialogue attributions across audiobook project.")
    parser.add_argument("--project", required=True, help="Project directory name inside brain/projects")
    parser.add_argument("--chapter", type=int, default=None, help="Target a specific chapter number (e.g. 39)")
    parser.add_argument("--dry-run", action="store_true", help="Preview proposed attribution fixes without modifying scripts")
    parser.add_argument("--apply", action="store_true", help="Write changes to disk")
    parser.add_argument("--escalate-gemini", action="store_true", help="Escalate unresolved Tier 1 lines to Gemini API")
    parser.add_argument("--local-conf", type=float, default=0.85, help="Confidence threshold for Tier 1 local Qwen auto-accept")
    args = parser.parse_args()

    # Default to dry-run if apply not specified
    dry_run = not args.apply

    # Resolve project path
    project_path = Path(args.project)
    if not project_path.is_absolute() and not project_path.exists():
        candidate = Path("brain/projects") / args.project
        if candidate.exists():
            project_path = candidate
        else:
            candidate2 = Path("projects") / args.project
            if candidate2.exists():
                project_path = candidate2

    if not project_path.exists():
        logger.error("Project directory not found: %s", args.project)
        sys.exit(1)

    scripts_dir = project_path / "script" if (project_path / "script").exists() else project_path / "scripts"
    chars_path = project_path / "characters.json"

    if not scripts_dir.exists() or not chars_path.exists():
        logger.error("Missing script(s)/ or characters.json in %s", project_path)
        sys.exit(1)


    # Load registry
    registry = CharacterRegistry.model_validate_json(chars_path.read_text(encoding="utf-8"))
    logger.info("Loaded registry with %d characters from %s", len(registry.characters), chars_path)

    # Load chapter scripts
    script_files = sorted(scripts_dir.glob("chapter_*.json"))
    chapter_scripts: list[ScriptChapter] = []
    file_map: dict[int, Path] = {}

    for sf in script_files:
        if sf.name.endswith(".meta.json"):
            continue
        try:
            ch = ScriptChapter.model_validate_json(sf.read_text(encoding="utf-8"))
            if args.chapter is not None and ch.chapter_number != args.chapter:
                continue
            chapter_scripts.append(ch)
            file_map[ch.chapter_number] = sf
        except Exception as exc:
            logger.warning("Failed to parse %s: %s", sf.name, exc)

    logger.info("Loaded %d chapter script(s) for audit/repair", len(chapter_scripts))

    # Detect suspicious turns
    suspicious = detect_suspicious_turns(chapter_scripts)
    logger.info("Detected %d suspicious turn(s) across selected chapters", len(suspicious))

    if not suspicious:
        print("\nNo suspicious dialogue turns found!")
        return

    # Print detection summary
    by_pattern: dict[str, int] = {}
    for s in suspicious:
        by_pattern[s.detection_pattern] = by_pattern.get(s.detection_pattern, 0) + 1

    print("\n" + "=" * 70)
    print("DETECTION SUMMARY:")
    for pat, count in sorted(by_pattern.items()):
        print(f"  - {pat}: {count}")
    print("=" * 70)

    # Initialize clients
    config = load_config()
    ollama_cfg = config.get("ollama", {})
    ollama = OllamaClient(
        host=ollama_cfg.get("host", "http://127.0.0.1:11435"),
        model=ollama_cfg.get("model", "qwen3.8:27b"),
        timeout=ollama_cfg.get("timeout", 600),
        think=ollama_cfg.get("think", False),
        max_output_tokens=350,
    )


    external_validator = GeminiValidationService(
        dict(config.get("external_validation", {})),
        project_path.parent,
    )

    adjudicator = TieredAttributionAdjudicator(
        ollama=ollama,
        external_validator=external_validator,
        registry=registry,
        local_auto_accept=args.local_conf,
    )

    print(f"\nRunning Tier 1 adjudication (dry_run={dry_run}, local_threshold={args.local_conf})...")
    report = adjudicator.adjudicate(
        suspicious,
        project_dir=project_path,
        chapters=chapter_scripts,
        dry_run=dry_run,
    )

    print("\n" + "=" * 70)
    print("ADJUDICATION RESULTS:")
    print(f"  Total suspicious turns: {report.summary['total_suspicious']}")
    print(f"  Local Qwen resolved:    {report.summary['local_resolved']}")
    print(f"  Escalated to Tier 2:    {report.summary['escalated_to_tier2']}")
    print(f"  Dry run mode:           {report.summary['dry_run']}")
    print("=" * 70)

    # Print diffs
    print("\nPROPOSED / APPLIED FIXES:")
    for res in report.results:
        if res.resolver_tier == "local_qwen" and res.resolved_speaker:
            status_icon = "APPLIED" if not dry_run else "PREVIEW"
            print(
                f"[{status_icon}] Ch{res.chapter_number} {res.line_id}: "
                f"'{res.original_speaker}' -> '{res.resolved_speaker}' "
                f"(conf={res.confidence:.2f}) | text={res.text[:50]!r}"
            )
            print(f"         Reason: {res.reason}")
        else:
            print(
                f"[ESCALATE] Ch{res.chapter_number} {res.line_id} (current: {res.original_speaker}): "
                f"Escalated (conf={res.confidence:.2f}) - {res.reason[:80]}"
            )

    # Save to disk if applied
    if not dry_run:
        print("\nWriting updated chapter scripts to disk...")
        for ch in chapter_scripts:
            sf = file_map.get(ch.chapter_number)
            if sf:
                atomic_write_text(sf, ch.model_dump_json(indent=2))
        atomic_write_text(chars_path, registry.model_dump_json(indent=2))

        # Also update book_script.json if present
        book_script_path = project_path / "book_script.json"
        if book_script_path.exists():
            try:
                # Update chapters in book_script.json
                bs_data = json.loads(book_script_path.read_text(encoding="utf-8"))
                script_by_num = {c.chapter_number: c.model_dump(mode="json") for c in chapter_scripts}
                if "chapters" in bs_data:
                    for idx, c_data in enumerate(bs_data["chapters"]):
                        c_num = c_data.get("chapter_number")
                        if c_num in script_by_num:
                            bs_data["chapters"][idx] = script_by_num[c_num]
                atomic_write_json(book_script_path, bs_data)
                print("Updated book_script.json")
            except Exception as exc:
                logger.warning("Could not update book_script.json: %s", exc)

        print(f"Successfully saved {len(chapter_scripts)} updated chapter scripts!")

        # Optional Tier 2 escalation
        if args.escalate_gemini:
            print("\nRunning Tier 2 escalation via GeminiValidationService...")
            escalation_result = external_validator.resolve_attributions(
                project_dir=project_path,
                chapters=chapter_scripts,
                character_ids=set(registry.characters),
                character_context={
                    cid: c.model_dump(include={"id", "name", "aliases", "gender", "age_range"}, mode="json")
                    for cid, c in registry.characters.items()
                },
            )
            print(f"Gemini escalation complete: {escalation_result}")
            # Re-save scripts with Gemini fixes
            for ch in chapter_scripts:
                sf = file_map.get(ch.chapter_number)
                if sf:
                    atomic_write_text(sf, ch.model_dump_json(indent=2))
            print("Saved post-escalation scripts to disk.")


if __name__ == "__main__":
    main()
