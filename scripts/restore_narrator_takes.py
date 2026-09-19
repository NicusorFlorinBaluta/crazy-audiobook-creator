#!/usr/bin/env python
r"""Restore the original male narrator takes that were mistakenly replaced with female takes.

This script reads each narrator take from `segments/repair-backup/`, validates it,
and restores it into all 4 stores via `replace_segment()`.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain.orchestrator.voice_client import VoiceClient
from shared.models import ValidateRequest
from shared.pronunciation import apply_pronunciations, load_pronunciation_dictionary
from shared.segment_repair import parse_chapter_number, replace_segment
from voice.validator.audio_analyzer import AudioAnalyzer

logger = logging.getLogger("RestoreNarrator")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

CACHE_DB = ROOT / "voice_cache.db"
STATE_DB = ROOT / "brain" / "projects" / "pipeline_state.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id", default="the-finest-edge-of-twilight-book", nargs="?")
    parser.add_argument("--apply", action="store_true", help="Actually replace segments in 4 stores")
    parser.add_argument("--limit", type=int, default=0, help="Optional limit on number of lines to restore")
    args = parser.parse_args()

    project_id = args.project_id
    project_dir = ROOT / "brain" / "projects" / project_id
    segments_dir = ROOT / "workspace" / project_id / "segments"
    backup_dir = segments_dir / "repair-backup"

    if not backup_dir.is_dir():
        print(f"No repair-backup directory found at {backup_dir}", file=sys.stderr)
        return 1

    script = json.loads((project_dir / "book_script.json").read_text(encoding="utf-8"))
    script_lines = {line["line_id"]: line for ch in script.get("chapters", []) for line in ch.get("lines", [])}
    mappings, _ = load_pronunciation_dictionary(project_dir)

    narrator_backups = sorted(
        p for p in backup_dir.glob("*.wav") if script_lines.get(p.stem, {}).get("speaker") == "narrator"
    )

    print(f"Found {len(narrator_backups)} narrator backup takes in {backup_dir}")

    client = VoiceClient()
    aa = AudioAnalyzer()
    restored = 0
    touched_chapters: set[int] = set()

    for idx, backup_path in enumerate(narrator_backups, 1):
        if args.limit and restored >= args.limit:
            break

        line_id = backup_path.stem
        line = script_lines[line_id]
        spoken = apply_pronunciations(line["text"], mappings)
        chapter = parse_chapter_number(line_id)

        # Inspect backup pitch
        backup_analysis = aa.analyze(str(backup_path))
        backup_pitch = backup_analysis.get("pitch_median", 0.0)

        # Inspect current segment pitch
        current_path = segments_dir / f"{line_id}.wav"
        curr_analysis = aa.analyze(str(current_path)) if current_path.is_file() else {}
        curr_pitch = curr_analysis.get("pitch_median", 0.0)

        if backup_pitch > 130.0:
            print(
                f"  [{idx}/{len(narrator_backups)}] {line_id} (ch {chapter}): SKIPPED - backup pitch is high ({backup_pitch:.1f} Hz)"
            )
            continue

        if not args.apply:
            print(
                f"  [{idx}/{len(narrator_backups)}] {line_id} (ch {chapter}): would restore male take ({backup_pitch:.1f} Hz) over current take ({curr_pitch:.1f} Hz)"
            )
            restored += 1
            touched_chapters.add(chapter)
            continue

        # Create safe temp candidate to pass to replace_segment
        temp_candidate = segments_dir / f"temp-restore-{line_id}.wav"
        import shutil

        shutil.copy2(backup_path, temp_candidate)

        # Validate with Whisper & Audio QA
        validated = client.validate_segment(
            ValidateRequest(
                audio_file=str(temp_candidate),
                expected_text=spoken,
            )
        )

        rep_result = replace_segment(
            project_id=project_id,
            line_id=line_id,
            candidate_path=temp_candidate,
            validated_result=validated,
            project_dir=project_dir,
            segments_dir=segments_dir,
            cache_db=CACHE_DB,
            state_db=STATE_DB,
            chapter=chapter,
            repaired_by="scripts/restore_narrator_takes.py",
        )

        if rep_result.success:
            restored += 1
            touched_chapters.add(chapter)
            print(
                f"  [{idx}/{len(narrator_backups)}] {line_id} (ch {chapter}): RESTORED male take ({backup_pitch:.1f} Hz, wer={validated.wer:.2f})"
            )
        else:
            temp_candidate.unlink(missing_ok=True)
            print(
                f"  [{idx}/{len(narrator_backups)}] {line_id} (ch {chapter}): FAILED ({rep_result.error})",
                file=sys.stderr,
            )

    print(f"\nCompleted: {restored} narrator take(s) restored across {len(touched_chapters)} chapter(s).")
    if touched_chapters:
        ch_list = ",".join(str(c) for c in sorted(touched_chapters))
        print(f"Touched chapters: {ch_list}")
        if args.apply:
            print("\nNext steps:")
            print(f"  1. python scripts/remaster_chapters.py {project_id}")
            print(f"  2. python scripts/reexport_deliveries.py {project_id} --stale")
    return 0


if __name__ == "__main__":
    sys.exit(main())
