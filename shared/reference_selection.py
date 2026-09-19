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


#: How much a reference line is favoured for each distinct hard name it
#: contains, capped at `_PRIORITY_TERM_CAP` terms.
#:
#: The engine runs in-context learning from the reference clip, so whatever it
#: hears there conditions every later line in that voice. A reference that
#: already contains `Drizzt` said correctly biases the model toward saying it
#: that way again -- the same idea as caching a correct pronunciation, but
#: applied to conditioning rather than to audio, so nothing is spliced and
#: prosody stays natural.
#:
#: Deliberately small. Clarity and diversity are what make a reference usable
#: in the first place, and a line stuffed with names but poorly spoken makes a
#: worse reference than a clean one. This tips the balance between otherwise
#: comparable candidates; it does not override the other terms.
PRIORITY_TERM_BONUS = 0.75
_PRIORITY_TERM_CAP = 3


def reference_line_score(
    text: str,
    target_words: int = 22,
    priority_terms: frozenset[str] | None = None,
) -> float:
    """Score clarity/diversity without using acoustic or model inference.

    `priority_terms` are the book's hard names, cased-folded. A line that
    contains one is preferred, because it becomes part of what the engine is
    conditioned on.
    """
    clean = _clean(text)
    words = re.findall(r"[^\W_]+(?:['’-][^\W_]+)?", clean.lower())
    if not words:
        return -1_000.0
    count = len(words)
    priority_bonus = 0.0
    if priority_terms:
        present = {word for word in words if word in priority_terms}
        priority_bonus = PRIORITY_TERM_BONUS * min(len(present), _PRIORITY_TERM_CAP)
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
        + priority_bonus
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
    priority_terms: Iterable[str] = (),
) -> ReferenceTextSelection:
    """Choose diverse real dialogue, combining lines only when necessary.

    `priority_terms` nudges selection toward lines containing the book's hard
    names, so the clip the engine is conditioned on demonstrates them.
    """
    priority = frozenset(term.casefold() for term in priority_terms if term)
    unique: dict[str, tuple[int, str]] = {}
    for index, raw in enumerate(lines):
        clean = _clean(raw)
        key = clean.casefold()
        if clean and key not in unique:
            unique[key] = (index, clean)
    ranked = sorted(
        unique.values(),
        key=lambda item: (-reference_line_score(item[1], priority_terms=priority), item[0]),
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
        total_score += reference_line_score(candidate, priority_terms=priority)
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
                total_score += reference_line_score(seed, priority_terms=priority)
                used_seed = True

    text = _clean(" ".join(chosen))
    return ReferenceTextSelection(
        text=text,
        source_line_count=len(chosen) - int(used_seed),
        used_seed_text=used_seed,
        score=round(total_score / max(1, len(chosen)), 6) if math.isfinite(total_score) else -1_000.0,
    )
