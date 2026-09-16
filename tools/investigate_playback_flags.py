"""Tool for inspecting, auto-diagnosing, vetoing, or repairing flagged playback issues.

Usage:
    python tools/investigate_playback_flags.py <project_id>
    python tools/investigate_playback_flags.py <project_id> --auto-diagnose
    python tools/investigate_playback_flags.py <project_id> --export-prompt
    python tools/investigate_playback_flags.py <project_id> --veto <flag_id> --reason "Narrator is correct here"
    python tools/investigate_playback_flags.py <project_id> --repair <flag_id> --speaker "elend" --reason "Tag says said Elend"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Ensure project root in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brain.orchestrator.job_queue import JobQueue
from brain.director.attribution_audit import audit_book_attribution
from shared import paths as shared_paths
from shared.artifacts import atomic_write_json, atomic_write_text
from shared.models import CharacterRegistry, ExtractedBook, ScriptChapter


def _load_project_data(project_id: str) -> tuple[Path, Path, JobQueue, dict[str, Any] | None, dict[str, Any] | None]:
    project_dir = shared_paths.PROJECTS_DIR / project_id
    workspace_dir = shared_paths.WORKSPACE_DIR / project_id
    if not project_dir.is_dir():
        raise SystemExit(f"Project directory not found: {project_dir}")

    job_queue = JobQueue()

    book_data = None
    book_file = project_dir / "book.json"
    if book_file.is_file():
        try:
            book_data = json.loads(book_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    char_data = None
    char_file = project_dir / "characters.json"
    if char_file.is_file():
        try:
            char_data = json.loads(char_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    return project_dir, workspace_dir, job_queue, book_data, char_data


def diagnose_flag(
    flag: dict[str, Any],
    project_dir: Path,
    book_data: dict[str, Any] | None,
    char_data: dict[str, Any] | None,
) -> dict[str, Any]:
    """Analyze a single flag against script and manuscript heuristics."""
    chapter_num = flag["chapter_number"]
    enriched = flag.get("enriched_data") or {}
    active_line = enriched.get("active_line") or {}
    manuscript_excerpt = enriched.get("manuscript_excerpt") or ""

    findings: list[str] = []
    suspected_speaker: str | None = None
    confidence: float = 0.5
    verdict: str = "INCONCLUSIVE"

    line_text = active_line.get("text", "")
    current_speaker = active_line.get("speaker", "narrator")

    # 1. Search for direct speech tags in manuscript excerpt
    # e.g., '“... ,” said Kelsier' or '“...” Wayne asked'
    if char_data and manuscript_excerpt:
        characters = char_data.get("characters", {})
        for char_id, char_info in characters.items():
            if char_id in ("narrator", "unnamed_male", "unnamed_female"):
                continue
            name = char_info.get("name") or char_id
            aliases = char_info.get("aliases") or []
            names_to_check = [name, char_id.replace("_", " "), *aliases]
            for n in names_to_check:
                if len(n) < 3:
                    continue
                escaped = re.escape(n)
                # Patterns: said [Name], [Name] said, asked [Name], replied [Name]
                tag_patterns = [
                    rf"(?:said|asked|whispered|shouted|replied|muttered|grunted|called)\s+{escaped}\b",
                    rf"\b{escaped}\s+(?:said|asked|whispered|shouted|replied|muttered|grunted|called)\b",
                ]
                for pat in tag_patterns:
                    if re.search(pat, manuscript_excerpt, re.IGNORECASE):
                        findings.append(f"Found speech tag pattern '{pat}' matching character '{char_id}' in manuscript excerpt.")
                        if char_id != current_speaker:
                            suspected_speaker = char_id
                            confidence = 0.85
                            verdict = "LIKELY_ATTRIBUTION_ERROR"
                        else:
                            confidence = 0.90
                            verdict = "LIKELY_CORRECT_ATTRIBUTION"
                        break
                if suspected_speaker:
                    break

    # 2. Gender mismatch heuristic
    if char_data and current_speaker:
        char_info = char_data.get("characters", {}).get(current_speaker)
        if char_info:
            gender = char_info.get("gender")
            voice_id = active_line.get("voice_id", "")
            if gender == "female" and any(k in voice_id for k in ("male", "_m_", "guy", "man")):
                findings.append(f"Voice mismatch detected: character '{current_speaker}' is female, but assigned voice is '{voice_id}'.")
                verdict = "VOICE_ASSIGNMENT_MISMATCH"
            elif gender == "male" and any(k in voice_id for k in ("female", "_f_", "girl", "woman")):
                findings.append(f"Voice mismatch detected: character '{current_speaker}' is male, but assigned voice is '{voice_id}'.")
                verdict = "VOICE_ASSIGNMENT_MISMATCH"

    # 3. Check user note if provided
    user_note = flag.get("user_note", "")
    if user_note:
        findings.append(f"User note: \"{user_note}\"")

    return {
        "flag_id": flag["flag_id"],
        "chapter_number": chapter_num,
        "position_ms": flag["position_ms"],
        "current_speaker": current_speaker,
        "active_line_id": active_line.get("line_id"),
        "line_text": line_text,
        "verdict": verdict,
        "suggested_speaker": suspected_speaker,
        "confidence": confidence,
        "findings": findings,
    }


def export_agent_prompt(flags: list[dict[str, Any]], project_id: str) -> str:
    """Generate a structured Markdown prompt for an AI agent to investigate."""
    lines = [
        f"# Playback Issue Investigation Request for Project: `{project_id}`",
        "",
        "The following audio playback issues were flagged by the user during playback (via mobile app / Android Auto).",
        "Please investigate each flagged section, inspect the manuscript context and character speech tags,",
        "and determine whether there is an attribution error, voice mismatch, or if the original audio was correct.",
        "",
    ]

    for idx, f in enumerate(flags, start=1):
        enriched = f.get("enriched_data") or {}
        active = enriched.get("active_line") or {}
        surrounding = enriched.get("surrounding_lines") or []
        excerpt = enriched.get("manuscript_excerpt") or "*(None extracted)*"

        pos_sec = f["position_ms"] / 1000.0
        time_str = f"{int(pos_sec // 60):02d}:{pos_sec % 60:04.1f}"

        lines.extend([
            f"## Issue {idx}: Flag `{f['flag_id']}`",
            f"- **Chapter**: {f['chapter_number']}",
            f"- **Playback Timestamp**: {time_str} ({f['position_ms']} ms)",
            f"- **Source**: `{f.get('source', 'phone')}`",
            f"- **Issue Category**: `{f.get('issue_type', 'wrong_speaker')}`",
            f"- **User Note**: {f.get('user_note') or '*(None)*'}",
            f"- **Active Line**: `{active.get('line_id')}` | **Speaker**: `{active.get('speaker')}` ({active.get('voice_id')})",
            f"- **Spoken Text**: > \"{active.get('text', '')}\"",
            "",
        ])

        candidate_lines = enriched.get("candidate_lines") or []
        if candidate_lines:
            lines.extend([
                "",
                "### Recent Lines Within Reaction Delay Window:",
            ])
            for c in candidate_lines:
                rel = f"{c.get('relative_sec', 0.0):+4.1f}s"
                tap_marker = " [AT TAP]" if c.get("is_at_tap") else ""
                marker = "**-->** " if c.get("line_id") == active.get("line_id") else "    "
                lines.append(f"{marker}- ({rel}){tap_marker} [`{c.get('line_id')}`] **{c.get('speaker')}**: \"{c.get('text')}\"")

        lines.extend([
            "",
            "### Manuscript Excerpt:",
            f"```text\n{excerpt}\n```",
            "",
            "**Questions for Agent**:",
            f"1. Is line `{active.get('line_id')}` correctly attributed to `{active.get('speaker')}`?",
            "2. If misattributed, who is the correct speaker and what evidence from the manuscript supports it?",
            "3. If correctly attributed, what is the rationale to record as an agent veto?",
            "---",
            "",
        ])

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Investigate flagged playback issues")
    parser.add_argument("project_id", help="Audiobook project ID")
    parser.add_argument("--flag-id", help="Target specific flag ID")
    parser.add_argument("--status", choices=["open", "pending", "investigating", "investigated", "vetoed", "resolved", "fixed", "all"], default="open")
    parser.add_argument("--auto-diagnose", action="store_true", help="Run heuristic diagnosis on flags")
    parser.add_argument("--export-prompt", action="store_true", help="Export Markdown prompt for an AI agent")
    parser.add_argument("--veto", help="Flag ID to veto")
    parser.add_argument("--repair", help="Flag ID to repair")
    parser.add_argument("--speaker", help="Correct speaker name for --repair")
    parser.add_argument("--reason", default="", help="Agent explanation or rationale for veto/repair")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")

    args = parser.parse_args()

    project_dir, workspace_dir, job_queue, book_data, char_data = _load_project_data(args.project_id)

    # Handle single Veto action
    if args.veto:
        reason = args.reason or "Attribution verified as correct per manuscript context."
        updated = job_queue.update_playback_flag(
            project_id=args.project_id,
            flag_id=args.veto,
            status="vetoed",
            agent_verdict="AGENT_VETO",
            agent_explanation=reason,
            resolution="Vetoed: original attribution confirmed correct.",
            resolved_by="agent:ai",
        )
        if not updated:
            print(f"Error: Flag {args.veto} not found.", file=sys.stderr)
            return 1
        flags = job_queue.get_playback_flags(args.project_id)
        atomic_write_json(project_dir / "playback_flags.json", {"project_id": args.project_id, "total_flags": len(flags), "flags": flags})
        print(f"Successfully vetoed flag {args.veto}: {reason}")
        return 0

    # Handle single Repair action
    if args.repair:
        if not args.speaker:
            print("Error: --speaker is required with --repair", file=sys.stderr)
            return 1
        flag = job_queue.get_playback_flag(args.project_id, args.repair)
        if not flag:
            print(f"Error: Flag {args.repair} not found.", file=sys.stderr)
            return 1

        ch_num = flag["chapter_number"]
        line_id = flag.get("line_id")
        if not line_id:
            enriched = flag.get("enriched_data") or {}
            line_id = enriched.get("matched_line_id")
        if not line_id:
            print(f"Error: Flag {args.repair} has no identified line_id.", file=sys.stderr)
            return 1

        # Patch script file
        script_path = project_dir / "script" / f"chapter_{ch_num:03d}.json"
        if not script_path.is_file():
            print(f"Error: Chapter script {script_path} not found.", file=sys.stderr)
            return 1

        sdata = json.loads(script_path.read_text(encoding="utf-8"))
        repaired = False
        old_speaker = None
        for line in sdata.get("lines", []):
            if str(line.get("line_id", "")) == line_id:
                old_speaker = line.get("speaker")
                line["speaker"] = args.speaker
                line["speaker_confidence"] = 1.0
                line["speaker_evidence"] = args.reason or f"Manually repaired by agent: {args.reason}"
                line["attribution_review_required"] = False
                repaired = True
                break

        if not repaired:
            print(f"Error: Line {line_id} not found in {script_path}.", file=sys.stderr)
            return 1

        atomic_write_json(script_path, sdata)

        # Invalidate cached WAV for this line if present
        ch_seg_wav = workspace_dir / "audio" / f"ch_{ch_num:03d}_{line_id}.wav"
        if ch_seg_wav.is_file():
            ch_seg_wav.unlink(missing_ok=True)

        res_msg = f"Repaired line {line_id} in ch {ch_num}: changed '{old_speaker}' -> '{args.speaker}'."
        updated = job_queue.update_playback_flag(
            project_id=args.project_id,
            flag_id=args.repair,
            status="resolved",
            agent_verdict="CONFIRMED_ERROR",
            agent_explanation=args.reason or "Attribution corrected in script.",
            resolution=res_msg,
            resolved_by="agent:ai",
        )
        flags = job_queue.get_playback_flags(args.project_id)
        atomic_write_json(project_dir / "playback_flags.json", {"project_id": args.project_id, "total_flags": len(flags), "flags": flags})
        print(res_msg)
        return 0

    # Query flags
    filter_status = None if (args.status == "all" or args.flag_id) else args.status
    flags = job_queue.get_playback_flags(args.project_id, status=filter_status)
    if args.flag_id:
        flags = [f for f in flags if f.get("flag_id") == args.flag_id]

    if not flags:
        print(f"No flags found for project '{args.project_id}' with status '{args.status}'.")
        return 0

    if args.export_prompt:
        prompt = export_agent_prompt(flags, args.project_id)
        print(prompt)
        return 0

    if args.auto_diagnose:
        diagnoses = [diagnose_flag(f, project_dir, book_data, char_data) for f in flags]
        if args.json:
            print(json.dumps(diagnoses, indent=2))
        else:
            print(f"=== Diagnosed {len(diagnoses)} Flags for Project '{args.project_id}' ===")
            for d in diagnoses:
                print(f"\nFlag {d['flag_id']} [Ch {d['chapter_number']}, {d['position_ms']} ms]:")
                print(f"  Line: {d['active_line_id']} (Current: '{d['current_speaker']}')")
                print(f"  Text: \"{d['line_text']}\"")
                print(f"  Verdict: {d['verdict']} (Confidence: {d['confidence']:.2f})")
                if d["suggested_speaker"]:
                    print(f"  Suggested: '{d['suggested_speaker']}'")
                for f_note in d["findings"]:
                    print(f"  - {f_note}")
        return 0

    if args.json:
        print(json.dumps(flags, indent=2))
    else:
        print(f"=== Playback Flags for Project '{args.project_id}' ({len(flags)} total) ===")
        for f in flags:
            pos_sec = f["position_ms"] / 1000.0
            time_str = f"{int(pos_sec // 60):02d}:{pos_sec % 60:04.1f}"
            enriched = f.get("enriched_data") or {}
            active = enriched.get("active_line") or {}
            print(f"- [{f['status'].upper()}] {f['flag_id']} | Ch {f['chapter_number']} at {time_str} ({f['source']})")
            print(f"    Line: {f.get('line_id') or active.get('line_id')} | Speaker: {active.get('speaker')}")
            print(f"    Text: \"{active.get('text', '')[:60]}...\"")
            if f.get("user_note"):
                print(f"    Note: \"{f['user_note']}\"")
            if f.get("agent_verdict"):
                print(f"    Verdict: {f['agent_verdict']} - {f.get('resolution')}")
            print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
