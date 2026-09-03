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
    parser.add_argument("--start-chapter", type=int, default=None, help="Start chapter number (inclusive, e.g. 40)")
    parser.add_argument("--end-chapter", type=int, default=None, help="End chapter number (inclusive, e.g. 63)")
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
            if args.start_chapter is not None and ch.chapter_number < args.start_chapter:
                continue
            if args.end_chapter is not None and ch.chapter_number > args.end_chapter:
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

    print(f"\nProcessing {len(chapter_scripts)} chapter(s) (dry_run={dry_run}, local_threshold={args.local_conf}, escalate_gemini={args.escalate_gemini})...\n")

    all_results = []
    total_suspicious = 0
    total_local_resolved = 0
    total_escalated = 0
    total_repairs = 0

    for ch in chapter_scripts:
        ch_num = ch.chapter_number
        sf = file_map.get(ch_num)
        ch_suspicious = detect_suspicious_turns([ch])

        if not ch_suspicious:
            logger.info("Chapter %d: Clean (0 suspicious turns)", ch_num)
            continue

        print("\n" + "=" * 60)
        print(f"CHAPTER {ch_num}: Found {len(ch_suspicious)} suspicious turn(s)")
        print("=" * 60)

        ch_report = adjudicator.adjudicate(
            ch_suspicious,
            project_dir=project_path,
            chapters=[ch],
            dry_run=dry_run,
        )

        all_results.extend(ch_report.results)
        total_suspicious += ch_report.summary["total_suspicious"]
        total_local_resolved += ch_report.summary["local_resolved"]
        total_escalated += ch_report.summary["escalated_to_tier2"]

        # Print fixes for this chapter
        ch_repairs = [
            r for r in ch_report.results
            if r.resolver_tier == "local_qwen" and r.resolved_speaker and r.resolved_speaker != r.original_speaker
        ]
        total_repairs += len(ch_repairs)
        if ch_repairs:
            print(f"  Repairs in Chapter {ch_num} ({len(ch_repairs)}):")
            for r in ch_repairs:
                status_icon = "APPLIED" if not dry_run else "PREVIEW"
                print(f"    [{status_icon}] {r.line_id}: '{r.original_speaker}' -> '{r.resolved_speaker}' (conf={r.confidence:.2f}) | {r.text[:45]!r}")
        else:
            print(f"  No speaker changes in Chapter {ch_num} (all existing attributions verified)")

        # Optional Tier 2 escalation for lines Qwen couldn't resolve
        unresolved_in_ch = [line for line in ch.lines if getattr(line, "attribution_review_required", False)]
        if args.escalate_gemini and unresolved_in_ch:
            print(f"  Escalating {len(unresolved_in_ch)} turn(s) in Chapter {ch_num} to Gemini API...")
            try:
                esc_res = external_validator.resolve_attributions(
                    project_dir=project_path,
                    chapters=[ch],
                    character_ids=set(registry.characters),
                    character_context={
                        cid: c.model_dump(include={"id", "name", "aliases", "gender", "age_range"}, mode="json")
                        for cid, c in registry.characters.items()
                    },
                )
                print(f"  Gemini escalation result: {esc_res}")
            except Exception as exc:
                logger.warning("Gemini escalation failed for Chapter %d: %s", ch_num, exc)

        # Save this chapter immediately to disk if applied
        if not dry_run and sf:
            atomic_write_text(sf, ch.model_dump_json(indent=2))
            logger.info("Saved Chapter %d atomically to %s", ch_num, sf.name)

    # Final summary across all processed chapters
    print("\n" + "=" * 70)
    print("ALL PROCESSED CHAPTERS SUMMARY:")
    print(f"  Total suspicious turns: {total_suspicious}")
    print(f"  Local Qwen resolved:    {total_local_resolved}")
    print(f"  Escalated to Tier 2:    {total_escalated}")
    print(f"  Total speaker repairs:  {total_repairs}")
    print(f"  Dry run mode:           {dry_run}")
    print("=" * 70)

    # Update metadata and book script if applied
    if not dry_run:
        from brain.director.script_generator import ScriptGenerator
        ScriptGenerator.sync_dialogue_counts(chapter_scripts, registry)
        atomic_write_text(chars_path, registry.model_dump_json(indent=2))
        logger.info("Updated characters.json with synced dialogue counts.")

        book_script_path = project_path / "book_script.json"
        if book_script_path.exists():
            try:
                bs_data = json.loads(book_script_path.read_text(encoding="utf-8"))
                script_by_num = {c.chapter_number: c.model_dump(mode="json") for c in chapter_scripts}
                if "chapters" in bs_data:
                    for idx, c_data in enumerate(bs_data["chapters"]):
                        c_num = c_data.get("chapter_number")
                        if c_num in script_by_num:
                            bs_data["chapters"][idx] = script_by_num[c_num]
                atomic_write_json(book_script_path, bs_data)
                logger.info("Updated book_script.json")
            except Exception as exc:
                logger.warning("Could not update book_script.json: %s", exc)

        # Write final report
        report_path = project_path / "external_validation" / "tiered_attribution_report.json"
        atomic_write_json(report_path, {
            "summary": {
                "total_suspicious": total_suspicious,
                "local_resolved": total_local_resolved,
                "escalated_to_tier2": total_escalated,
                "total_repairs": total_repairs,
                "dry_run": False,
            },
            "results": [r.to_dict() for r in all_results],
        })
        print(f"\nFinal attribution report written to {report_path}")



if __name__ == "__main__":
    main()
