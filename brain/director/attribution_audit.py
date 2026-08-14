"""Source-grounded release gate for audiobook speaker attribution."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from brain.director.script_generator import ScriptGenerator
from shared.constants import Gender
from shared.models import CharacterRegistry, ExtractedBook, ScriptChapter, ScriptLine


AUDIT_VERSION = "speaker-attribution-v3"


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

            next_text = fragments[index + 1].text if index + 1 < len(fragments) else ""
            prev_text = fragments[index - 1].text if index > 0 else ""
            tag_text = (
                next_text
                if ScriptGenerator._is_pure_dialogue_tag(next_text)
                else (
                    prev_text
                    if ScriptGenerator._is_leading_dialogue_tag(prev_text)
                    else ""
                )
            )
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

            if not tag_text:
                continue
            exact, evidence_kind, evidence_gender = ScriptGenerator._dialogue_tag_evidence(
                tag_text,
                registry,
            )
            character = registry.characters.get(speaker)
            contradiction = exact is not None and exact != speaker
            gender_contradiction = (
                evidence_gender is not None
                and character is not None
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
                    )
                )

    return {
        "audit_version": AUDIT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "passed": not issues,
        "summary": {
            "chapters": len(book.chapters),
            "dialogue_fragments": dialogue_count,
            "narrator_quotations": narrator_quote_count,
            "blocking_issues": len(issues),
        },
        "issues": issues,
    }


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
) -> dict[str, Any]:
    return {
        "chapter_number": chapter_number,
        "fragment_id": fragment_id,
        "line_id": line.line_id if line is not None else None,
        "speaker": line.speaker if line is not None else None,
        "kind": kind,
        "message": message,
        "source_excerpt": text.strip()[:240],
    }
