"""Suspicious dialogue attribution detector.

Scans chapter script lines for patterns that indicate dialogue turn collapse,
staccato misattribution, narrator-separated attribution collapse, or low confidence.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from shared.models import ScriptChapter, ScriptLine

CONTINUATION_MARKERS = {
    "continued",
    "went on",
    "added",
    "resumed",
    "repeated",
    "furthered",
    "clarified",
    "elaborated",
}


def _is_dialogue_line(line: ScriptLine) -> bool:
    """Return True if the line represents quoted or spoken dialogue."""
    if line.dialogue_kind == "spoken":
        return True
    if line.dialogue_kind == "non_spoken_quote":
        return False
    val = line.text.strip()
    if not val:
        return False
    # Check leading quotes or dashes
    if val.startswith(('"', "“", "‘", "'", "—", "–")):
        return True
    # If speaker is non-narrator and quotes appear inside
    if line.speaker != "narrator" and any(q in val for q in ('"', "“", "”", "'", "’")):
        return True
    return False


def _has_continuation_tag(text: str) -> bool:
    """Check if narrative text contains explicit continuation evidence."""
    lowered = text.casefold()
    return any(re.search(rf"\b{re.escape(marker)}\b", lowered) for marker in CONTINUATION_MARKERS)


@dataclass
class SuspiciousTurn:
    line_id: str
    chapter_number: int
    text: str
    current_speaker: str
    detection_reason: str
    detection_pattern: str
    surrounding_lines: list[dict[str, Any]]
    scene_text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect_suspicious_turns(
    chapters: list[ScriptChapter],
    *,
    min_confidence: float = 0.70,
    max_short_response_chars: int = 80,
    window_radius: int = 5,
    scene_radius: int = 8,
) -> list[SuspiciousTurn]:
    """Scan chapters for suspicious dialogue lines that warrant re-adjudication.

    Detection Patterns:
    1. consecutive_collapse: Adjacent dialogue lines with the same non-narrator speaker
       where at least one is a question or short response.
    2. narrator_separated_collapse: Dialogue from speaker A, followed by short narration
       without continuation tags, followed by dialogue assigned to speaker A again.
    3. low_confidence: Any dialogue line with speaker_confidence < min_confidence.
    4. untagged_staccato: 3+ alternating dialogue turns in a sequence where speaker evidence
       lacks explicit tags and confidence is below 0.85.
    """
    flagged_map: dict[str, SuspiciousTurn] = {}

    for chapter in chapters:
        lines = chapter.lines
        total = len(lines)
        if not lines:
            continue

        dialogue_indices = [idx for idx, line in enumerate(lines) if _is_dialogue_line(line)]

        # `lines`, `total` and `chapter` are bound as defaults rather than
        # captured. The closure is only ever called within this iteration
        # today, so the capture is currently harmless -- but it is harmless by
        # accident, and would silently attribute one chapter's turns to another
        # the moment this call is deferred, batched or made async.
        def build_suspicious_turn(
            idx: int,
            reason: str,
            pattern: str,
            *,
            lines: list = lines,
            total: int = total,
            chapter=chapter,
        ) -> SuspiciousTurn:
            target = lines[idx]
            w_start = max(0, idx - window_radius)
            w_end = min(total, idx + window_radius + 1)
            surrounding = [
                {
                    "line_id": neighbor.line_id,
                    "text": neighbor.text,
                    "speaker": neighbor.speaker,
                    "speaker_confidence": neighbor.speaker_confidence,
                    "dialogue_kind": neighbor.dialogue_kind,
                    "is_target": (neighbor.line_id == target.line_id),
                }
                for neighbor in lines[w_start:w_end]
            ]
            s_start = max(0, idx - scene_radius)
            s_end = min(total, idx + scene_radius + 1)
            scene = " ".join(neighbor.text.strip() for neighbor in lines[s_start:s_end])

            return SuspiciousTurn(
                line_id=target.line_id,
                chapter_number=chapter.chapter_number,
                text=target.text,
                current_speaker=target.speaker,
                detection_reason=reason,
                detection_pattern=pattern,
                surrounding_lines=surrounding,
                scene_text=scene,
            )

        # -------------------------------------------------------------
        # Pattern 1: Consecutive Same-Speaker Dialogue Collapse
        # -------------------------------------------------------------
        for i in range(len(dialogue_indices) - 1):
            idx_a = dialogue_indices[i]
            idx_b = dialogue_indices[i + 1]

            # True immediate adjacency: no intervening dialogue
            # Only check if both are assigned to the same non-narrator speaker
            line_a = lines[idx_a]
            line_b = lines[idx_b]

            if line_a.speaker == line_b.speaker and line_a.speaker != "narrator":
                # Check intervening narration
                intervening_texts = [lines[k].text for k in range(idx_a + 1, idx_b)]
                combined_intervening = " ".join(intervening_texts)
                has_continuation = _has_continuation_tag(combined_intervening)

                if not has_continuation:
                    text_a = line_a.text.strip()
                    text_b = line_b.text.strip()

                    is_q_or_short = (
                        "?" in text_a
                        or "?" in text_b
                        or len(text_a) <= max_short_response_chars
                        or len(text_b) <= max_short_response_chars
                    )

                    if is_q_or_short:
                        # Flag line_b as the collapsed second turn
                        if line_b.line_id not in flagged_map:
                            reason = (
                                f"Consecutive same-speaker dialogue collapse: adjacent lines "
                                f"share speaker '{line_a.speaker}' across a question or short response"
                            )
                            flagged_map[line_b.line_id] = build_suspicious_turn(
                                idx_b, reason, "consecutive_collapse"
                            )

        # -------------------------------------------------------------
        # Pattern 2: Narrator-Separated Dialogue Collapse
        # -------------------------------------------------------------
        for i in range(len(dialogue_indices) - 1):
            idx_a = dialogue_indices[i]
            idx_b = dialogue_indices[i + 1]

            # 1 to 3 narrator lines in between
            num_intervening = idx_b - idx_a - 1
            if 1 <= num_intervening <= 3:
                line_a = lines[idx_a]
                line_b = lines[idx_b]

                if line_a.speaker == line_b.speaker and line_a.speaker != "narrator":
                    intervening_texts = [lines[k].text for k in range(idx_a + 1, idx_b)]
                    combined_intervening = " ".join(intervening_texts)
                    has_continuation = _has_continuation_tag(combined_intervening)

                    if not has_continuation:
                        text_a = line_a.text.strip()
                        text_b = line_b.text.strip()
                        if (
                            "?" in text_a
                            or "?" in text_b
                            or len(text_a) <= max_short_response_chars
                            or len(text_b) <= max_short_response_chars
                        ):
                            if line_b.line_id not in flagged_map:
                                reason = (
                                    f"Narrator-separated dialogue collapse: '{line_a.speaker}' "
                                    f"resumed after narrative beat without continuation evidence"
                                )
                                flagged_map[line_b.line_id] = build_suspicious_turn(
                                    idx_b, reason, "narrator_separated_collapse"
                                )

        # -------------------------------------------------------------
        # Pattern 3: Low-Confidence Dialogue
        # -------------------------------------------------------------
        for idx in dialogue_indices:
            line = lines[idx]
            if line.speaker != "narrator":
                conf = line.speaker_confidence
                if conf is not None and conf < min_confidence:
                    if line.line_id not in flagged_map:
                        reason = f"Low speaker confidence ({conf:.2f} < {min_confidence:.2f})"
                        flagged_map[line.line_id] = build_suspicious_turn(
                            idx, reason, "low_confidence"
                        )

        # -------------------------------------------------------------
        # Pattern 4: Untagged Staccato Sequence Check
        # -------------------------------------------------------------
        # Find runs of 3+ dialogue lines within close proximity
        # where speaker evidence is weak / absent and confidence < 0.85
        staccato_run: list[int] = []
        for i in range(len(dialogue_indices)):
            cur_idx = dialogue_indices[i]
            if not staccato_run:
                staccato_run.append(cur_idx)
            else:
                prev_idx = staccato_run[-1]
                # If separated by at most 2 narrative lines
                if (cur_idx - prev_idx) <= 3:
                    staccato_run.append(cur_idx)
                else:
                    if len(staccato_run) >= 3:
                        _check_staccato_run(staccato_run, lines, flagged_map, build_suspicious_turn)
                    staccato_run = [cur_idx]
        if len(staccato_run) >= 3:
            _check_staccato_run(staccato_run, lines, flagged_map, build_suspicious_turn)

    return sorted(flagged_map.values(), key=lambda t: (t.chapter_number, t.line_id))


def _check_staccato_run(
    run_indices: list[int],
    lines: list[ScriptLine],
    flagged_map: dict[str, SuspiciousTurn],
    builder: Any,
) -> None:
    """Flag lines in an untagged staccato run if confidence is borderline or missing explicit tags."""
    weak_keywords = {"inferred", "context", "alternation", "scene", "default", "none"}
    for idx in run_indices:
        line = lines[idx]
        if line.line_id in flagged_map or line.speaker == "narrator":
            continue
        ev = (line.speaker_evidence or "").casefold().strip()
        conf = line.speaker_confidence or 0.0

        is_weak_evidence = (
            not ev
            or any(kw in ev for kw in weak_keywords)
            or len(ev) < 15
        )

        if is_weak_evidence and conf < 0.85:
            reason = (
                f"Untagged staccato dialogue turn with borderline confidence ({conf:.2f}) "
                f"and weak evidence tag"
            )
            flagged_map[line.line_id] = builder(idx, reason, "untagged_staccato")
