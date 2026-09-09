"""Deterministic book-local pronunciation inventory and synthesis helpers."""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Protocol

from shared.constants import (
    DEFAULT_OLLAMA_HOST,
    DEFAULT_OLLAMA_MODEL,
    GenerationCancelled,
)

logger = logging.getLogger(__name__)


def _repo_root() -> Path:
    """Resolve the repository root from this file, not the working directory."""
    return Path(__file__).resolve().parents[1]


_COMMON_SENTENCE_WORDS = {
    "After",
    "Again",
    "Ago",
    "All",
    "Always",
    "And",
    "As",
    "Because",
    "Before",
    "But",
    "Could",
    "Every",
    "Finally",
    "First",
    "For",
    "From",
    "Had",
    "Have",
    "Company",
    "Father",
    "He",
    "Her",
    "Here",
    "His",
    "How",
    "However",
    "If",
    "Instead",
    "It",
    "Its",
    "No",
    "Not",
    "Now",
    "One",
    "Only",
    "Or",
    "Perhaps",
    "She",
    "So",
    "Something",
    "That",
    "The",
    "Their",
    "Then",
    "There",
    "These",
    "They",
    "This",
    "Those",
    "Though",
    "Through",
    "Until",
    "Was",
    "We",
    "What",
    "Whatever",
    "When",
    "Where",
    "Whether",
    "Which",
    "While",
    "Uncle",
    "Who",
    "Why",
    "Will",
    "With",
    "Without",
    "Would",
    "Years",
    "Yes",
    "You",
    "Your",
    "I'd",
    "I'll",
    "I'm",
    "I've",
}
_CANDIDATE_PATTERN = re.compile(r"\b[A-Z][A-Za-z'’-]{2,}\b")

_ENGLISH_WORDS_CACHE: set[str] | None = None


def get_english_dictionary() -> set[str]:
    """Return the cached set of standard English dictionary words."""
    global _ENGLISH_WORDS_CACHE
    if _ENGLISH_WORDS_CACHE is not None:
        return _ENGLISH_WORDS_CACHE
    dict_gz = Path(__file__).resolve().parent / "data" / "english_words.txt.gz"
    if dict_gz.is_file():
        try:
            import gzip

            with gzip.open(dict_gz, "rt", encoding="utf-8") as f:
                _ENGLISH_WORDS_CACHE = {line.strip().lower() for line in f if line.strip()}
                return _ENGLISH_WORDS_CACHE
        except Exception as exc:
            logger.warning("Failed to load english_words.txt.gz: %s", exc)
    _ENGLISH_WORDS_CACHE = set()
    return _ENGLISH_WORDS_CACHE


def is_english_word(word: str) -> bool:
    """Return True if word exists in the standard English dictionary."""
    if not word:
        return False
    return word.strip().casefold() in get_english_dictionary()


def extract_concise_sentence(text: str, term: str = "", max_chars: int = 100) -> str:
    """Extract a concise single sentence around term (max_chars) for fast audio preview."""
    if not text:
        return ""
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= max_chars and "\n" not in cleaned:
        return cleaned

    # Split into individual sentences on punctuation boundaries
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    term_pat = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE) if term else None

    target_sentence = ""
    if term_pat:
        for s in sentences:
            if term_pat.search(s):
                target_sentence = s.strip()
                break

    if not target_sentence:
        target_sentence = sentences[0].strip() if sentences else cleaned

    if len(target_sentence) <= max_chars:
        return target_sentence

    # If sentence is still longer than max_chars, extract a centered window around term
    if term_pat:
        m = term_pat.search(target_sentence)
        if m:
            start_pos = m.start()
            end_pos = m.end()
            term_len = end_pos - start_pos
            half = max(10, (max_chars - term_len - 6) // 2)
            win_start = max(0, start_pos - half)
            win_end = min(len(target_sentence), end_pos + half)

            # Snap to word boundaries
            if win_start > 0:
                space_idx = target_sentence.find(" ", win_start)
                if space_idx != -1 and space_idx < start_pos:
                    win_start = space_idx + 1
            if win_end < len(target_sentence):
                space_idx = target_sentence.rfind(" ", start_pos, win_end)
                if space_idx != -1 and space_idx > end_pos:
                    win_end = space_idx

            snippet = target_sentence[win_start:win_end].strip()
            if win_start > 0:
                snippet = "..." + snippet
            if win_end < len(target_sentence):
                snippet = snippet + "..."
            return snippet

    return target_sentence[:max_chars].rstrip() + "..."


def _is_heading_or_title(text: str) -> bool:
    """Return True if text appears to be a chapter title or title-cased heading."""
    words = re.findall(r"\b[A-Za-z]+\b", text)
    if not words:
        return False
    if len(words) <= 10 and sum(1 for w in words if w[0].isupper()) / len(words) >= 0.7:
        return True
    if re.match(r"^(?:chapter|part|prologue|epilogue|book|act)\b", text.strip(), re.IGNORECASE):
        return True
    return False


def _is_sentence_initial(text: str, start: int) -> bool:
    """Return true when a token only has sentence punctuation/quotes before it."""
    prefix = text[:start].rstrip()
    while prefix and prefix[-1] in "\"'“”‘’([{":
        prefix = prefix[:-1].rstrip()
    return not prefix or prefix[-1] in ".!?"


def _validate_entries(payload: Any, source: Path) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise TypeError(f"Pronunciation dictionary must be an object: {source}")
    result: dict[str, str] = {}
    for word, replacement in payload.items():
        if not isinstance(word, str) or not isinstance(replacement, str):
            raise TypeError("Pronunciation entries must map text to text")
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
    include_defaults: bool = True,
) -> tuple[dict[str, str], dict[str, str]]:
    """Load validated mappings and their source, with project entries winning.

    When `include_defaults=True`, default recommendations from
    `project_dir / "pronunciation_recommendations.json"` are loaded as base mappings.
    Global dictionary overrides defaults, and project dictionary overrides both.
    """
    global_path = global_path or _repo_root() / "brain" / "pronunciation_dict.json"
    mappings: dict[str, tuple[str, str, str]] = {}

    if include_defaults:
        recs_path = project_dir / "pronunciation_recommendations.json"
        if recs_path.exists():
            try:
                raw_recs = json.loads(recs_path.read_text(encoding="utf-8"))
                if isinstance(raw_recs, dict):
                    for word, rec_val in raw_recs.items():
                        if not isinstance(word, str) or not word.strip():
                            continue
                        word_clean = word.strip()
                        default_spoken = ""
                        if isinstance(rec_val, dict):
                            default_spoken = str(rec_val.get("default", "")).strip()
                        elif isinstance(rec_val, str):
                            default_spoken = rec_val.strip()
                        if default_spoken and default_spoken.casefold() != word_clean.casefold():
                            mappings[word_clean.casefold()] = (word_clean, default_spoken, "default")
            except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
                logger.debug("Could not read recommendations for defaults in %s: %s", project_dir, exc)

    for source_name, path in (
        ("global", global_path),
        ("project", project_dir / "pronunciation_dict.json"),
    ):
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"Invalid pronunciation dictionary: {path}") from exc
        for word, replacement in _validate_entries(raw, path).items():
            mappings[word.casefold()] = (word, replacement, source_name)

    if include_defaults:
        active_mappings = {
            word: replacement
            for word, replacement, _ in mappings.values()
            if replacement and replacement.casefold() != word.casefold()
        }
    else:
        active_mappings = {
            word: replacement
            for word, replacement, _ in mappings.values()
            if replacement
        }
    return (
        active_mappings,
        {word: source for word, _, source in mappings.values()},
    )


def normalize_phonetic_text(text: str) -> str:
    """Normalize phonetic respelling text for TTS while preserving hyphens and compounds.

    Does NOT replace hyphens with spaces to prevent neural TTS from inserting
    word-boundary pauses.
    """
    if not text:
        return text
    # Strip enclosing quotes while keeping internal hyphens and apostrophes
    cleaned = text.strip().strip("\"'“”‘’")
    return re.sub(r"\s+", " ", cleaned).strip()


_COMPOUND_BASES = {
    "home": "Home",
    "isle": "aisle",
    "isles": "aisles",
    "isler": "eye ler",
    "islers": "eye lers",
    "night": "Night",
    "blood": "blood",
    "storm": "Storm",
    "light": "light",
    "high": "High",
    "sun": "Sun",
    "maker": "maker",
    "rock": "Rock",
    "bud": "bud",
    "shard": "Shard",
    "blade": "blade",
    "plate": "plate",
    "truth": "Truth",
    "watcher": "watcher",
    "wind": "Wind",
    "runner": "runner",
    "sky": "Sky",
    "breaker": "breaker",
    "soul": "Soul",
    "caster": "caster",
    "mist": "Mist",
    "born": "born",
    "fire": "Fire",
    "heart": "heart",
    "dragon": "Dragon",
    "rider": "rider",
    "shadow": "Shadow",
    "dancer": "dancer",
    "chasm": "Chasm",
    "fiend": "fiend",
    "void": "Void",
    "bringer": "bringer",
    "bringers": "bringers",
    "oath": "Oath",
    "gate": "gate",
    "keeper": "keeper",
    "singer": "singer",
    "singers": "singers",
    "world": "World",
    "hopper": "hopper",
    "sing": "Sing",
    "ash": "Ash",
    "fell": "Fell",
    "haven": "haven",
    "stone": "Stone",
    "wood": "Wood",
    "bright": "Bright",
    "lord": "lord",
    "lady": "lady",
}

_KNOWN_TERM_OVERRIDES = {
    "kokerlii": ("Cokerlee", "Koh-ker-lee"),
    "pache": ("Pahchee", "Paych"),
    "szeth": ("Seth", "Zeth"),
    "jasnah": ("Yasnah", "Jaznah"),
    "sadeas": ("Sahdeeus", "Saydeeus"),
    "kaladin": ("Caladin", "Kalladin"),
    "shallan": ("Shahlan", "Shalan"),
    "adolin": ("Aydolin", "Ahdolin"),
    "navani": ("Nahvahnee", "Navahnee"),
    "renarin": ("Rehnarin", "Renarin"),
    "dalinar": ("Dahlinar", "Dalinar"),
    "taravangian": ("Taravanjian", "Tah-rah-van-gee-an"),
    "kharbranth": ("Karbranth", "Kahrbranth"),
    "alethi": ("Ahlethee", "Uhlethee"),
    "parshendi": ("Parshendee", "Parsh-en-dee"),
    "parshman": ("Parshman", "Parsh-man"),
    "parshmen": ("Parshmen", "Parsh-men"),
}


def _split_compound(word: str) -> list[str] | None:
    w = word.lower()
    for i in range(3, len(w) - 2):
        left, right = w[:i], w[i:]
        if left in _COMPOUND_BASES and right in _COMPOUND_BASES:
            return [_COMPOUND_BASES[left], _COMPOUND_BASES[right]]
    return None


def _split_into_phonetic_chunks(word: str) -> list[str]:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", word)
    spaced = re.sub(r"[_\-]+", " ", spaced)
    chunks: list[str] = []
    vowel_pat = re.compile(
        r"([bcdfghjklmnpqrstvwxzBCDFGHJKLMNPQRSTVWXZ]*[aeiouyAEIOUY]+(?:[bcdfghjklmnpqrstvwxzBCDFGHJKLMNPQRSTVWXZ]+(?![aeiouyAEIOUY]))?)"
    )
    for token in spaced.split():
        matches = [m.group(0) for m in vowel_pat.finditer(token)]
        if matches and "".join(matches).lower() == token.lower():
            chunks.extend(matches)
        else:
            chunks.append(token)
    return chunks


def generate_phonetic_recommendations(term: str, context: str = "") -> dict[str, str]:
    """Generate 1 default and 1 alternate TTS-friendly phonetic respelling."""
    raw = term.strip()
    if not raw:
        return {"default": "", "alternate": ""}
    key = raw.lower()
    if key in _KNOWN_TERM_OVERRIDES:
        d, a = _KNOWN_TERM_OVERRIDES[key]
        return {"default": d, "alternate": a}

    # Multi-word terms (whitespace separated): process each word independently to preserve word separation
    if re.search(r"\s+", raw):
        words = raw.split()
        sub_recs = [generate_phonetic_recommendations(w, context) for w in words]
        rec_def = " ".join(r["default"] for r in sub_recs)
        rec_alt = " ".join(r["alternate"] for r in sub_recs)
        if rec_def.lower() == rec_alt.lower() or not rec_alt:
            rec_alt = raw
        return {"default": rec_def, "alternate": rec_alt}

    # Hyphenated terms: process parts independently to preserve hyphen boundaries
    if "-" in raw:
        parts = [p for p in raw.split("-") if p]
        if len(parts) > 1:
            sub_recs = [generate_phonetic_recommendations(p, context) for p in parts]
            rec_def = "-".join(r["default"] for r in sub_recs)
            rec_alt = "-".join(r["alternate"] for r in sub_recs)
            if rec_def.lower() == rec_alt.lower() or not rec_alt:
                rec_alt = raw
            return {"default": rec_def, "alternate": rec_alt}

    comp = _split_compound(raw)
    if comp:
        rec_def = "".join(comp)
        alt_parts: list[str] = []
        for p in comp:
            low = p.lower()
            if low == "aisle":
                alt_parts.append("aisle")
            elif low == "eye ler":
                alt_parts.append("aisler")
            elif low == "eye lers":
                alt_parts.append("aislers")
            elif low == "night":
                alt_parts.append("Nite")
            elif low == "storm":
                alt_parts.append("Stawm")
            elif low == "light":
                alt_parts.append("Lite")
            else:
                alt_parts.append(p)
        rec_alt = "-".join(comp) if alt_parts == comp else "".join(alt_parts)
        if rec_alt.lower() == rec_def.lower():
            rec_alt = "-".join(comp)
        return {"default": rec_def, "alternate": rec_alt}

    clean_def = raw
    clean_alt = raw
    if re.search(r"lii$", clean_def, re.I):
        clean_def = re.sub(r"lii$", "lee", clean_def, flags=re.I)
        clean_alt = re.sub(r"lii$", "-lee", clean_alt, flags=re.I)
    elif re.search(r"ii$", clean_def, re.I):
        clean_def = re.sub(r"ii$", "ee", clean_def, flags=re.I)
        clean_alt = re.sub(r"ii$", "-ee", clean_alt, flags=re.I)

    if re.match(r"^Sz", clean_def, re.I):
        clean_def = re.sub(r"^Sz", "S", clean_def, flags=re.I)
        clean_alt = re.sub(r"^Sz", "Z", clean_alt, flags=re.I)
    elif re.match(r"^Kh", clean_def, re.I):
        clean_def = re.sub(r"^Kh", "K", clean_def, flags=re.I)
    elif re.match(r"^J[aeiou]", clean_def, re.I):
        clean_def = "Y" + clean_def[1:]

    sylls_def = _split_into_phonetic_chunks(clean_def)
    sylls_alt = _split_into_phonetic_chunks(clean_alt)

    def format_sylls(sylls: list[str], alt: bool = False) -> str:
        parts: list[str] = []
        for s in sylls:
            low = s.lower()
            if low == "pa":
                parts.append("Pah" if not alt else "Pay")
            elif low == "ka":
                parts.append("Cah" if not alt else "Kah")
            elif low == "sha":
                parts.append("Shah" if not alt else "Sha")
            elif low == "che" and len(sylls) > 1:
                parts.append("chee" if not alt else "ch")
            elif low == "jas":
                parts.append("Jaz" if alt else "Yas")
            elif low == "nah":
                parts.append("nah")
            else:
                parts.append(s.capitalize() if not parts else s.lower())
        if not alt and parts:
            return parts[0].capitalize() + "".join(p.lower() for p in parts[1:])
        return "-".join(parts)

    rec_def = format_sylls(sylls_def, alt=False)
    rec_alt = format_sylls(sylls_alt, alt=True)

    if rec_def.lower() == rec_alt.lower() or not rec_alt:
        rec_alt = "-".join(sylls_def) if len(sylls_def) > 1 else raw

    return {"default": rec_def, "alternate": rec_alt}


def apply_pronunciations(text: str, mappings: dict[str, str]) -> str:
    """Apply longest-first replacements once, never recursively."""
    folded = {word.casefold(): (word, normalize_phonetic_text(replacement)) for word, replacement in mappings.items()}
    ordered = sorted(folded.values(), key=lambda item: (-len(item[0]), item[0].casefold()))
    if not ordered:
        return text
    pattern = re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(word) for word, _ in ordered) + r")(?!\w)",
        re.IGNORECASE,
    )
    return pattern.sub(lambda match: folded[match.group(0).casefold()][1], text)


class PronunciationLLM(Protocol):
    """Minimal interface this module needs from an LLM client.

    Satisfied by ``brain.director.ollama_client.OllamaClient``. Declaring it as
    a Protocol keeps ``shared`` free of a dependency on ``brain`` while letting
    callers inject the real client, which brings retry budgets, repetition-loop
    detection, output-limit enforcement and -- most importantly -- cooperative
    cancellation, so a user pause actually interrupts this work.
    """

    def generate(
        self,
        prompt: str,
        *,
        temperature: float = ...,
        top_p: float = ...,
        system: str | None = ...,
        format: str | None = ...,
    ) -> str: ...


def _get_configured_ollama() -> tuple[str, str]:
    """Retrieve configured Ollama host and model from brain/config.yaml.

    Only used on the fallback path when no client is injected. The config path
    is resolved from this file's location rather than the process working
    directory, because a dashboard started from another directory would
    otherwise silently fall back to the defaults below and could resolve
    pronunciations on a different model than the one scripting the book.
    """
    cfg_path = _repo_root() / "brain" / "config.yaml"
    host = DEFAULT_OLLAMA_HOST
    model = DEFAULT_OLLAMA_MODEL
    if cfg_path.is_file():
        try:
            import yaml

            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            ollama_cfg = cfg.get("ollama", {})
            host = str(ollama_cfg.get("host") or host)
            model = str(ollama_cfg.get("model") or model)
        except (ImportError, OSError, ValueError) as exc:
            logger.warning(
                "Could not read %s for pronunciation model selection; falling back to %s: %s",
                cfg_path,
                model,
                exc,
            )
    return host, model


_PRONUNCIATION_PROMPT_HEADER = (
    "You are an expert fantasy and fiction pronunciation director for audiobooks.\n"
    "For each candidate proper noun or out-of-vocabulary term and its book context, provide the exact spoken phonetic respelling for a Neural TTS engine.\n"
    "Rules:\n"
    "1. For single-word terms, write phonetic respellings as fluid single words or natural English syllables without spaces between syllables (e.g. 'Kaludin', 'Zeth', 'Taravanjian', 'Homeaisle', 'Shalan'). Do NOT put spaces between syllables of a single word.\n"
    "2. For multi-word terms or names (e.g. 'Braelin Janquay', 'Uncle Jax', 'Ghaliver Longstocking'), ALWAYS preserve the spaces between separate words. Never concatenate separate words or names into a single word (e.g. write 'Braelin Yanquay', NEVER 'BraelinJanquay').\n"
    "3. For hyphenated terms (e.g. 'Ten-Towns', 'Caer-Konig'), preserve the hyphen or use spaces between distinct words; do NOT concatenate them into a single squashed word.\n"
    "4. Provide 1 default respelling and 1 alternate valid respelling.\n"
    '5. Output STRICT JSON with key \'recommendations\': [{"term": "...", "default": "...", "alternate": "..."}]\n\n'
    "CANDIDATES:\n"
)


def _pronunciation_prompt(items: list[tuple[str, str]]) -> str:
    return _PRONUNCIATION_PROMPT_HEADER + json.dumps(
        [{"term": t, "context": c[:200]} for t, c in items],
        ensure_ascii=False,
        indent=2,
    )


def _clean_rec(term: str, rec: str) -> str:
    cleaned = normalize_phonetic_text(rec)
    if not cleaned:
        return ""
    # Guard against LLM concatenating words when term had spaces
    if " " in term and " " not in cleaned:
        fb = generate_phonetic_recommendations(term)
        return fb.get("default", cleaned)
    # Guard against LLM concatenating words when term had hyphens
    if "-" in term and "-" not in cleaned and " " not in cleaned:
        fb = generate_phonetic_recommendations(term)
        return fb.get("default", cleaned)
    return cleaned


def _parse_pronunciation_response(raw_text: str) -> dict[str, dict[str, str]]:
    """Extract normalized recommendations from a strict-JSON model response."""
    parsed = json.loads(raw_text)
    result: dict[str, dict[str, str]] = {}
    for record in parsed.get("recommendations", []):
        raw_term = str(record.get("term", "")).strip()
        term = raw_term.casefold()
        default = _clean_rec(raw_term, str(record.get("default", "")))
        alternate = _clean_rec(raw_term, str(record.get("alternate", "")))
        if term and default:
            result[term] = {"default": default, "alternate": alternate or default}
    return result


def resolve_pronunciations_with_llm(
    items: list[tuple[str, str]],
    ollama_host: str | None = None,
    model: str | None = None,
    timeout_s: float = 12.0,
    client: PronunciationLLM | None = None,
) -> dict[str, dict[str, str]]:
    """Batch-resolve TTS-ready phonetic respellings via the local LLM.

    Prefer passing ``client`` (the pipeline's own ``OllamaClient``). That path
    inherits the retry budget, repetition-loop detection, output-token cap and
    cooperative cancellation, so a user pause interrupts pronunciation
    resolution instead of leaving it running against the GPU.

    The direct-HTTP fallback exists only for callers that have no client to
    hand (standalone dashboard requests). It has none of those protections.
    """
    if not items:
        return {}

    if client is not None:
        try:
            raw_text = client.generate(
                _pronunciation_prompt(items),
                temperature=0.2,
                top_p=0.9,
                format="json",
            )
            return _parse_pronunciation_response(raw_text)
        except (GenerationCancelled, KeyboardInterrupt):
            # Cooperative cancellation from `OllamaClient`; propagate so the
            # pipeline parks instead of silently continuing.
            raise
        except Exception as exc:
            logger.debug("Injected LLM pronunciation resolution failed: %s", exc)
            return {}

    default_host, default_model = _get_configured_ollama()
    target_host = ollama_host or default_host
    target_model = model or default_model

    import urllib.request

    prompt = _pronunciation_prompt(items)

    req_body = json.dumps(
        {
            "model": target_model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "think": False,
            "options": {
                "temperature": 0.2,
                "top_p": 0.9,
                "num_predict": 2048,
            },
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        f"{target_host.rstrip('/')}/api/generate",
        data=req_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw_text = data.get("response", "")
            parsed = json.loads(raw_text)
            recs = parsed.get("recommendations", [])
            result: dict[str, dict[str, str]] = {}
            for r in recs:
                raw_term = str(r.get("term", "")).strip()
                term = raw_term.casefold()
                d = _clean_rec(raw_term, str(r.get("default", "")))
                a = _clean_rec(raw_term, str(r.get("alternate", "")))
                if term and d:
                    result[term] = {"default": d, "alternate": a or d}
            return result
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.debug("Ollama pronunciation resolution unavailable: %s", exc)
        return {}


from shared.cache import cache_service


def build_pronunciation_inventory(
    project_dir: Path,
    use_llm: bool = True,
    client: PronunciationLLM | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Inventory verified mappings and repeated unresolved book terms with recommendations.

    Pass ``client`` (the pipeline's ``OllamaClient``) wherever one is available
    so LLM recommendation lookups are cancellable and share the pipeline's
    retry and safety limits.
    """
    script_path = project_dir / "book_script.json"
    if not script_path.exists():
        return {"schema": 1, "verified": 0, "unresolved": 0, "candidates": []}

    dict_path = project_dir / "pronunciation_dict.json"
    global_dict = _repo_root() / "brain" / "pronunciation_dict.json"
    chars_path = project_dir / "characters.json"
    inv_path = project_dir / "pronunciation_inventory.json"

    # Compute modification signature
    mtimes = [script_path.stat().st_mtime]
    if dict_path.is_file():
        mtimes.append(dict_path.stat().st_mtime)
    if global_dict.is_file():
        mtimes.append(global_dict.stat().st_mtime)
    if chars_path.is_file():
        mtimes.append(chars_path.stat().st_mtime)
    current_sig = max(mtimes)

    cache_key = f"pronunciation_inv:{project_dir.resolve()}"
    if not force:
        cached = cache_service.get(cache_key)
        if cached and isinstance(cached, dict) and cached.get("sig") == current_sig:
            return cached.get("data", {})

        if inv_path.is_file():
            try:
                if inv_path.stat().st_mtime >= current_sig:
                    data = json.loads(inv_path.read_text(encoding="utf-8"))
                    cache_service.set(cache_key, {"sig": current_sig, "data": data}, ttl_seconds=1800)
                    return data
            except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
                logger.warning(
                    "Could not read the cached inventory %s; it will be rebuilt from the scripts: %s", inv_path, exc
                )

    payload = json.loads(script_path.read_text(encoding="utf-8"))
    mappings, mapping_sources = load_pronunciation_dictionary(project_dir, include_defaults=False)
    mapping_by_folded = {word.casefold(): (word, replacement) for word, replacement in mappings.items()}
    source_by_folded = {word.casefold(): source for word, source in mapping_sources.items()}

    # Load cached recommendations if present
    recs_path = project_dir / "pronunciation_recommendations.json"
    cached_recs: dict[str, dict[str, str]] = {}
    if recs_path.is_file():
        try:
            cached_recs = json.loads(recs_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError):
            cached_recs = {}

    character_names: set[str] = set()
    characters_path = project_dir / "characters.json"
    if characters_path.exists():
        characters = json.loads(characters_path.read_text(encoding="utf-8")).get("characters", {})
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
    english_words = get_english_dictionary()

    def record(term: str, chapter_number: int, text: str) -> None:
        key = term.casefold()
        counts[key] += 1
        display.setdefault(key, term)
        chapters[key].add(chapter_number)
        if len(contexts[key]) < 3:
            contexts[key].append(extract_concise_sentence(text, term, max_chars=100))

    for chapter_index, chapter in enumerate(payload.get("chapters", []), 1):
        chapter_number = int(chapter.get("chapter_number") or chapter_index)
        for line in chapter.get("lines", chapter.get("utterances", [])):
            text = line.get("text") if isinstance(line, dict) else None
            if not isinstance(text, str):
                continue
            is_heading = _is_heading_or_title(text)
            occupied: list[tuple[int, int]] = []
            for alias in sorted(multiword_aliases, key=lambda value: (-len(value), value)):
                for alias_match in re.finditer(rf"(?<!\w){re.escape(alias)}(?!\w)", text, re.IGNORECASE):
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
                if not is_heading and not _is_sentence_initial(text, match.start()):
                    mid_sentence.add(key)

    all_keys = set(counts) | set(mapping_by_folded)
    valid_keys = []
    for key in all_keys:
        verified = key in mapping_by_folded
        occurrence_count = counts.get(key, 0)
        # Skip standard English dictionary words unless explicitly verified or in character cast
        if not verified and key not in character_aliases and key in english_words:
            continue
        if not verified and occurrence_count < 2 and key not in character_aliases:
            continue
        if not verified and key not in character_aliases and key not in mid_sentence:
            continue
        valid_keys.append(key)

    # Batch-resolve missing terms with LLM if enabled
    recs_updated = False
    if use_llm and client:
        unresolved_keys = [k for k in valid_keys if k not in mapping_by_folded and k not in cached_recs]
        if unresolved_keys:
            items_to_query = [{"term": display.get(k, k), "context": contexts.get(k, [""])[0]} for k in unresolved_keys]
            # Assumes usage of batch LLM helper
            llm_results = resolve_pronunciations_with_llm(
                [(item["term"], item["context"]) for item in items_to_query],
                client=client,
            )
            for k in unresolved_keys:
                t = display.get(k, k).casefold()
                if t in llm_results:
                    cached_recs[k] = llm_results[t]
                    recs_updated = True

    candidates: list[dict[str, Any]] = []
    for key in valid_keys:
        verified = key in mapping_by_folded
        occurrence_count = counts.get(key, 0)
        mapped_word, replacement = mapping_by_folded.get(key, (display.get(key, key), None))
        display_term = display.get(key, mapped_word)

        # Get or generate recommendations
        rec_default = ""
        rec_alternate = ""
        if not verified:
            if key in cached_recs:
                rec_default = cached_recs[key].get("default", "")
                rec_alternate = cached_recs[key].get("alternate", "")
                # Auto-repair cached squashed recommendations (e.g. BraelinJanquay -> Braelin Yanquay)
                is_squashed_space = (" " in display_term and " " not in rec_default)
                is_squashed_hyphen = ("-" in display_term and "-" not in rec_default and " " not in rec_default)
                if is_squashed_space or is_squashed_hyphen:
                    rec_default = ""
                    rec_alternate = ""
            if not rec_default:
                ctx = contexts.get(key, [""])[0]
                generated = generate_phonetic_recommendations(display_term, ctx)
                rec_default = generated.get("default", "")
                rec_alternate = generated.get("alternate", "")
                cached_recs[key] = {"default": rec_default, "alternate": rec_alternate}
                recs_updated = True

        effective = replacement if verified else rec_default
        candidates.append(
            {
                "term": display_term,
                "status": "verified" if verified else "review_required",
                "spoken_text": replacement,
                "recommendation_default": rec_default,
                "recommendation_alternate": rec_alternate,
                "effective_spoken": effective or "",
                "mapping_source": source_by_folded.get(key) or ("default" if rec_default else None),
                "occurrences": occurrence_count,
                "chapters": sorted(chapters.get(key, set())),
                "contexts": contexts.get(key, []),
            }
        )

    if recs_updated:
        try:
            recs_path.write_text(json.dumps(cached_recs, indent=2), encoding="utf-8")
        except (OSError, ValueError, TypeError) as exc:
            logger.warning(
                "Could not write %s; recorded pronunciations will be recomputed next run: %s", recs_path, exc
            )

    candidates.sort(
        key=lambda item: (
            item["status"] != "review_required",
            -item["occurrences"],
            item["term"].casefold(),
        )
    )
    result = {
        "schema": 1,
        "verified": sum(item["status"] == "verified" for item in candidates),
        "unresolved": sum(item["status"] == "review_required" for item in candidates),
        "candidates": candidates,
    }

    try:
        from shared.artifacts import atomic_write_json

        atomic_write_json(inv_path, result)
    except (OSError, ValueError, TypeError) as exc:
        logger.warning(
            "Could not write the pronunciation inventory %s; the dashboard will show none: %s", inv_path, exc
        )

    cache_service.set(cache_key, {"sig": current_sig, "data": result}, ttl_seconds=1800)
    return result
