"""Deterministic book-local pronunciation inventory and synthesis helpers."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


_COMMON_SENTENCE_WORDS = {
    "After", "Again", "Ago", "All", "Always", "And", "As", "Because", "Before", "But",
    "Could", "Every", "Finally", "First", "For", "From", "Had", "Have",
    "Company", "Father", "He", "Her", "Here", "His", "How", "However", "If", "Instead",
    "It", "Its", "No", "Not", "Now", "One", "Only", "Or", "Perhaps",
    "She", "So", "Something", "That", "The", "Their", "Then", "There",
    "These", "They", "This", "Those", "Though", "Through", "Until", "Was",
    "We", "What", "Whatever", "When", "Where", "Whether", "Which", "While",
    "Uncle", "Who", "Why", "Will", "With", "Without", "Would", "Years", "Yes", "You", "Your",
    "I'd", "I'll", "I'm", "I've",
}
_CANDIDATE_PATTERN = re.compile(r"\b[A-Z][A-Za-z'’-]{2,}\b")


def _is_sentence_initial(text: str, start: int) -> bool:
    """Return true when a token only has sentence punctuation/quotes before it."""
    prefix = text[:start].rstrip()
    while prefix and prefix[-1] in '\"\'“”‘’([{':
        prefix = prefix[:-1].rstrip()
    return not prefix or prefix[-1] in ".!?"


def _validate_entries(payload: Any, source: Path) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError(f"Pronunciation dictionary must be an object: {source}")
    result: dict[str, str] = {}
    for word, replacement in payload.items():
        if not isinstance(word, str) or not isinstance(replacement, str):
            raise ValueError("Pronunciation entries must map text to text")
        word = word.strip()
        replacement = replacement.strip()
        if not word or not replacement:
            raise ValueError("Pronunciation entries cannot be empty")
        if len(word) > 120 or len(replacement) > 240:
            raise ValueError("Pronunciation entry exceeds the safe length limit")
        if any(ord(char) < 32 for char in word + replacement):
            raise ValueError("Pronunciation entries cannot contain control characters")
        result[word] = replacement
    return result


def load_pronunciation_dictionary(
    project_dir: Path,
    global_path: Path | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Load validated mappings and their source, with project entries winning."""
    global_path = global_path or Path("brain/pronunciation_dict.json")
    mappings: dict[str, tuple[str, str, str]] = {}
    for source_name, path in (
        ("global", global_path),
        ("project", project_dir / "pronunciation_dict.json"),
    ):
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"Invalid pronunciation dictionary: {path}") from exc
        for word, replacement in _validate_entries(raw, path).items():
            mappings[word.casefold()] = (word, replacement, source_name)
    return (
        {word: replacement for word, replacement, _ in mappings.values()},
        {word: source for word, _, source in mappings.values()},
    )


def apply_pronunciations(text: str, mappings: dict[str, str]) -> str:
    """Apply longest-first replacements once, never recursively."""
    folded = {word.casefold(): (word, replacement) for word, replacement in mappings.items()}
    ordered = sorted(folded.values(), key=lambda item: (-len(item[0]), item[0].casefold()))
    if not ordered:
        return text
    pattern = re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(word) for word, _ in ordered) + r")(?!\w)",
        re.IGNORECASE,
    )
    return pattern.sub(lambda match: folded[match.group(0).casefold()][1], text)


def build_pronunciation_inventory(project_dir: Path) -> dict[str, Any]:
    """Inventory verified mappings and repeated unresolved book terms.

    This deliberately does not invent a phonetic spelling. Unresolved terms are
    review candidates until a deterministic project mapping is supplied.
    """
    script_path = project_dir / "book_script.json"
    if not script_path.exists():
        return {"schema": 1, "verified": 0, "unresolved": 0, "candidates": []}
    payload = json.loads(script_path.read_text(encoding="utf-8"))
    mappings, mapping_sources = load_pronunciation_dictionary(project_dir)
    mapping_by_folded = {
        word.casefold(): (word, replacement)
        for word, replacement in mappings.items()
    }
    source_by_folded = {word.casefold(): source for word, source in mapping_sources.items()}

    character_names: set[str] = set()
    characters_path = project_dir / "characters.json"
    if characters_path.exists():
        characters = json.loads(characters_path.read_text(encoding="utf-8")).get(
            "characters", {}
        )
        for character_id, info in characters.items():
            character_names.add(str(character_id).replace("_", " ").casefold())
            if isinstance(info, dict) and info.get("name"):
                character_names.add(str(info["name"]).casefold())

    # Gender-labelled group voices represent one source-book entity. Include the
    # shared alias, but do not show fragments such as "Ones" or "Above" alone.
    character_aliases = set(character_names)
    for name in tuple(character_names):
        words = name.split()
        if len(words) > 2 and words[-1] in {"male", "female"}:
            character_aliases.add(" ".join(words[:-1]))
    multiword_aliases = {name for name in character_aliases if " " in name}
    multiword_parts = {part for name in multiword_aliases for part in name.split()}

    counts: dict[str, int] = defaultdict(int)
    display: dict[str, str] = {}
    chapters: dict[str, set[int]] = defaultdict(set)
    contexts: dict[str, list[str]] = defaultdict(list)
    mid_sentence: set[str] = set()

    def record(term: str, chapter_number: int, text: str) -> None:
        key = term.casefold()
        counts[key] += 1
        display.setdefault(key, term)
        chapters[key].add(chapter_number)
        if len(contexts[key]) < 3:
            contexts[key].append(text[:240])

    for chapter_index, chapter in enumerate(payload.get("chapters", []), 1):
        chapter_number = int(chapter.get("chapter_number") or chapter_index)
        for line in chapter.get("lines", chapter.get("utterances", [])):
            text = line.get("text") if isinstance(line, dict) else None
            if not isinstance(text, str):
                continue
            occupied: list[tuple[int, int]] = []
            for alias in sorted(multiword_aliases, key=lambda value: (-len(value), value)):
                for alias_match in re.finditer(
                    rf"(?<!\w){re.escape(alias)}(?!\w)", text, re.IGNORECASE
                ):
                    record(alias_match.group(0), chapter_number, text)
                    occupied.append(alias_match.span())
            for match in _CANDIDATE_PATTERN.finditer(text):
                if any(start <= match.start() < end for start, end in occupied):
                    continue
                term = re.sub(r"(?:'s|’s)$", "", match.group(0), flags=re.IGNORECASE)
                if term in _COMMON_SENTENCE_WORDS:
                    continue
                key = term.casefold()
                if key in multiword_parts and key not in character_aliases:
                    continue
                record(term, chapter_number, text)
                if not _is_sentence_initial(text, match.start()):
                    mid_sentence.add(key)

    all_keys = set(counts) | set(mapping_by_folded)
    candidates: list[dict[str, Any]] = []
    for key in all_keys:
        verified = key in mapping_by_folded
        occurrence_count = counts.get(key, 0)
        if not verified and occurrence_count < 2 and key not in character_aliases:
            continue
        if not verified and key not in character_aliases and key not in mid_sentence:
            continue
        mapped_word, replacement = mapping_by_folded.get(key, (display.get(key, key), None))
        candidates.append(
            {
                "term": display.get(key, mapped_word),
                "status": "verified" if verified else "review_required",
                "spoken_text": replacement,
                "mapping_source": source_by_folded.get(key),
                "occurrences": occurrence_count,
                "chapters": sorted(chapters.get(key, set())),
                "contexts": contexts.get(key, []),
            }
        )
    candidates.sort(
        key=lambda item: (
            item["status"] != "review_required",
            -item["occurrences"],
            item["term"].casefold(),
        )
    )
    return {
        "schema": 1,
        "verified": sum(item["status"] == "verified" for item in candidates),
        "unresolved": sum(item["status"] == "review_required" for item in candidates),
        "candidates": candidates,
    }
