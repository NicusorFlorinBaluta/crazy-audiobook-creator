"""Whole-cast duplicate detection: local guards and merge application.

The problem this exists for
---------------------------
`CharacterAnalyzer._adjudicate_name_candidates` proposes identity pairs only
from *lexical* overlap -- a shared distinctive token, or one id being a suffix
of the other. Measured on a real 57-character cast, that lets it consider **24
of 1,540 pairs (1.6%)**. A character recorded once under a proper name and
again under an appellative shares no token, so no pair is ever proposed and the
duplicate survives into casting as a second voice.

This module handles the half of the problem that must not be delegated to a
model: deciding when a proposed merge is *forbidden*, and applying an accepted
one without losing data.

The conjunction veto
--------------------
The load-bearing guard. If the source text ever joins the two names as separate
participants -- "Ilnezhara and Tazmikella", "Bruenor and Drizzt" -- they are two
people, whatever a model claims.

Proximity alone does **not** work, and this was measured rather than assumed.
On the same book, counting occurrences within 200 characters:

    Ilnezhara / Tazmikella   (distinct twins)   6
    Jarlaxle  / Uncle Jax    (same person)     10
    Regis     / Rumblebelly  (same person)     15

Aliases co-occur *more* than distinct characters, because prose introduces an
alias right beside the name it replaces. Conjunction separates them cleanly:

    Ilnezhara / Tazmikella   2      Jarlaxle / Uncle Jax          0
    Bruenor   / Drizzt       2      Regis    / Rumblebelly        0
    Catti-brie/ Wulfgar      1      Drizzt   / Drizzt Do'Urden    0
    Entreri   / Jarlaxle     3      Catti-brie / Catti-brie Do'Urden 0

Every distinct pair is conjoined at least once; no alias pair ever is.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any

logger = logging.getLogger(__name__)

# How the source joins two separate participants. Deliberately narrow: a comma
# alone is too loose (it matches "Drizzt, the drow ranger, walked"), so a bare
# comma must still be followed by a conjunction or another name in a list.
_CONJUNCTIONS = r"(?:and|&|nor|or|versus|vs\.?)"

# Names too generic to veto on. "the dwarf" conjoined with "the elf" says
# nothing about two registry entries, and matching them would veto real merges.
_UNVETOABLE = {
    "man",
    "woman",
    "boy",
    "girl",
    "child",
    "elf",
    "dwarf",
    "drow",
    "human",
    "king",
    "queen",
    "lord",
    "lady",
    "master",
    "mistress",
    "sister",
    "brother",
    "father",
    "mother",
    "narrator",
    "stranger",
    "guard",
    "soldier",
    "servant",
}


def _vetoable_terms(entry: dict[str, Any], char_id: str) -> list[str]:
    """Names specific enough that conjoining them means two people."""
    candidates = [str(entry.get("name") or char_id), char_id.replace("_", " ")]
    candidates += [str(a) for a in entry.get("aliases") or []]
    terms: list[str] = []
    for raw in candidates:
        term = raw.strip()
        if len(term) < 3:
            continue
        words = [
            w.strip("'\".,;:-")
            for w in re.split(r"[\s_]+", term.lower())
            # Articles and prepositions carry no identity, so they must not
            # rescue an otherwise generic term ("the dwarf") from the filter.
            if w and w.strip("'\".,;:-") not in {"the", "a", "an", "of", "de"}
        ]
        # A term made only of generic words cannot carry a veto.
        if not words or all(w in _UNVETOABLE for w in words):
            continue
        if term not in terms:
            terms.append(term)
    return terms


def conjunction_count(text: str, left_terms: list[str], right_terms: list[str]) -> int:
    """How often the source names both sides as separate participants."""
    if not text or not left_terms or not right_terms:
        return 0
    # Longest first: regex alternation is ordered, and a short alternative that
    # is a prefix of a longer one would otherwise win and cut the match short.
    left = "|".join(re.escape(t) for t in sorted(left_terms, key=len, reverse=True))
    right = "|".join(re.escape(t) for t in sorted(right_terms, key=len, reverse=True))
    patterns = [
        # "Bruenor and Drizzt", "Bruenor, and Drizzt"
        rf"\b(?:{left})\b\s*(?:,\s*)?{_CONJUNCTIONS}\s+\b(?:{right})\b",
        rf"\b(?:{right})\b\s*(?:,\s*)?{_CONJUNCTIONS}\s+\b(?:{left})\b",
        # Serial list: "Bruenor, Drizzt, and Catti-brie". A bare comma alone is
        # NOT enough -- "Jarlaxle, Uncle Jax to the girl, smiled" is apposition
        # naming one person, and vetoing on that would refuse the very merges
        # this exists to find. Requiring the list to continue with another
        # comma or a conjunction separates the two.
        rf"\b(?:{left})\b\s*,\s*\b(?:{right})\b\s*(?:,|\s+{_CONJUNCTIONS}\b)",
        rf"\b(?:{right})\b\s*,\s*\b(?:{left})\b\s*(?:,|\s+{_CONJUNCTIONS}\b)",
    ]
    return sum(len(re.findall(p, text, re.IGNORECASE)) for p in patterns)


# Ids the extractor numbers or positions because it could not name them:
# `dwarf_blacksmith_1` / `_2`, `driver_left` / `driver_right`. Two entries that
# differ only in such a marker are two anonymous people the book never named,
# never one person under two names -- and their names are too generic for the
# conjunction veto to see.
_POSITIONAL_SUFFIX = re.compile(r"[_-](\d+|left|right|first|second|third|a|b)$", re.IGNORECASE)


def _positional_siblings(left_id: str, right_id: str) -> bool:
    left_match, right_match = _POSITIONAL_SUFFIX.search(left_id), _POSITIONAL_SUFFIX.search(right_id)
    if not (left_match and right_match):
        return False
    if left_match.group(1).lower() == right_match.group(1).lower():
        return False
    return left_id[: left_match.start()] == right_id[: right_match.start()]


def merge_veto(
    primary_id: str,
    duplicate_id: str,
    characters: dict[str, Any],
    source_text: str,
) -> str | None:
    """Return why this merge must be refused, or None if it may proceed.

    Every check here is deterministic and local. A model proposal can only ever
    *survive* these; it can never override one.
    """
    if primary_id == duplicate_id:
        return "a character cannot be merged into itself"
    if "narrator" in (primary_id, duplicate_id):
        return "the narrator is never merged with a character"
    if primary_id not in characters or duplicate_id not in characters:
        return "one side of the pair is not in the registry"

    left, right = characters[primary_id], characters[duplicate_id]

    def gender_of(entry: Any) -> str:
        value = entry.get("gender") if isinstance(entry, dict) else getattr(entry, "gender", None)
        return str(getattr(value, "value", value) or "").lower()

    left_gender, right_gender = gender_of(left), gender_of(right)
    if {left_gender, right_gender} == {"male", "female"}:
        return f"explicit genders disagree ({left_gender} vs {right_gender})"

    if _positional_siblings(primary_id, duplicate_id):
        return "the ids differ only by a positional or numeric marker"

    def as_dict(entry: Any) -> dict[str, Any]:
        if isinstance(entry, dict):
            return entry
        return {
            "name": getattr(entry, "name", ""),
            "aliases": getattr(entry, "aliases", []) or [],
        }

    conjoined = conjunction_count(
        source_text,
        _vetoable_terms(as_dict(left), primary_id),
        _vetoable_terms(as_dict(right), duplicate_id),
    )
    if conjoined:
        return f"the source names them as separate participants {conjoined} time(s) (conjunction veto)"
    return None


def apply_merge(
    primary_id: str,
    duplicate_id: str,
    characters: dict[str, Any],
) -> dict[str, Any]:
    """Fold `duplicate_id` into `primary_id`, losing nothing.

    The duplicate's name and aliases become aliases of the survivor, and its
    dialogue count is added, so downstream importance and casting see one
    character with the combined weight rather than two halves.

    Returns a record of what happened, for the audit file.
    """
    primary = characters[primary_id]
    duplicate = characters.pop(duplicate_id)

    def get(entry: Any, field: str, default: Any) -> Any:
        return entry.get(field, default) if isinstance(entry, dict) else getattr(entry, field, default)

    def put(entry: Any, field: str, value: Any) -> None:
        if isinstance(entry, dict):
            entry[field] = value
        else:
            setattr(entry, field, value)

    absorbed = [str(get(duplicate, "name", duplicate_id)), duplicate_id.replace("_", " ")]
    absorbed += [str(a) for a in (get(duplicate, "aliases", []) or [])]

    aliases = list(get(primary, "aliases", []) or [])
    primary_name = str(get(primary, "name", primary_id))
    for alias in absorbed:
        alias = alias.strip()
        if alias and alias != primary_name and alias not in aliases:
            aliases.append(alias)
    put(primary, "aliases", aliases)

    combined = int(get(primary, "dialogue_count", 0) or 0) + int(get(duplicate, "dialogue_count", 0) or 0)
    put(primary, "dialogue_count", combined)

    # Keep the richer description rather than whichever happened to win.
    for field in ("voice_description", "speaking_style", "age_range"):
        current = str(get(primary, field, "") or "")
        other = str(get(duplicate, field, "") or "")
        if len(other) > len(current):
            put(primary, field, other)

    logger.info(
        "[CastIdentity] Merged '%s' into '%s'; combined dialogue_count=%d",
        duplicate_id,
        primary_id,
        combined,
    )
    return {
        "primary_id": primary_id,
        "merged_id": duplicate_id,
        "absorbed_aliases": absorbed,
        "combined_dialogue_count": combined,
    }


def choose_primary(
    left_id: str,
    right_id: str,
    characters: dict[str, Any],
) -> tuple[str, str]:
    """Decide which id survives a merge: the one with more dialogue.

    Ties break on the longer id, which is nearly always the fuller proper name
    ("thibbledorf_pwent" over "pwent"), then alphabetically so the choice is
    deterministic rather than dependent on dict ordering.
    """

    def weight(cid: str) -> tuple[int, int, str]:
        entry = characters.get(cid, {})
        count = entry.get("dialogue_count", 0) if isinstance(entry, dict) else getattr(entry, "dialogue_count", 0)
        return (int(count or 0), len(cid), cid)

    left, right = weight(left_id), weight(right_id)
    return (left_id, right_id) if left >= right else (right_id, left_id)


# ---------------------------------------------------------------------------
# Unlinked speaker recovery
# ---------------------------------------------------------------------------
#
# Measured on the real book: "Zak" appears 38 times and speaks repeatedly
# ("Zak said", "Zak explained", "Zak admitted"), but the registry holds
# Zaknafein with aliases ["the weapons master", "Zaknafein"] and no "Zak". It
# is not a registry entry, so identity adjudication never sees it; it is not
# lexically linkable to `zaknafein` either. Every one of those attributions is
# unresolvable at scripting time.
#
# That is a *missing alias on an existing character*, which is a different
# thing from a missing character, and it is the case this scan targets.

# Verbs that mark the preceding or following proper noun as a speaker.
_SPEECH_VERBS = (
    r"(?:said|asked|replied|whispered|shouted|murmured|answered|added|called|"
    r"snapped|growled|declared|explained|admitted|continued|insisted|muttered|"
    r"agreed|countered|offered|warned|repeated|breathed|laughed)"
)

# Capitalised words that begin sentences or stand in for names. Without this the
# scan reports "She said" and "You asked" as unknown speakers.
_NOT_A_NAME = {
    "he",
    "she",
    "it",
    "they",
    "we",
    "you",
    "i",
    "who",
    "what",
    "that",
    "this",
    "there",
    "then",
    "and",
    "but",
    "the",
    "one",
    "someone",
    "anyone",
    "nobody",
    "everyone",
    "all",
    "both",
    "each",
    "another",
    "others",
    "still",
    "yet",
    "when",
    "where",
    "why",
    "how",
    "his",
    "her",
    "their",
    "its",
    "our",
    "my",
}


def find_unlinked_speakers(
    text: str,
    characters: dict[str, Any],
    *,
    min_attributions: int = 2,
) -> dict[str, int]:
    """Names attributed with a speech verb that no registry entry answers to.

    Returns name -> attribution count, highest first.

    `min_attributions` defaults to 2 because a single hit is dominated by
    noise: on the real book a floor of 1 yields 16 candidates, most of them
    place names caught by the regex (Taulmaril is a bow, Kryptgarden a forest).
    The floor does not decide anything -- it only bounds how much gets sent for
    adjudication, and the model still has to ground every verdict.
    """
    if not text:
        return {}

    known: set[str] = set()
    for char_id, entry in characters.items():
        name = entry.get("name") if isinstance(entry, dict) else getattr(entry, "name", None)
        aliases = entry.get("aliases") if isinstance(entry, dict) else getattr(entry, "aliases", None)
        for term in [char_id.replace("_", " "), str(name or "")] + [str(a) for a in aliases or []]:
            for word in re.split(r"[\s_]+", term):
                cleaned = word.strip("'\".,;:-").casefold()
                if len(cleaned) >= 3:
                    known.add(cleaned)

    name_pattern = r"[A-Z][a-z'\-]{2,}"
    counts: Counter[str] = Counter()
    for pattern in (
        rf"\b({name_pattern})\s+{_SPEECH_VERBS}\b",
        rf"\b{_SPEECH_VERBS}\s+({name_pattern})\b",
    ):
        for match in re.finditer(pattern, text):
            candidate = match.group(1).strip()
            folded = candidate.casefold()
            if folded in _NOT_A_NAME or folded in known:
                continue
            counts[candidate] += 1

    return {name: count for name, count in counts.most_common() if count >= max(1, min_attributions)}


def alias_veto(
    alias: str,
    character_id: str,
    characters: dict[str, Any],
    text: str,
) -> str | None:
    """Return why this alias must not be attached, or None if it may be.

    Adding an alias is far safer than merging -- nothing is destroyed and no
    character disappears -- so the bar is lower than `merge_veto`. It is not
    absent, though: an alias silently redirects every future attribution of
    that name.
    """
    alias = (alias or "").strip()
    if len(alias) < 3:
        return "an alias must be at least three characters"
    if character_id not in characters:
        return "the target character is not in the registry"
    if character_id == "narrator":
        return "the narrator does not take aliases"

    folded = alias.casefold()
    if folded in _NOT_A_NAME:
        return "the alias is a pronoun or sentence opener, not a name"

    # The alias must actually be in the book. A model-invented name would
    # redirect attributions that can never occur, and points at a bad proposal.
    if not re.search(rf"\b{re.escape(alias)}\b", text, re.IGNORECASE):
        return "the alias does not appear in the source text"

    # It must not already belong to somebody else, or two characters would
    # answer to the same name and attribution could not choose between them.
    for other_id, entry in characters.items():
        if other_id == character_id:
            continue
        name = entry.get("name") if isinstance(entry, dict) else getattr(entry, "name", None)
        aliases = entry.get("aliases") if isinstance(entry, dict) else getattr(entry, "aliases", None)
        owned = {str(name or "").casefold(), other_id.replace("_", " ").casefold()}
        owned |= {str(a).casefold() for a in aliases or []}
        if folded in owned:
            return f"'{alias}' already belongs to '{other_id}'"

    return None


def apply_alias(alias: str, character_id: str, characters: dict[str, Any]) -> dict[str, Any]:
    """Attach a recovered alias to an existing character."""
    entry = characters[character_id]
    aliases = list((entry.get("aliases") if isinstance(entry, dict) else getattr(entry, "aliases", None)) or [])
    if alias not in aliases:
        aliases.append(alias)
    if isinstance(entry, dict):
        entry["aliases"] = aliases
    else:
        entry.aliases = aliases
    logger.info("[CastIdentity] Recovered alias '%s' for '%s'", alias, character_id)
    return {"character_id": character_id, "alias": alias}
