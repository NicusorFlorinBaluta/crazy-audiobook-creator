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
import logging
import sys
from pathlib import Path
from typing import Any

# Ensure project root in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logger = logging.getLogger(__name__)

from brain.director.attribution_audit import (
    _refutations,
    action_beat_attributions,
    tag_speaker_evidence,
)
from brain.director.attribution_detector import (
    _is_dialogue_line,
    build_turn_window,
)
from brain.director.script_generator import (
    ScriptGenerator,
)
from brain.orchestrator.job_queue import JobQueue
from brain.orchestrator.voice_client import VoiceClient
from brain.validators.tiered_adjudicator import (
    _attached_tag_evidence,
    _reads_as_attached_tag,
)
from shared import paths as shared_paths
from shared.artifacts import atomic_write_json
from shared.constants import Gender
from shared.models import (
    CharacterRegistry,
    GenerateLineRequest,
    ScriptChapter,
    ScriptLine,
    ValidateRequest,
)
from shared.pronunciation import load_pronunciation_dictionary
from shared.pronunciation_evidence import terms_in_text
from shared.segment_repair import replace_segment
from shared.voice_casting import get_speaker_voice_mapping


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
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Failed to read book.json: %s", exc)

    char_data = None
    char_file = project_dir / "characters.json"
    if char_file.is_file():
        try:
            char_data = json.loads(char_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Failed to read characters.json: %s", exc)

    return project_dir, workspace_dir, job_queue, book_data, char_data


def _diagnose_single_line(
    line: ScriptLine,
    idx: int | None,
    chapter: ScriptChapter | None,
    registry: CharacterRegistry,
    beat_attributions: dict[str, str],
    refutations_by_idx: dict[int, list[tuple[str, str, Gender | None]]],
) -> dict[str, Any]:
    text = (line.text or "").strip()
    current_speaker = line.speaker or "narrator"

    # Check if this line is an attached tag to the previous dialogue line (e.g. ch18_0132 "she accused.")
    if current_speaker == "narrator" and _reads_as_attached_tag(text) and chapter and idx is not None and idx > 0:
        prev_line = chapter.lines[idx - 1]
        if _is_dialogue_line(prev_line):
            named, _kind, gender = ScriptGenerator._dialogue_tag_evidence(text, registry)
            if not named:
                named, gender = tag_speaker_evidence(text, registry)
            suggested = named
            if not suggested and gender:
                scene_speakers = {
                    l.speaker
                    for l in chapter.lines[max(0, idx - 10) : min(len(chapter.lines), idx + 11)]
                    if l.speaker and l.speaker != "narrator"
                }
                cands = [
                    s for s in scene_speakers if registry.characters.get(s) and registry.characters[s].gender == gender
                ]
                if len(cands) == 1:
                    suggested = cands[0]
                elif prev_line.speaker in cands:
                    suggested = prev_line.speaker
            if suggested:
                return {
                    "line_id": line.line_id,
                    "current_speaker": current_speaker,
                    "line_text": text,
                    "is_dialogue": False,
                    "verdict": (
                        "LIKELY_CORRECT_ATTRIBUTION" if suggested == prev_line.speaker else "LIKELY_ATTRIBUTION_ERROR"
                    ),
                    "suggested_speaker": suggested,
                    "confidence": 0.85,
                    "findings": [
                        f"Attached tag '{text}' attributes preceding dialogue line {prev_line.line_id} to '{suggested}'."
                    ],
                    "culprit_score": 0.0 if suggested == prev_line.speaker else 0.85,
                }

    # 1. Check if non-dialogue narration
    is_dialogue = _is_dialogue_line(line)
    if line.dialogue_kind in ("narration", "non_spoken_quote"):
        is_dialogue = False
    if (
        line.speaker == "narrator"
        and not ScriptGenerator._is_dialogue_fragment(text)
        and not any(q in text for q in ('"', "“", "”", "'", "’", "—", "–"))
    ):
        is_dialogue = False

    if not is_dialogue:
        if current_speaker == "narrator":
            return {
                "line_id": line.line_id,
                "current_speaker": current_speaker,
                "line_text": text,
                "is_dialogue": False,
                "verdict": "INCONCLUSIVE",
                "suggested_speaker": None,
                "confidence": 0.5,
                "findings": ["Line is narration; no speaker suggestion applicable."],
                "culprit_score": 0.0,
            }
        return {
            "line_id": line.line_id,
            "current_speaker": current_speaker,
            "line_text": text,
            "is_dialogue": False,
            "verdict": "LIKELY_ATTRIBUTION_ERROR",
            "suggested_speaker": "narrator",
            "confidence": 0.95,
            "findings": [f"Line is narration but assigned to '{current_speaker}'; should be 'narrator'."],
            "culprit_score": 0.95,
        }

    # 2. Dialogue line: Attached speech tag evidence
    attached_named = None
    attached_gender = None
    attached_tag_text = ""
    if chapter and idx is not None and idx + 1 < len(chapter.lines):
        following = chapter.lines[idx + 1]
        if following.speaker == "narrator":
            f_text = str(following.text or "").strip()
            if _reads_as_attached_tag(f_text):
                turn = build_turn_window(chapter, idx, reason="flag_investigation", pattern="flag")
                attached_named, attached_gender, attached_tag_text = _attached_tag_evidence(turn, registry)
                if not attached_named:
                    named_ev, gender_ev = tag_speaker_evidence(f_text, registry)
                    attached_named = attached_named or named_ev
                    attached_gender = attached_gender or gender_ev
                    attached_tag_text = f_text

    if attached_named:
        if attached_named == current_speaker:
            return {
                "line_id": line.line_id,
                "current_speaker": current_speaker,
                "line_text": text,
                "is_dialogue": True,
                "verdict": "LIKELY_CORRECT_ATTRIBUTION",
                "suggested_speaker": attached_named,
                "confidence": 0.98,
                "findings": [
                    f"Attached speech tag '{attached_tag_text}' explicitly confirms speaker '{attached_named}'."
                ],
                "culprit_score": 0.0,
            }
        return {
            "line_id": line.line_id,
            "current_speaker": current_speaker,
            "line_text": text,
            "is_dialogue": True,
            "verdict": "LIKELY_ATTRIBUTION_ERROR",
            "suggested_speaker": attached_named,
            "confidence": 0.98,
            "findings": [
                f"Attached speech tag '{attached_tag_text}' explicitly attributes line to '{attached_named}', contradicting '{current_speaker}'."
            ],
            "culprit_score": 1.0,
        }

    # 3. Action beat attribution
    if line.line_id in beat_attributions:
        beat_spk = beat_attributions[line.line_id]
        if beat_spk == current_speaker:
            return {
                "line_id": line.line_id,
                "current_speaker": current_speaker,
                "line_text": text,
                "is_dialogue": True,
                "verdict": "LIKELY_CORRECT_ATTRIBUTION",
                "suggested_speaker": beat_spk,
                "confidence": 0.92,
                "findings": [f"Action beat sharing paragraph confirms speaker '{beat_spk}'."],
                "culprit_score": 0.0,
            }
        return {
            "line_id": line.line_id,
            "current_speaker": current_speaker,
            "line_text": text,
            "is_dialogue": True,
            "verdict": "LIKELY_ATTRIBUTION_ERROR",
            "suggested_speaker": beat_spk,
            "confidence": 0.92,
            "findings": [
                f"Action beat sharing paragraph attributes line to '{beat_spk}', contradicting '{current_speaker}'."
            ],
            "culprit_score": 0.92,
        }

    # 4. Refutations / Addressee / Possessives
    if idx is not None and idx in refutations_by_idx:
        ref_findings = []
        for src, refuted_spk, req_gender in refutations_by_idx[idx]:
            if src == "addressed_not_speaking":
                ref_findings.append(f"Speaker '{refuted_spk}' is addressed in speech tag, not speaking.")
            elif src == "possessive_contradiction":
                ref_findings.append(f"Possessive contradiction detected for speaker '{refuted_spk}'.")
            elif src == "gendering_tag":
                ref_findings.append(
                    f"Speaker '{refuted_spk}' contradicts tag gender {getattr(req_gender, 'value', req_gender)}."
                )
        return {
            "line_id": line.line_id,
            "current_speaker": current_speaker,
            "line_text": text,
            "is_dialogue": True,
            "verdict": "LIKELY_ATTRIBUTION_ERROR",
            "suggested_speaker": None,
            "confidence": 0.88,
            "findings": ref_findings,
            "culprit_score": 0.88,
        }

    # 5. Tag gender check
    if attached_gender is not None:
        spk_char = registry.characters.get(current_speaker)
        if spk_char and spk_char.gender in (Gender.MALE, Gender.FEMALE):
            if spk_char.gender != attached_gender:
                suggested = None
                if chapter and idx is not None:
                    nearby = [
                        chapter.lines[i].speaker
                        for i in range(max(0, idx - 3), min(len(chapter.lines), idx + 4))
                        if i != idx and chapter.lines[i].speaker not in (current_speaker, "narrator")
                    ]
                    for c_name in nearby:
                        cand = registry.characters.get(c_name)
                        if cand and cand.gender == attached_gender:
                            suggested = cand.id
                            break
                return {
                    "line_id": line.line_id,
                    "current_speaker": current_speaker,
                    "line_text": text,
                    "is_dialogue": True,
                    "verdict": "VOICE_ASSIGNMENT_MISMATCH",
                    "suggested_speaker": suggested,
                    "confidence": 0.85,
                    "findings": [
                        f"Attached tag '{attached_tag_text}' is {attached_gender.value}, contradicting character gender {spk_char.gender.value}."
                    ],
                    "culprit_score": 0.85,
                }
            return {
                "line_id": line.line_id,
                "current_speaker": current_speaker,
                "line_text": text,
                "is_dialogue": True,
                "verdict": "LIKELY_CORRECT_ATTRIBUTION",
                "suggested_speaker": current_speaker,
                "confidence": 0.85,
                "findings": [
                    f"Attached tag '{attached_tag_text}' gender ({attached_gender.value}) confirms '{current_speaker}'."
                ],
                "culprit_score": 0.0,
            }

    # 6. High confidence in script with no contradictions
    conf = line.get("speaker_confidence") if isinstance(line, dict) else getattr(line, "speaker_confidence", None)
    if conf is not None and conf >= 0.90 and current_speaker != "narrator":
        return {
            "line_id": line.line_id,
            "current_speaker": current_speaker,
            "line_text": text,
            "is_dialogue": True,
            "verdict": "LIKELY_CORRECT_ATTRIBUTION",
            "suggested_speaker": current_speaker,
            "confidence": conf,
            "findings": [
                f"Script attribution '{current_speaker}' has confidence {conf:.2f} and no contradicting tag, beat, or refutation."
            ],
            "culprit_score": 0.0,
        }

    return {
        "line_id": line.line_id,
        "current_speaker": current_speaker,
        "line_text": text,
        "is_dialogue": True,
        "verdict": "INCONCLUSIVE",
        "suggested_speaker": None,
        "confidence": 0.50,
        "findings": [
            "No deterministic speech tag, action beat, or refutation found for this line; inconclusive without full adjudication."
        ],
        "culprit_score": 0.2,
    }


def diagnose_flag(
    flag: dict[str, Any],
    project_dir: Path,
    book_data: dict[str, Any] | None,
    char_data: dict[str, Any] | None,
) -> dict[str, Any]:
    """Analyze a single flag against real pipeline attribution evidence across the reaction window."""
    chapter_num = flag["chapter_number"]
    enriched = flag.get("enriched_data") or {}
    active_line = enriched.get("active_line") or {}
    active_line_id = flag.get("line_id") or enriched.get("matched_line_id") or active_line.get("line_id")

    if char_data:
        registry = CharacterRegistry.model_validate(char_data)
    elif (project_dir / "characters.json").is_file():
        try:
            registry = CharacterRegistry.model_validate_json(
                (project_dir / "characters.json").read_text(encoding="utf-8")
            )
        except Exception:
            registry = CharacterRegistry()
    else:
        registry = CharacterRegistry()

    chapter: ScriptChapter | None = None
    script_path = project_dir / "script" / f"chapter_{chapter_num:03d}.json"
    if script_path.is_file():
        try:
            sdata = json.loads(script_path.read_text(encoding="utf-8"))
            sdata.setdefault("chapter_title", f"Chapter {chapter_num}")
            chapter = ScriptChapter.model_validate(sdata)
        except Exception as exc:
            logger.debug("Failed to read %s: %s", script_path, exc)

    chapter_text = ""
    if book_data and "chapters" in book_data:
        for ch_entry in book_data.get("chapters", []):
            if ch_entry.get("number") == chapter_num or ch_entry.get("chapter_number") == chapter_num:
                chapter_text = ch_entry.get("text") or ch_entry.get("content") or ""
                break
    if not chapter_text and (project_dir / "book.json").is_file():
        try:
            b_json = json.loads((project_dir / "book.json").read_text(encoding="utf-8"))
            for ch_entry in b_json.get("chapters", []):
                if ch_entry.get("number") == chapter_num or ch_entry.get("chapter_number") == chapter_num:
                    chapter_text = ch_entry.get("text") or ch_entry.get("content") or ""
                    break
        except Exception as exc:
            logger.debug("Failed to read book.json: %s", exc)

    beat_attributions: dict[str, str] = {}
    if chapter and chapter_text and registry.characters:
        try:
            beat_attributions = action_beat_attributions(chapter, chapter_text, registry)
        except Exception as exc:
            logger.debug("action_beat_attributions: %s", exc)

    refutations_by_idx: dict[int, list[tuple[str, str, Gender | None]]] = {}
    if chapter and registry.characters:
        try:
            for idx, src, refuted_spk, req_gender in _refutations(chapter, registry):
                refutations_by_idx.setdefault(idx, []).append((src, refuted_spk, req_gender))
        except Exception as exc:
            logger.debug("_refutations: %s", exc)

    # Determine candidate lines across the reaction window
    active_idx = None
    if chapter and active_line_id:
        active_idx = next((i for i, l in enumerate(chapter.lines) if l.line_id == active_line_id), None)

    cand_dicts = enriched.get("candidate_lines") or []
    candidate_line_ids: list[str] = [c["line_id"] for c in cand_dicts if c.get("line_id")]
    if not candidate_line_ids and active_line_id:
        candidate_line_ids = [active_line_id]

    if chapter and active_idx is not None and len(candidate_line_ids) <= 1:
        start_idx = max(0, active_idx - 4)
        end_idx = min(len(chapter.lines), active_idx + 2)
        candidate_line_ids = [chapter.lines[i].line_id for i in range(start_idx, end_idx)]

    ranked_line_diagnoses: list[dict[str, Any]] = []
    for lid in candidate_line_ids:
        line_obj = None
        line_idx = None
        if chapter:
            line_idx = next((i for i, l in enumerate(chapter.lines) if l.line_id == lid), None)
            if line_idx is not None:
                line_obj = chapter.lines[line_idx]
        if line_obj is None:
            raw = active_line if lid == active_line_id else next((c for c in cand_dicts if c.get("line_id") == lid), {})
            d_kind = raw.get("dialogue_kind")
            if d_kind not in ("spoken", "non_spoken_quote", "reported_collective_speech"):
                d_kind = None
            line_obj = ScriptLine(
                line_id=lid,
                speaker=raw.get("speaker", "narrator"),
                text=raw.get("text", ""),
                dialogue_kind=d_kind,
            )

        diag = _diagnose_single_line(
            line=line_obj,
            idx=line_idx,
            chapter=chapter,
            registry=registry,
            beat_attributions=beat_attributions,
            refutations_by_idx=refutations_by_idx,
        )
        ranked_line_diagnoses.append(diag)

    # Order window diagnoses by error severity (culprit_score desc)
    errors = [
        d for d in ranked_line_diagnoses if d["verdict"] in ("LIKELY_ATTRIBUTION_ERROR", "VOICE_ASSIGNMENT_MISMATCH")
    ]
    errors.sort(key=lambda d: d.get("culprit_score", 0.0), reverse=True)

    if errors:
        primary = errors[0]
    else:
        active_diag = next((d for d in ranked_line_diagnoses if d["line_id"] == active_line_id), None)
        primary = active_diag or (
            ranked_line_diagnoses[0]
            if ranked_line_diagnoses
            else {
                "line_id": active_line_id,
                "current_speaker": active_line.get("speaker", "narrator"),
                "line_text": active_line.get("text", ""),
                "verdict": "INCONCLUSIVE",
                "suggested_speaker": None,
                "confidence": 0.5,
                "findings": ["No lines in window could be diagnosed."],
            }
        )

    user_note = flag.get("user_note", "")
    all_findings = list(primary["findings"])
    if user_note:
        all_findings.append(f'User note: "{user_note}"')

    return {
        "flag_id": flag["flag_id"],
        "chapter_number": chapter_num,
        "position_ms": flag["position_ms"],
        "current_speaker": primary["current_speaker"],
        "active_line_id": primary["line_id"],
        "line_text": primary["line_text"],
        "verdict": primary["verdict"],
        "diagnosis": primary["verdict"],
        "suggested_speaker": primary["suggested_speaker"],
        "confidence": primary["confidence"],
        "findings": all_findings,
        "window_diagnoses": ranked_line_diagnoses,
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

        lines.extend(
            [
                f"## Issue {idx}: Flag `{f['flag_id']}`",
                f"- **Chapter**: {f['chapter_number']}",
                f"- **Playback Timestamp**: {time_str} ({f['position_ms']} ms)",
                f"- **Source**: `{f.get('source', 'phone')}`",
                f"- **Issue Category**: `{f.get('issue_type', 'wrong_speaker')}`",
                f"- **User Note**: {f.get('user_note') or '*(None)*'}",
                f"- **Active Line**: `{active.get('line_id')}` | **Speaker**: `{active.get('speaker')}` ({active.get('voice_id')})",
                f'- **Spoken Text**: > "{active.get("text", "")}"',
                "",
            ]
        )

        candidate_lines = enriched.get("candidate_lines") or []
        if candidate_lines:
            lines.extend(
                [
                    "",
                    "### Recent Lines Within Reaction Delay Window:",
                ]
            )
            for c in candidate_lines:
                rel = f"{c.get('relative_sec', 0.0):+4.1f}s"
                tap_marker = " [AT TAP]" if c.get("is_at_tap") else ""
                marker = "**-->** " if c.get("line_id") == active.get("line_id") else "    "
                lines.append(
                    f'{marker}- ({rel}){tap_marker} [`{c.get("line_id")}`] **{c.get("speaker")}**: "{c.get("text")}"'
                )

        lines.extend(
            [
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
            ]
        )

    return "\n".join(lines)


def record_attribution_repair(
    *,
    project_id: str,
    chapter_number: int,
    line_id: str,
    old_speaker: str,
    new_speaker: str,
    spoken_text: str,
    rule_reason: str | None = None,
    ledger_path: Path | None = None,
    test_file_path: Path | None = None,
) -> dict[str, str]:
    """Emit a ledger entry in docs/attribution-case-ledger.md and a pending regression test."""
    ledger_path = ledger_path or (shared_paths.REPO_ROOT / "docs" / "attribution-case-ledger.md")
    test_file_path = test_file_path or (shared_paths.REPO_ROOT / "tests" / "test_attribution_audit.py")

    snippet = spoken_text.strip().replace("\n", " ").replace("|", "/").strip()
    if len(snippet) > 35:
        snippet = snippet[:32] + "…"

    rule = rule_reason or f"Playback flag repair: speaker corrected to {new_speaker}"
    rule = rule.replace("|", "/")

    try:
        rel_test_path = test_file_path.relative_to(shared_paths.REPO_ROOT).as_posix()
    except ValueError:
        rel_test_path = test_file_path.name
        if not rel_test_path.startswith("tests/"):
            rel_test_path = f"tests/{rel_test_path}"

    ledger_row = (
        f"| {line_id} | {old_speaker} -> {new_speaker} in ch {chapter_number}: '{snippet}' | "
        f"{rule} | 1 flag resolved | `{rel_test_path}` | — |"
    )

    # 1. Update ledger file
    if ledger_path.is_file():
        content = ledger_path.read_text(encoding="utf-8")
        if line_id not in content:
            target_heading = "## Rules measured and rejected"
            if target_heading in content:
                parts = content.split(target_heading, 1)
                table_part = parts[0].rstrip()
                new_content = f"{table_part}\n{ledger_row}\n\n{target_heading}{parts[1]}"
            else:
                new_content = content.rstrip() + f"\n{ledger_row}\n"
            ledger_path.write_text(new_content, encoding="utf-8")
    else:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(
            f"# Attribution case ledger\n\n## Rules in force\n\n"
            f"| case | what was wrong | rule | measured | test | provenance |\n"
            f"| --- | --- | --- | --- | --- | --- |\n"
            f"{ledger_row}\n\n## Rules measured and rejected\n",
            encoding="utf-8",
        )

    # 2. Generate regression test
    test_fn_name = f"test_attribution_regression_{line_id}"
    test_code = (
        f"    def {test_fn_name}(self) -> None:\n"
        f'        """Pinned regression test for {line_id} (chapter {chapter_number:03d}).\n'
        f"\n"
        f"        Wrong speaker: {old_speaker} -> Correct: {new_speaker}.\n"
        f"        Source text: {snippet!r}\n"
        f"        Rule/Reason: {rule}\n"
        f'        """\n'
        f"        import json\n"
        f"        from pathlib import Path\n"
        f"\n"
        f'        script_path = Path("brain/projects/{project_id}/script/chapter_{chapter_number:03d}.json")\n'
        f"        if not script_path.is_file():\n"
        f'            self.skipTest(f"Script not found: {{script_path}}")\n'
        f'        data = json.loads(script_path.read_text(encoding="utf-8"))\n'
        f'        line = next((l for l in data.get("lines", []) if l.get("line_id") == "{line_id}"), None)\n'
        f'        self.assertIsNotNone(line, f"Line {line_id} not found in {{script_path}}")\n'
        f'        self.assertEqual(line.get("speaker"), "{new_speaker}")\n'
    )

    if test_file_path.is_file():
        t_content = test_file_path.read_text(encoding="utf-8")
        if test_fn_name not in t_content:
            marker = 'if __name__ == "__main__":'
            if marker in t_content:
                parts = t_content.split(marker, 1)
                new_t_content = f"{parts[0].rstrip()}\n\n{test_code}\n\n{marker}{parts[1]}"
            else:
                new_t_content = f"{t_content.rstrip()}\n\n{test_code}\n"
            test_file_path.write_text(new_t_content, encoding="utf-8")
    else:
        test_file_path.parent.mkdir(parents=True, exist_ok=True)
        test_file_path.write_text(
            f"import unittest\n\n\nclass AttributionAuditTests(unittest.TestCase):\n{test_code}\n\n\n"
            f'if __name__ == "__main__":\n    unittest.main()\n',
            encoding="utf-8",
        )

    return {
        "ledger_path": str(ledger_path),
        "test_file_path": str(test_file_path),
        "ledger_row": ledger_row,
        "test_name": test_fn_name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Investigate flagged playback issues")
    parser.add_argument("project_id", help="Audiobook project ID")
    parser.add_argument("--flag-id", help="Target specific flag ID")
    parser.add_argument(
        "--status",
        choices=["open", "pending", "investigating", "investigated", "vetoed", "resolved", "fixed", "all"],
        default="open",
    )
    parser.add_argument("--auto-diagnose", action="store_true", help="Run heuristic diagnosis on flags")
    parser.add_argument("--export-prompt", action="store_true", help="Export Markdown prompt for an AI agent")
    parser.add_argument("--veto", help="Flag ID to veto")
    parser.add_argument("--repair", help="Flag ID to repair")
    parser.add_argument("--speaker", help="Correct speaker name for --repair")
    parser.add_argument("--reason", default="", help="Agent explanation or rationale for veto/repair")
    parser.add_argument(
        "--ledger-path",
        default=None,
        help="Path to attribution case ledger markdown file (default: docs/attribution-case-ledger.md)",
    )
    parser.add_argument(
        "--test-path",
        default=None,
        help="Path to attribution audit test file (default: tests/test_attribution_audit.py)",
    )
    parser.add_argument(
        "--apply", action="store_true", default=False, help="Actually commit repairs (default is dry-run)"
    )
    parser.add_argument(
        "--dry-run", dest="apply", action="store_false", help="Dry run without committing changes (default)"
    )
    parser.add_argument("--json", action="store_true", help="Output results as JSON")

    args = parser.parse_args(argv)

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
        atomic_write_json(
            project_dir / "playback_flags.json",
            {"project_id": args.project_id, "total_flags": len(flags), "flags": flags},
        )
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
            line_id = enriched.get("matched_line_id") or (enriched.get("active_line") or {}).get("line_id")
        if not line_id:
            print(f"Error: Flag {args.repair} has no identified line_id.", file=sys.stderr)
            return 1

        # Locate chapter script
        script_path = project_dir / "script" / f"chapter_{ch_num:03d}.json"
        if not script_path.is_file():
            print(f"Error: Chapter script {script_path} not found.", file=sys.stderr)
            return 1

        sdata = json.loads(script_path.read_text(encoding="utf-8"))
        target_line = None
        for line in sdata.get("lines", []):
            if str(line.get("line_id", "")) == line_id:
                target_line = line
                break

        if target_line is None:
            print(f"Error: Line {line_id} not found in {script_path}.", file=sys.stderr)
            return 1

        old_speaker = target_line.get("speaker")
        spoken = target_line.get("spoken_text") or target_line.get("text") or ""
        emotion = target_line.get("emotion") or "normal"

        speaker_to_voice = get_speaker_voice_mapping(project_dir)
        voice_id = speaker_to_voice.get(args.speaker) or target_line.get("voice_id") or args.speaker

        p_dict, _ = load_pronunciation_dictionary(project_dir)
        validation_terms = sorted(terms_in_text(spoken, p_dict.keys()))

        # Generate candidate audio with VoiceClient
        client = VoiceClient()
        gen_req = GenerateLineRequest(
            project_id=args.project_id,
            line=ScriptLine(
                line_id=f"repair-{line_id}",
                speaker=args.speaker,
                voice_id=voice_id,
                text=spoken,
                emotion=emotion,
            ),
        )
        try:
            generated = client.generate_line(gen_req)
        except Exception as exc:
            print(f"Error: Voice generation failed for line {line_id}: {exc}", file=sys.stderr)
            return 1

        candidate = Path(generated.audio_file)

        # Validate candidate audio with VoiceClient
        try:
            val_req = ValidateRequest(
                audio_file=str(candidate),
                expected_text=spoken,
                validation_terms=validation_terms,
            )
            validated = client.validate_segment(val_req)
        except Exception as exc:
            candidate.unlink(missing_ok=True)
            print(f"Error: Audio validation failed for line {line_id}: {exc}", file=sys.stderr)
            return 1

        val_status = str(getattr(validated, "status", "")).lower()
        passed = val_status in ("pass", "passed", "accepted_with_warning") or getattr(
            validated, "passed_hard_gates", False
        )
        if not passed:
            candidate.unlink(missing_ok=True)
            print(
                f"Error: Generated audio failed validation (status: {val_status}). "
                f"Repair refused; flag {args.repair} left open.",
                file=sys.stderr,
            )
            return 1

        if not args.apply:
            candidate.unlink(missing_ok=True)
            print(
                f"[DRY-RUN] Validated repair for line {line_id} in chapter {ch_num}: "
                f"would change '{old_speaker}' -> '{args.speaker}' (voice '{voice_id}'). "
                f"Validation passed ({val_status}). Run with --apply to commit."
            )
            return 0

        # Apply repair across all 4 stores
        segments_dir = workspace_dir / "segments"
        cache_db = shared_paths.REPO_ROOT / "voice_cache.db"
        state_db = shared_paths.PROJECTS_DIR / "pipeline_state.db"

        rep_result = replace_segment(
            project_id=args.project_id,
            line_id=line_id,
            candidate_path=candidate,
            validated_result=validated,
            project_dir=project_dir,
            segments_dir=segments_dir,
            cache_db=cache_db,
            state_db=state_db,
            chapter=ch_num,
            repaired_by="tools/investigate_playback_flags.py",
        )

        if not rep_result.success:
            candidate.unlink(missing_ok=True)
            print(
                f"Error: replace_segment failed: {rep_result.error}. Flag {args.repair} left open.",
                file=sys.stderr,
            )
            return 1

        # Patch chapter script
        target_line["speaker"] = args.speaker
        target_line["voice_id"] = voice_id
        target_line["speaker_confidence"] = 1.0
        target_line["speaker_evidence"] = args.reason or f"Manually repaired by agent: {args.reason}"
        target_line["attribution_review_required"] = False
        atomic_write_json(script_path, sdata)

        res_msg = f"Repaired line {line_id} in chapter {ch_num}: changed '{old_speaker}' -> '{args.speaker}' (voice '{voice_id}')."
        updated = job_queue.update_playback_flag(
            project_id=args.project_id,
            flag_id=args.repair,
            status="resolved",
            agent_verdict="CONFIRMED_ERROR",
            agent_explanation=args.reason
            or f"Attribution corrected in script and audio regenerated for chapter {ch_num}.",
            resolution=res_msg,
            resolved_by="agent:ai",
        )
        flags = job_queue.get_playback_flags(args.project_id)
        atomic_write_json(
            project_dir / "playback_flags.json",
            {"project_id": args.project_id, "total_flags": len(flags), "flags": flags},
        )
        print(res_msg)

        ledger_info = record_attribution_repair(
            project_id=args.project_id,
            chapter_number=ch_num,
            line_id=line_id,
            old_speaker=old_speaker,
            new_speaker=args.speaker,
            spoken_text=spoken,
            rule_reason=args.reason,
            ledger_path=Path(args.ledger_path) if args.ledger_path else None,
            test_file_path=Path(args.test_path) if args.test_path else None,
        )
        print(f"\n[ATTRIBUTION REGRESSION] Recorded in {ledger_info['ledger_path']}:")
        print(f"  {ledger_info['ledger_row']}")
        print(f"Generated pending regression test in {ledger_info['test_file_path']}:")
        print(f"  {ledger_info['test_name']}")
        print("\nACTION REQUIRED (Ground Rule 5):")
        print("  1. Review the generated test in tests/test_attribution_audit.py.")
        print("  2. Verify the rule wording in docs/attribution-case-ledger.md.")
        print("  3. If this failure mode represents a generalisable attribution rule,")
        print("     formulate the refutation/tag rule and replace the script lookup with a synthetic test case.")

        print("\nFollow-up commands required to update deliverables:")
        print(f"  python scripts/remaster_chapters.py {args.project_id} {ch_num}")
        print(f"  python scripts/reexport_deliveries.py {args.project_id}")
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
                print(f'  Text: "{d["line_text"]}"')
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
            print(f'    Text: "{active.get("text", "")[:60]}..."')
            if f.get("user_note"):
                print(f'    Note: "{f["user_note"]}"')
            if f.get("agent_verdict"):
                print(f"    Verdict: {f['agent_verdict']} - {f.get('resolution')}")
            print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
