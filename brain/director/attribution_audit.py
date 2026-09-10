"""Source-grounded release gate for audiobook speaker attribution."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from brain.director.script_generator import (
    _HE_SPEECH_TAG,
    _SHE_SPEECH_TAG,
    _SPEECH_VERB_SET,
    _SUBJECT_PRONOUNS,
    ScriptGenerator,
)
from brain.validators.tiered_adjudicator import _reads_as_attached_tag
from shared.constants import Gender
from shared.models import CharacterRegistry, ExtractedBook, ScriptChapter, ScriptLine

logger = logging.getLogger(__name__)

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


_FIRST_PERSON_POSSESSIVE = re.compile(r"\b(?:my|mine|our)\s+([a-z]{3,})\b", re.IGNORECASE)
_SECOND_PERSON_POSSESSIVE = re.compile(r"\byour\s+([a-z]{3,})\b", re.IGNORECASE)


def detect_possessive_contradictions(
    chapters: list[ScriptChapter],
) -> list[dict[str, Any]]:
    """Find a speaker who both owns and does not own the same thing.

    This is the independent consistency check that
    `docs/plans/targeted-block-adjudication-2026-09-06.md` names as the
    mitigation for its Risk 2, and it is deliberately kept **outside** the
    adjudicator and out of any prompt. The risk it answers:

    > The ch11 error was *detectable* because the block was self-contradictory
    > about who owns the tower. A block-level model is explicitly instructed to
    > produce a self-consistent assignment... This trades loud, detectable
    > errors for smooth, plausible, invisible ones.

    The signature of that error, from the shipped script:

    ```
    ch11_0148 [effron] "...You have never invited me to be a guest in YOUR tower."
    ch11_0149 [effron] "You will never be invited into MY tower, mother,"
    ```

    Both lines are attributed to Effron, and one speaker cannot both own and
    not own the tower inside a single unbroken turn. So: within a maximal
    same-speaker run, look for a noun carrying a first-person possessive in one
    line and a second-person possessive in another.

    Measured on the two analysed books, this fires **once per book**:

    * `the-finest-edge-of-twilight-book` -- `tower`, ch11: the real error.
    * `isles-of-the-emberdark` -- `way`, ch25: a false positive. "knowing your
      way home" and "find our way back" are routes, not possessions.

    One item per book is a reading, not a queue, which is why the result is
    reported rather than enforced. No stop-list of abstract nouns is applied:
    with a single false positive to learn from, that would be fitting to noise.
    """
    findings: list[dict[str, Any]] = []
    for chapter in chapters:
        run: list[ScriptLine] = []

        def _flush(run: list[ScriptLine], chapter: ScriptChapter = chapter) -> None:
            if len(run) < 2:
                return
            owned: dict[str, list[str]] = {}
            addressed: dict[str, list[str]] = {}
            for line in run:
                for match in _FIRST_PERSON_POSSESSIVE.finditer(line.text or ""):
                    owned.setdefault(match.group(1).lower(), []).append(line.line_id)
                for match in _SECOND_PERSON_POSSESSIVE.finditer(line.text or ""):
                    addressed.setdefault(match.group(1).lower(), []).append(line.line_id)
            for noun in sorted(set(owned) & set(addressed)):
                mine, yours = set(owned[noun]), set(addressed[noun])
                # Both forms in one line is a contrast ("your tower, not mine"),
                # not a contradiction. Require them on different lines.
                if not (mine - yours) or not (yours - mine):
                    continue
                findings.append(
                    {
                        "chapter_number": chapter.chapter_number,
                        "speaker": run[0].speaker,
                        "noun": noun,
                        "claimed_line_id": sorted(mine - yours)[0],
                        "disclaimed_line_id": sorted(yours - mine)[0],
                        "reason": (
                            f"{run[0].speaker!r} both owns and does not own {noun!r} "
                            "within one unbroken turn; one of these lines is attributed wrongly"
                        ),
                    }
                )

        for line in chapter.lines:
            if line.speaker == "narrator":
                continue
            if run and run[-1].speaker == line.speaker:
                run.append(line)
            else:
                _flush(run)
                run = [line]
        _flush(run)
    return findings


def tag_addressee(tag: str, registry: CharacterRegistry) -> str | None:
    """Who the speech tag says is being *spoken to*.

    The 2026-09-06 record establishes the principle -- "a speech tag names
    several people, only its subject speaks" -- and applies it to stop a name
    after the verb being read as an inverted subject. The same reading yields
    something useful in its own right: that name is the **addressee**, and a
    character cannot be spoken to by themselves.

    Only the two unambiguous shapes are read, because a wrong addressee would
    refute a correct attribution:

    * ``<speech verb> to <name>`` -- "the man said to Dusk."
    * ``<pronoun> <speech verb> <name>`` -- "he asked Breezy."

    A possessive is not an addressee: "moving as if to put his arm around
    Dusk's shoulders" is about Dusk without addressing him.
    """
    if not tag:
        return None
    lowered = " " + re.sub(r"\s+", " ", tag).strip().casefold() + " "
    verbs = "|".join(sorted(_SPEECH_VERB_SET, key=len, reverse=True))
    pronouns = "|".join(sorted(_SUBJECT_PRONOUNS, key=len, reverse=True))

    for character_id, character in registry.characters.items():
        if character_id == "narrator":
            continue
        names = {str(character_id).replace("_", " "), str(character.name or "")}
        names |= {str(alias) for alias in (character.aliases or [])}
        for name in names:
            cleaned = name.strip().casefold()
            if len(cleaned) < 3:
                continue
            token = re.escape(cleaned).replace(r"\ ", r"\s+")
            # "...said to Dusk" / "...asked Breezy" with a pronoun subject.
            if re.search(rf"\b(?:{verbs})\s+to\s+{token}(?!'s)\b", lowered) or re.search(
                rf"\b(?:{pronouns})\s+(?:\w+ly\s+)?(?:{verbs})\s+{token}(?!'s)\b", lowered
            ):
                return character_id
    return None


def _refutations(
    chapter: ScriptChapter,
    registry: CharacterRegistry,
) -> list[tuple[int, str, str, Gender | None]]:
    """Lines the text itself refutes, with what it refutes them for."""
    lines = chapter.lines
    found: list[tuple[int, str, str, Gender | None]] = []

    for finding in detect_possessive_contradictions([chapter]):
        index = next((i for i, line in enumerate(lines) if line.line_id == finding["disclaimed_line_id"]), None)
        if index is not None:
            found.append((index, "possessive_contradiction", finding["speaker"], None))

    for index, line in enumerate(lines):
        if not line.speaker or line.speaker == "narrator" or index + 1 >= len(lines):
            continue
        following = lines[index + 1]
        if following.speaker != "narrator":
            continue
        tag = str(following.text or "").strip()
        if not _reads_as_attached_tag(tag):
            continue
        named, kind, gender = ScriptGenerator._dialogue_tag_evidence(tag, registry)
        if named or gender is None or kind != "pronoun_gender":
            continue
        # Only a pronoun tied to the speech verb may refuse an attribution;
        # see the 2026-09-06 record on `ch28_0028`.
        if not (_HE_SPEECH_TAG.search(tag) or _SHE_SPEECH_TAG.search(tag)):
            continue
        character = registry.characters.get(line.speaker)
        if character and character.gender in (Gender.MALE, Gender.FEMALE) and character.gender != gender:
            found.append((index, "gendering_tag", line.speaker, gender))

    # A character cannot be spoken to by themselves. This catches the shape the
    # gender rule cannot: "the man said to Dusk." refutes Dusk without any
    # gender disagreement, because Dusk is male and so is "the man".
    for index, line in enumerate(lines):
        if not line.speaker or line.speaker == "narrator" or index + 1 >= len(lines):
            continue
        following = lines[index + 1]
        if following.speaker != "narrator":
            continue
        tag = str(following.text or "").strip()
        if not _reads_as_attached_tag(tag):
            continue
        if tag_addressee(tag, registry) == line.speaker and not any(
            existing_index == index for existing_index, _, _, _ in found
        ):
            found.append((index, "addressed_not_speaking", line.speaker, None))
    return found


#: How many spoken lines either side count as "this scene" when looking for the
#: one participant a refutation leaves standing.
REFUTATION_SCENE_WINDOW = 14


#: Context handed to the constrained-choice tier. Far wider than the per-line
#: adjudicator's window, because the question is one line rather than many and
#: the extra prefill is paid once.
CONSTRAINED_CHOICE_WINDOW = 30

#: Identical answers required across independent runs. An answer that moves
#: between runs is not an answer: on the open question, Gemini gave `dahlia`
#: at 1.00 and `effron` at 0.74 for `ch11_0148` minutes apart.
CONSTRAINED_CHOICE_RUNS = 3


def refuted_candidate_sets(
    chapters: list[ScriptChapter],
    registry: CharacterRegistry,
    *,
    window: int = REFUTATION_SCENE_WINDOW,
) -> list[dict[str, Any]]:
    """Every refuted line with the candidates the scene allows.

    `resolve_refuted_by_unique_candidate` acts on the entries with exactly one
    candidate. This exposes the rest -- the ones where the text alone cannot
    choose -- so a model can be asked a *closed* question about them.
    """
    out: list[dict[str, Any]] = []
    for chapter in chapters:
        lines = chapter.lines
        for index, source, refuted, required_gender in _refutations(chapter, registry):
            low, high = max(0, index - window), min(len(lines), index + window + 1)
            present = [ln.speaker for ln in lines[low:high] if ln.speaker and ln.speaker != "narrator"]
            candidates = []
            for speaker_id in dict.fromkeys(present):
                if speaker_id == refuted:
                    continue
                character = registry.characters.get(speaker_id)
                if required_gender is not None and (character is None or character.gender != required_gender):
                    continue
                candidates.append(speaker_id)
            out.append(
                {
                    "line_id": lines[index].line_id,
                    "chapter_number": chapter.chapter_number,
                    "index": index,
                    "refuted": refuted,
                    "source": source,
                    "required_gender": required_gender.value if required_gender else None,
                    "candidates": candidates,
                }
            )
    return out


def build_constrained_choice_prompt(
    chapter: ScriptChapter,
    index: int,
    refuted: str,
    candidates: list[str],
    registry: CharacterRegistry,
    *,
    why: str,
    window: int = CONSTRAINED_CHOICE_WINDOW,
) -> str:
    """Ask which of a fixed list speaks one line, given a wide scene.

    The open question -- "who speaks this line?" -- is the one the per-line
    adjudicator already answered wrongly, and the one Gemini answers
    differently on different runs. By the time this is called the
    deterministic layer has established two things it did not have then: who
    did *not* speak, and the complete set of who could have. That turns
    attribution into a multiple-choice question, which is a different and much
    easier task.
    """
    lines = chapter.lines
    low, high = max(0, index - window), min(len(lines), index + window + 1)
    context = "\n".join(
        f"{'>>> TARGET ' if i == index else '           '}[{lines[i].line_id}] {lines[i].speaker}: {lines[i].text}"
        for i in range(low, high)
    )
    roster = "\n".join(
        f"  - {cid}: {registry.characters[cid].name} ({registry.characters[cid].gender.value})"
        for cid in candidates
        if cid in registry.characters
    )
    return (
        "You are resolving ONE line of audiobook dialogue.\n\n"
        f"ESTABLISHED FACT: {refuted!r} did NOT speak the TARGET line, because {why}.\n"
        "This is settled by the author's own text and is not open to revision.\n\n"
        f"The speaker is exactly one of these, and no one else:\n{roster}\n\n"
        f"SCENE ({high - low} lines of context):\n{context}\n\n"
        "Choose which candidate speaks the TARGET line. Weigh who is being addressed, "
        "who answers whom, and any action beats. If the scene genuinely does not "
        "distinguish them, say so with low confidence rather than guessing.\n\n"
        'Return ONLY JSON: {"speaker_id": "<one of the candidates>", '
        '"confidence": 0.0-1.0, "evidence": "verbatim phrase from the scene"}'
    )


def resolve_refuted_by_unique_candidate(
    chapters: list[ScriptChapter],
    registry: CharacterRegistry,
    *,
    window: int = REFUTATION_SCENE_WINDOW,
) -> list[dict[str, Any]]:
    """Answer a refuted line when the scene leaves exactly one candidate.

    A refutation says who did *not* speak. Flagging that for a human is the
    obvious response and the wrong one here: reviewing an attribution means
    reading the passage, and the operator has not read the book. The 2026-09-04
    record makes the same argument for cast merges -- "approving a merge means
    reading the verbatim excerpts that justify it, which is a plot summary of a
    book the operator has not read yet". Anything resolvable without a human
    reading should be.

    Two refutations are available, both deterministic:

    * a **possessive contradiction** refutes the stored speaker outright
      (`detect_possessive_contradictions`);
    * a **gendering speech tag** refutes anyone of the other gender.

    If exactly one *other* speaker in the surrounding scene survives the
    refutation, that is the answer, and no one needs to read anything. If two
    or more survive, or none does, the line is left alone -- the rule is
    deliberately unable to guess.

    Measured on the two scripted books:

    ```
                                  refuted  unique  ambiguous  none
    the-finest-edge-of-twilight         1       1          0     0
    isles-of-the-emberdark              8       2          5     1
    ```

    All three resolutions were checked by hand against the passage.
    `ch11_0148` becomes `dahlia`, which is what the possessive contradiction,
    the tag on `ch11_0149`, the 2026-09-06 record and Gemini's adjudication
    tier all independently indicate. The one `none` is the known false positive
    in `isles-of-the-emberdark` -- "your way home" against "our way back" --
    where the rule correctly declines to act.

    Returns proposals; applying them is the caller's job.
    """
    proposals: list[dict[str, Any]] = []
    for chapter in chapters:
        lines = chapter.lines
        for index, source, refuted, required_gender in _refutations(chapter, registry):
            low, high = max(0, index - window), min(len(lines), index + window + 1)
            present = [
                line.speaker for line in lines[low:high] if line.speaker and line.speaker != "narrator"
            ]
            candidates = []
            for speaker_id in dict.fromkeys(present):
                if speaker_id == refuted:
                    continue
                character = registry.characters.get(speaker_id)
                if required_gender is not None and (character is None or character.gender != required_gender):
                    continue
                candidates.append(speaker_id)
            if len(candidates) != 1:
                continue
            proposals.append(
                {
                    "line_id": lines[index].line_id,
                    "chapter_number": chapter.chapter_number,
                    "from": refuted,
                    "to": candidates[0],
                    "source": source,
                    "reason": (
                        f"{source.replace('_', ' ')} rules out {refuted!r}; "
                        f"{candidates[0]!r} is the only other speaker in the scene it allows"
                    ),
                }
            )
    return proposals


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
            issues.append(_issue(chapter.number, None, None, "missing_script", "Chapter script is missing"))
            continue
        owners = _fragment_owners(script)
        fragments = ScriptGenerator._split_into_fragment_spans(chapter.text)
        chapter_speakers = ScriptGenerator._get_chapter_scoped_speakers(chapter.text, registry)
        para_dialogue_map, para_tag_map = ScriptGenerator._paragraph_attribution_maps(fragments, registry, chapter.text)

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
                else (prev_text if ScriptGenerator._is_leading_dialogue_tag(prev_text) else "")
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

            if embedded_term and (speaker != "narrator" or kind != "non_spoken_quote"):
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
                        owner.attribution_review_reason or "Speaker attribution requires human confirmation",
                        fragment.text,
                    )
                )
                continue

            if owner.attribution_resolver == "human":
                # A human reviewer has explicitly confirmed this attribution;
                # automated heuristics and speech-tag fallbacks must not overrule human judgment.
                continue

            # Identity-reveal parsing is relevant only to generic speakers.
            # Avoid compiling every registered name pattern for the thousands
            # of already-named dialogue lines in a full book audit.
            self_identity = (
                _self_identified_character(
                    owner.text,
                    registry,
                    prior_context=chapter.text[max(0, owner.source_start - 500) : owner.source_start],
                )
                if speaker in _GENERIC_SPEAKER_IDS
                else None
            )
            if speaker in _GENERIC_SPEAKER_IDS and self_identity is not None and self_identity != speaker:
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
            contradiction = exact is not None and exact != speaker and not compatible_named_role
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

    possessive_contradictions = detect_possessive_contradictions(scripts)
    if possessive_contradictions:
        logger.warning(
            "[AttributionAudit] %d self-contradictory possessive claim(s): %s",
            len(possessive_contradictions),
            "; ".join(f"{f['speaker']}/{f['noun']} ch{f['chapter_number']}" for f in possessive_contradictions[:5]),
        )

    return {
        "audit_version": AUDIT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "passed": not issues,
        "summary": {
            "chapters": len(book.chapters),
            "dialogue_fragments": dialogue_count,
            "narrator_quotations": narrator_quote_count,
            "blocking_issues": len(issues),
            "possessive_contradictions": len(possessive_contradictions),
        },
        "issues": issues,
        # Reported, never blocking. See `detect_possessive_contradictions`:
        # this is the independent check the block-adjudication plan requires,
        # and it must stay outside the adjudicator and out of every prompt.
        "possessive_contradictions": possessive_contradictions,
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
    lines_by_id = {line.line_id: line for chapter in scripts for line in chapter.lines}
    targets_by_line: dict[str, set[str]] = {}
    for issue in report["issues"]:
        line_id = str(issue.get("line_id") or "")
        target = str(issue.get("expected_speaker") or "")
        if issue.get("kind") != "named_tag" or not line_id or target not in registry.characters or target == "narrator":
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
            (line for line in chapter.lines if line.speaker in _GENERIC_SPEAKER_IDS and line.dialogue_kind == "spoken"),
            key=lambda line: (line.source_start or 0, line.line_id),
        )
        clusters: list[list[ScriptLine]] = []
        for line in generic_lines:
            if (
                not clusters
                or (line.source_start or 0) - (clusters[-1][-1].source_end or 0) > _IDENTITY_CLUSTER_MAX_GAP
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
                            max(0, line.source_start - 500) : line.source_start
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
        line.speaker_evidence = f"Deterministic attached source tag identifies '{target}'."
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
        expected_gender = Gender.FEMALE if generic.endswith("female") else Gender.MALE
        if target_character is None or target_character.gender != expected_gender:
            conflicted.extend(line.line_id for line in cluster)
            continue
        for line in cluster:
            previous = line.speaker
            line.speaker = target
            line.speaker_confidence = 1.0
            line.speaker_evidence = f"Deterministic self-identity reveal resolves this scene speaker as '{target}'."
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
        "attempted": len(targets_by_line)
        + sum(
            len(lines) for cluster_id, lines in identity_cluster_lines.items() if identity_cluster_targets[cluster_id]
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
    lines_by_id = {line.line_id: line for chapter in scripts for line in chapter.lines}
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
