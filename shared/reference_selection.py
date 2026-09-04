"""Deterministic, source-first voice-reference text selection."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ReferenceTextSelection:
    text: str
    source_line_count: int
    used_seed_text: bool
    score: float


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def reference_line_score(text: str, target_words: int = 22) -> float:
    """Score clarity/diversity without using acoustic or model inference."""
    clean = _clean(text)
    words = re.findall(r"[^\W_]+(?:['’-][^\W_]+)?", clean.lower())
    if not words:
        return -1_000.0
    count = len(words)
    unique_ratio = len(set(words)) / count
    most_common = max(words.count(word) for word in set(words)) / count
    length_score = 1.0 - min(1.0, abs(count - target_words) / target_words)
    punctuation_penalty = min(1.0, clean.count("!") / 3.0)
    all_caps = sum(token.isupper() and len(token) > 1 for token in clean.split())
    repetition_penalty = max(0.0, most_common - 0.18) * 3.0
    very_short_penalty = 1.5 if count < 4 else 0.0
    return round(
        length_score * 2.0
        + unique_ratio * 2.0
        - repetition_penalty
        - punctuation_penalty
        - min(1.0, all_caps / 3.0)
        - very_short_penalty,
        6,
    )


def select_reference_text(
    lines: Iterable[str],
    *,
    seed_text: str = "",
    minimum_words: int = 15,
    maximum_words: int = 38,
) -> ReferenceTextSelection:
    """Choose diverse real dialogue, combining lines only when necessary."""
    unique: dict[str, tuple[int, str]] = {}
    for index, raw in enumerate(lines):
        clean = _clean(raw)
        key = clean.casefold()
        if clean and key not in unique:
            unique[key] = (index, clean)
    ranked = sorted(
        unique.values(),
        key=lambda item: (-reference_line_score(item[1]), item[0]),
    )
    chosen: list[str] = []
    chosen_words: set[str] = set()
    total_words = 0
    total_score = 0.0
    for _, candidate in ranked:
        words = re.findall(r"[^\W_]+", candidate.lower())
        if not words:
            continue
        new_ratio = len(set(words) - chosen_words) / len(set(words))
        if chosen and new_ratio < 0.35:
            continue
        if chosen and total_words + len(words) > maximum_words:
            continue
        chosen.append(candidate)
        chosen_words.update(words)
        total_words += len(words)
        total_score += reference_line_score(candidate)
        if total_words >= minimum_words:
            break

    used_seed = False
    seed = _clean(seed_text)
    if total_words < minimum_words and seed:
        seed_words = re.findall(r"[^\W_]+", seed.lower())
        if seed.casefold() not in {value.casefold() for value in chosen}:
            remaining = maximum_words - total_words
            if remaining > 0:
                chosen.append(" ".join(seed.split()[:remaining]))
                total_words += min(len(seed_words), remaining)
                total_score += reference_line_score(seed)
                used_seed = True

    text = _clean(" ".join(chosen))
    return ReferenceTextSelection(
        text=text,
        source_line_count=len(chosen) - int(used_seed),
        used_seed_text=used_seed,
        score=round(total_score / max(1, len(chosen)), 6) if math.isfinite(total_score) else -1_000.0,
    )
