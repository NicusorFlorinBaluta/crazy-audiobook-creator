"""Source-grounded release gate for audiobook speaker attribution."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from brain.director.script_generator import ScriptGenerator
from shared.constants import Gender
from shared.models import CharacterRegistry, ExtractedBook, ScriptChapter, ScriptLine

AUDIT_VERSION = "speaker-attribution-v5"

_GENERIC_SPEAKER_IDS = {
    "minor_female",
    "minor_male",
    "unnamed_female",
    "unnamed_male",
    "unknown_female",
    "unknown_male",
}
_IDENTITY_CLUSTER_MAX_GAP = 2_000


def _self_identified_character(
    text: str,
    registry: CharacterRegistry,
    *,
    prior_context: str = "",
) -> str | None:
    """Return a unique registered identity explicitly claimed by the speaker."""
    normalized = " ".join(str(text or "").split())
    matches: set[str] = set()
    answer = re.sub(r"^[\s\"'“”‘’]+|[\s\"'“”‘’,.!?;:]+$", "", normalized)
    identity_question = re.search(
        r"\b(?:what(?:'s|\s+is)\s+your\s+name|your\s+name|"
        r"who\s+are\s+you|what\s+should\s+i\s+call\s+you|call\s+you)\b",
        prior_context[-500:],
        re.IGNORECASE,
    )
    for character_id, character in registry.characters.items():
        if character_id == "narrator" or character_id in _GENERIC_SPEAKER_IDS:
            continue
        names = {
            str(character.name or "").strip(),
            character_id.replace("_", " ").strip(),
            *(str(alias).strip() for alias in (character.aliases or [])),
        }
        for name in names:
            if len(name) < 3:
                continue
            identity = re.escape(name).replace(r"\ ", r"\s+")
            if re.search(
                rf"\b(?:my\s+name\s+is|i\s+am|i['’]m|call\s+me)\s+{identity}\b",
                normalized,
                re.IGNORECASE,
            ):
                matches.add(character_id)
                break
            if identity_question and answer.casefold() == " ".join(name.split()).casefold():
                matches.add(character_id)
                break
    return next(iter(matches)) if len(matches) == 1 else None


def audit_book_attribution(
    book: ExtractedBook,
    registry: CharacterRegistry,
    scripts: list[ScriptChapter],
    *,
    confidence_threshold: float = 0.55,
) -> dict[str, Any]:
    """Audit immutable source fragments against their final script owners."""
    scripts_by_number = {chapter.chapter_number: chapter for chapter in scripts}
    issues: list[dict[str, Any]] = []
    dialogue_count = 0
    narrator_quote_count = 0

    for chapter in book.chapters:
        script = scripts_by_number.get(chapter.number)
        if script is None:
            issues.append(
                _issue(chapter.number, None, None, "missing_script", "Chapter script is missing")
            )
            continue
        owners = _fragment_owners(script)
        fragments = ScriptGenerator._split_into_fragment_spans(chapter.text)
        chapter_speakers = ScriptGenerator._get_chapter_scoped_speakers(chapter.text, registry)
        para_dialogue_map, para_tag_map = (
            ScriptGenerator._paragraph_attribution_maps(
                fragments, registry, chapter.text
            )
        )

        for index, fragment in enumerate(fragments):
            if not ScriptGenerator._is_dialogue_fragment(fragment.text):
                continue
            dialogue_count += 1
            owner = owners.get(index)
            if owner is None:
                issues.append(
                    _issue(
                        chapter.number,
                        index,
                        None,
                        "unmapped_dialogue_fragment",
                        "Quoted source fragment is not represented by a script line",
                        fragment.text,
                    )
                )
                continue

            speaker = ScriptGenerator._normalize_speaker_id(owner.speaker)
            kind = owner.dialogue_kind or ("spoken" if speaker != "narrator" else None)
            if speaker not in registry.characters:
                issues.append(
                    _issue(
                        chapter.number,
                        index,
                        owner,
                        "unknown_speaker",
                        f"Quoted fragment uses unknown speaker '{speaker}'",
                        fragment.text,
                    )
                )
                continue

            next_frag = fragments[index + 1] if index + 1 < len(fragments) else None
            prev_frag = fragments[index - 1] if index > 0 else None
            next_same_para = next_frag and "\n" not in chapter.text[fragment.end : next_frag.start]
            prev_same_para = prev_frag and "\n" not in chapter.text[prev_frag.end : fragment.start]

            next_text = next_frag.text if (next_frag and next_same_para) else ""
            prev_text = prev_frag.text if (prev_frag and prev_same_para) else ""
            tag_text = (
                next_text
                if ScriptGenerator._is_pure_dialogue_tag(next_text)
                else (
                    prev_text
                    if ScriptGenerator._is_leading_dialogue_tag(prev_text)
                    else ""
                )
            )
            exact: str | None = None
            evidence_kind: str | None = None
            evidence_gender: Gender | None = None
            if tag_text:
                exact, evidence_kind, evidence_gender = ScriptGenerator._dialogue_tag_evidence(
                    tag_text,
                    registry,
                )
            if exact is None and index in para_tag_map:
                para_exact, para_kind, para_gender = para_tag_map[index]
                if para_exact is not None:
                    exact, evidence_kind, evidence_gender = para_exact, para_kind, para_gender

            collective_tag = ScriptGenerator._is_collective_dialogue_tag(tag_text)
            embedded_term = ScriptGenerator._is_embedded_quoted_term(index, fragments)

            if embedded_term and (
                speaker != "narrator" or kind != "non_spoken_quote"
            ):
                issues.append(
                    _issue(
                        chapter.number,
                        index,
                        owner,
                        "embedded_quoted_term",
                        "Short lexical/scare quote embedded in narration is assigned as a spoken turn",
                        fragment.text,
                    )
                )
                continue

            if speaker == "narrator":
                narrator_quote_count += 1
                if kind == "reported_collective_speech":
                    if not collective_tag or len(owner.speaker_evidence.strip()) < 12:
                        issues.append(
                            _issue(
                                chapter.number,
                                index,
                                owner,
                                "unsupported_reported_collective_speech",
                                "Reported collective speech lacks an adjacent anonymous plural speech tag or evidence",
                                fragment.text,
                            )
                        )
                    continue
                if kind != "non_spoken_quote":
                    issues.append(
                        _issue(
                            chapter.number,
                            index,
                            owner,
                            "narrator_spoken_dialogue",
                            "Narrator owns a quotation without an explicit non-spoken classification",
                            fragment.text,
                        )
                    )
                    continue
                if len(owner.speaker_evidence.strip()) < 12:
                    issues.append(
                        _issue(
                            chapter.number,
                            index,
                            owner,
                            "unsupported_non_spoken_quote",
                            "Non-spoken quotation lacks explicit source-grounded evidence",
                            fragment.text,
                        )
                    )
                    continue
            elif kind == "non_spoken_quote":
                issues.append(
                    _issue(
                        chapter.number,
                        index,
                        owner,
                        "non_spoken_character_contradiction",
                        "A non-spoken quotation is assigned to a character voice",
                        fragment.text,
                    )
                )
                continue
            elif collective_tag:
                issues.append(
                    _issue(
                        chapter.number,
                        index,
                        owner,
                        "collective_speech_character_contradiction",
                        "Anonymous plural reported speech is assigned to a named character",
                        fragment.text,
                    )
                )
                continue

            if owner.attribution_review_required:
                issues.append(
                    _issue(
                        chapter.number,
                        index,
                        owner,
                        "attribution_review_required",
                        owner.attribution_review_reason
                        or "Speaker attribution requires human confirmation",
                        fragment.text,
                    )
                )
                continue

            # Identity-reveal parsing is relevant only to generic speakers.
            # Avoid compiling every registered name pattern for the thousands
            # of already-named dialogue lines in a full book audit.
            self_identity = (
                _self_identified_character(
                    owner.text,
                    registry,
                    prior_context=chapter.text[
                        max(0, owner.source_start - 500):owner.source_start
                    ],
                )
                if speaker in _GENERIC_SPEAKER_IDS
                else None
            )
            if (
                speaker in _GENERIC_SPEAKER_IDS
                and self_identity is not None
                and self_identity != speaker
            ):
                issues.append(
                    _issue(
                        chapter.number,
                        index,
                        owner,
                        "self_identified_generic_speaker",
                        "A generic speaker explicitly identifies as a registered character",
                        fragment.text,
                        expected_speaker=self_identity,
                    )
                )
                continue

            confidence = owner.speaker_confidence
            if confidence is None or confidence < confidence_threshold:
                issues.append(
                    _issue(
                        chapter.number,
                        index,
                        owner,
                        "low_or_missing_confidence",
                        "Quoted fragment lacks release-grade speaker confidence",
                        fragment.text,
                    )
                )
                continue

            character = registry.characters.get(speaker)
            compatible_named_role = bool(
                evidence_kind == "generic_role_tag"
                and exact in _GENERIC_SPEAKER_IDS
                and speaker not in _GENERIC_SPEAKER_IDS
                and character is not None
                and evidence_gender is not None
                and character.gender == evidence_gender
            )
            contradiction = (
                exact is not None
                and exact != speaker
                and not compatible_named_role
            )
            gender_contradiction = (
                evidence_gender is not None
                and character is not None
                and speaker != "narrator"
                and kind != "non_spoken_quote"
                and character.gender in (Gender.MALE, Gender.FEMALE)
                and character.gender != evidence_gender
            )
            if contradiction or gender_contradiction:
                detail = (
                    f"Attached speech tag identifies '{exact}'"
                    if contradiction
                    else f"Attached speech tag identifies a {evidence_gender.value} speaker"
                )
                issues.append(
                    _issue(
                        chapter.number,
                        index,
                        owner,
                        evidence_kind or "dialogue_tag_contradiction",
                        detail,
                        fragment.text,
                        expected_speaker=exact,
                    )
                )
                continue



            if speaker != "narrator" and speaker not in chapter_speakers:
                issues.append(
                    _issue(
                        chapter.number,
                        index,
                        owner,
                        "absent_character_in_chapter",
                        f"Character '{speaker}' has no presence or mention in Chapter {chapter.number}",
                        fragment.text,
                    )
                )
                continue

    return {
        "audit_version": AUDIT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "passed": not issues,
        "summary": {
            "chapters": len(book.chapters),
            "dialogue_fragments": dialogue_count,
            "narrator_quotations": narrator_quote_count,
            "blocking_issues": len(issues),
        },
        "issues": issues,
    }


def repair_deterministic_named_attribution(
    book: ExtractedBook,
    registry: CharacterRegistry,
    scripts: list[ScriptChapter],
    *,
    confidence_threshold: float = 0.55,
) -> dict[str, Any]:
    """Repair unambiguous registered names from source tags and identity reveals.

    A grouped script line can own multiple source fragments. It is changed only
    when every named-tag contradiction for that line identifies the same
    registered character; conflicting evidence remains blocking.
    """
    report = audit_book_attribution(
        book,
        registry,
        scripts,
        confidence_threshold=confidence_threshold,
    )
    lines_by_id = {
        line.line_id: line
        for chapter in scripts
        for line in chapter.lines
    }
    targets_by_line: dict[str, set[str]] = {}
    for issue in report["issues"]:
        line_id = str(issue.get("line_id") or "")
        target = str(issue.get("expected_speaker") or "")
        if (
            issue.get("kind") != "named_tag"
            or not line_id
            or target not in registry.characters
            or target == "narrator"
        ):
            continue
        targets_by_line.setdefault(line_id, set()).add(target)

    # A character may enter a scene under a generic label and reveal their name
    # later.  Repair the surrounding generic-speaker cluster only when that
    # cluster contains exactly one explicit registered self-identity.
    identity_cluster_targets: dict[str, set[str]] = {}
    identity_cluster_lines: dict[str, list[ScriptLine]] = {}
    chapter_text = {chapter.number: chapter.text for chapter in book.chapters}
    for chapter in scripts:
        generic_lines = sorted(
            (
                line
                for line in chapter.lines
                if line.speaker in _GENERIC_SPEAKER_IDS
                and line.dialogue_kind == "spoken"
            ),
            key=lambda line: (line.source_start or 0, line.line_id),
        )
        clusters: list[list[ScriptLine]] = []
        for line in generic_lines:
            if (
                not clusters
                or (line.source_start or 0) - (clusters[-1][-1].source_end or 0)
                > _IDENTITY_CLUSTER_MAX_GAP
                or line.speaker != clusters[-1][-1].speaker
            ):
                clusters.append([line])
            else:
                clusters[-1].append(line)
        for cluster in clusters:
            targets = {
                target
                for line in cluster
                if (
                    target := _self_identified_character(
                        line.text,
                        registry,
                        prior_context=chapter_text.get(chapter.chapter_number, "")[
                            max(0, line.source_start - 500):line.source_start
                        ],
                    )
                )
                is not None
            }
            cluster_id = cluster[0].line_id
            identity_cluster_targets[cluster_id] = targets
            identity_cluster_lines[cluster_id] = cluster

    repaired: list[dict[str, str]] = []
    conflicted: list[str] = []
    for line_id, targets in sorted(targets_by_line.items()):
        if len(targets) != 1:
            conflicted.append(line_id)
            continue
        line = lines_by_id.get(line_id)
        if line is None:
            continue
        target = next(iter(targets))
        previous = line.speaker
        if previous == target:
            continue
        line.speaker = target
        line.speaker_confidence = 1.0
        line.speaker_evidence = (
            f"Deterministic attached source tag identifies '{target}'."
        )
        line.attribution_resolver = "deterministic_named_tag"
        line.attribution_review_required = False
        line.attribution_review_reason = ""
        line.attribution_confidence_history.append(
            {
                "resolver": "deterministic_named_tag",
                "model": "source_parser",
                "decision": "resolved",
                "speaker_id": target,
                "confidence": 1.0,
                "reason": "Unique registered character in attached source tag",
            }
        )
        repaired.append({"line_id": line_id, "from": previous, "to": target})

    for cluster_id, targets in sorted(identity_cluster_targets.items()):
        if not targets:
            continue
        if len(targets) != 1:
            conflicted.extend(line.line_id for line in identity_cluster_lines[cluster_id])
            continue
        target = next(iter(targets))
        target_character = registry.characters.get(target)
        cluster = identity_cluster_lines[cluster_id]
        generic = cluster[0].speaker
        expected_gender = (
            Gender.FEMALE if generic.endswith("female") else Gender.MALE
        )
        if target_character is None or target_character.gender != expected_gender:
            conflicted.extend(line.line_id for line in cluster)
            continue
        for line in cluster:
            previous = line.speaker
            line.speaker = target
            line.speaker_confidence = 1.0
            line.speaker_evidence = (
                f"Deterministic self-identity reveal resolves this scene speaker as '{target}'."
            )
            line.attribution_resolver = "deterministic_identity_reveal"
            line.attribution_review_required = False
            line.attribution_review_reason = ""
            line.attribution_confidence_history.append(
                {
                    "resolver": "deterministic_identity_reveal",
                    "model": "source_parser",
                    "decision": "resolved",
                    "speaker_id": target,
                    "confidence": 1.0,
                    "reason": "Unique registered self-identity in contiguous generic-speaker scene",
                }
            )
            repaired.append({"line_id": line.line_id, "from": previous, "to": target})
    if repaired:
        ScriptGenerator.sync_dialogue_counts(scripts, registry)
    return {
        "attempted": len(targets_by_line) + sum(
            len(lines)
            for cluster_id, lines in identity_cluster_lines.items()
            if identity_cluster_targets[cluster_id]
        ),
        "repaired": repaired,
        "conflicted_line_ids": conflicted,
    }


def queue_attribution_audit_issues(
    report: dict[str, Any],
    scripts: list[ScriptChapter],
    *,
    confidence_threshold: float,
) -> list[str]:
    """Route deterministic audit contradictions through external validation.

    Script-director confidence cannot override a source-grounded release-gate
    contradiction.  Mark those lines uncertain before Gemini is invoked so the
    normal escalation and provenance path can adjudicate them automatically.
    """
    lines_by_id = {
        line.line_id: line
        for chapter in scripts
        for line in chapter.lines
    }
    queued: list[str] = []
    confidence_ceiling = max(0.0, confidence_threshold - 0.01)
    for issue in report.get("issues", []):
        line_id = str(issue.get("line_id") or "")
        line = lines_by_id.get(line_id)
        if line is None:
            continue
        reason = (
            f"Deterministic attribution audit ({issue.get('kind', 'issue')}): "
            f"{issue.get('message', 'source evidence contradicts the assignment')}"
        )
        line.attribution_review_required = True
        line.attribution_review_reason = reason
        line.speaker_confidence = min(
            float(line.speaker_confidence or 0.0),
            confidence_ceiling,
        )
        if line_id not in queued:
            queued.append(line_id)
    return queued


def _fragment_owners(script: ScriptChapter) -> dict[int, ScriptLine]:
    owners: dict[int, ScriptLine] = {}
    for line in script.lines:
        fragment_ids = list(line.source_fragment_ids)
        if not fragment_ids and line.source_fragment_id is not None:
            fragment_ids = [line.source_fragment_id]
        for fragment_id in fragment_ids:
            owners[fragment_id] = line
    return owners


def _issue(
    chapter_number: int,
    fragment_id: int | None,
    line: ScriptLine | None,
    kind: str,
    message: str,
    text: str = "",
    *,
    expected_speaker: str | None = None,
) -> dict[str, Any]:
    issue = {
        "chapter_number": chapter_number,
        "fragment_id": fragment_id,
        "line_id": line.line_id if line is not None else None,
        "speaker": line.speaker if line is not None else None,
        "kind": kind,
        "message": message,
        "source_excerpt": text.strip()[:240],
    }
    if expected_speaker:
        issue["expected_speaker"] = expected_speaker
    return issue
