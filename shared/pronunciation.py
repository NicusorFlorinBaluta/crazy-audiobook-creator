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
) -> tuple[dict[str, str], dict[str, str]]:
    """Load validated mappings and their source, with project entries winning."""
    global_path = global_path or _repo_root() / "brain" / "pronunciation_dict.json"
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


def normalize_phonetic_text(text: str) -> str:
    """Normalize hyphenated inter-syllable respellings to spaces for fluid TTS.

    E.g. 'Koh-ker-lee' -> 'Koh ker lee', 'home-aisle' -> 'home aisle', 'Pah-chee' -> 'Pah chee'
    while preserving non-hyphenated words.
    """
    if not text:
        return text
    # Convert hyphens connecting alphanumeric characters into natural spaces
    cleaned = re.sub(r"(?<=[A-Za-z0-9])-(?=[A-Za-z0-9])", " ", text)
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
    "kokerlii": ("Coker lee", "Koh ker lee"),
    "pache": ("Pah chee", "Paych"),
    "szeth": ("Seth", "Zeth"),
    "jasnah": ("Yas nah", "Jaz nah"),
    "sadeas": ("Sah dee us", "Say dee us"),
    "kaladin": ("Cal a din", "Kah lah din"),
    "shallan": ("Shah lan", "Sha lahn"),
    "adolin": ("Ay do lin", "Ah do lin"),
    "navani": ("Nah vah nee", "Na vah nee"),
    "renarin": ("Reh na rin", "Ren a rin"),
    "dalinar": ("Dah li nar", "Dal i nar"),
    "taravangian": ("Tah rah vahn jee an", "Ta ra van gian"),
    "kharbranth": ("Kar branth", "Kahr branth"),
    "alethi": ("Ah leth ee", "Uh leth ee"),
    "parshendi": ("Par shen dee", "Parsh en dee"),
    "parshman": ("Parsh man", "Parsh mun"),
    "parshmen": ("Parsh men", "Parsh min"),
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

    comp = _split_compound(raw)
    if comp:
        rec_def = " ".join(comp)
        alt_parts: list[str] = []
        for p in comp:
            low = p.lower()
            if low == "aisle":
                alt_parts.append("eye ull")
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
        rec_alt = " ".join(alt_parts) if alt_parts != comp else (comp[0] + " " + comp[1].capitalize())
        return {"default": rec_def, "alternate": rec_alt}

    clean_def = raw
    clean_alt = raw
    if re.search(r"lii$", clean_def, re.I):
        clean_def = re.sub(r"lii$", " lee", clean_def, flags=re.I)
        clean_alt = re.sub(r"lii$", "lee", clean_alt, flags=re.I)
    elif re.search(r"ii$", clean_def, re.I):
        clean_def = re.sub(r"ii$", " ee", clean_def, flags=re.I)
        clean_alt = re.sub(r"ii$", "ee", clean_alt, flags=re.I)

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
        return " ".join(parts)

    rec_def = format_sylls(sylls_def, alt=False)
    rec_alt = format_sylls(sylls_alt, alt=True)

    if rec_def.lower() == rec_alt.lower() or not rec_alt:
        rec_alt = raw

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
        except Exception as exc:
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
    "1. Write phonetic respellings in plain English syllables (e.g. 'KALL-uh-din', 'Zeth', 'tah-rah-VAN-jee-an', 'shah-LAHN').\n"
    "2. Capitalize the stressed syllable.\n"
    "3. Provide 1 default respelling and 1 alternate valid respelling.\n"
    '4. Output STRICT JSON with key \'recommendations\': [{"term": "...", "default": "...", "alternate": "..."}]\n\n'
    "CANDIDATES:\n"
)


def _pronunciation_prompt(items: list[tuple[str, str]]) -> str:
    return _PRONUNCIATION_PROMPT_HEADER + json.dumps(
        [{"term": t, "context": c[:200]} for t, c in items],
        ensure_ascii=False,
        indent=2,
    )


def _parse_pronunciation_response(raw_text: str) -> dict[str, dict[str, str]]:
    """Extract normalized recommendations from a strict-JSON model response."""
    parsed = json.loads(raw_text)
    result: dict[str, dict[str, str]] = {}
    for record in parsed.get("recommendations", []):
        term = str(record.get("term", "")).strip().casefold()
        default = normalize_phonetic_text(str(record.get("default", "")))
        alternate = normalize_phonetic_text(str(record.get("alternate", "")))
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

    prompt = (
        "You are an expert fantasy and fiction pronunciation director for audiobooks.\n"
        "For each candidate proper noun or out-of-vocabulary term and its book context, provide the exact spoken phonetic respelling for a Neural TTS engine.\n"
        "Rules:\n"
        "1. Write phonetic respellings in plain English syllables (e.g. 'KALL-uh-din', 'Zeth', 'tah-rah-VAN-jee-an', 'shah-LAHN').\n"
        "2. Capitalize the stressed syllable.\n"
        "3. Provide 1 default respelling and 1 alternate valid respelling.\n"
        '4. Output STRICT JSON with key \'recommendations\': [{"term": "...", "default": "...", "alternate": "..."}]\n\n'
        "CANDIDATES:\n" + json.dumps([{"term": t, "context": c[:200]} for t, c in items], ensure_ascii=False, indent=2)
    )

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
                term = str(r.get("term", "")).strip().casefold()
                d = normalize_phonetic_text(str(r.get("default", "")))
                a = normalize_phonetic_text(str(r.get("alternate", "")))
                if term and d:
                    result[term] = {"default": d, "alternate": a or d}
            return result
    except Exception as exc:
        logger.debug("Ollama pronunciation resolution unavailable: %s", exc)
        return {}


from shared.cache import cache_service


def build_pronunciation_inventory(
    project_dir: Path,
    use_llm: bool = True,
    client: PronunciationLLM | None = None,
) -> dict[str, Any]:
    """Inventory verified mappings and repeated unresolved book terms with recommendations.

    Pass ``client`` (the pipeline's ``OllamaClient``) wherever one is available
    so LLM recommendation lookups are cancellable and share the pipeline's
    retry and safety limits.
    """
    script_path = project_dir / "book_script.json"
    if not script_path.exists():
        return {"schema": 1, "verified": 0, "unresolved": 0, "candidates": []}

    dict_path = project_dir / "pronunciation_dictionary.json"
    chars_path = project_dir / "characters.json"
    inv_path = project_dir / "pronunciation_inventory.json"

    # Compute modification signature
    mtimes = [script_path.stat().st_mtime]
    if dict_path.is_file():
        mtimes.append(dict_path.stat().st_mtime)
    if chars_path.is_file():
        mtimes.append(chars_path.stat().st_mtime)
    current_sig = max(mtimes)

    cache_key = f"pronunciation_inv:{project_dir.resolve()}"
    cached = cache_service.get(cache_key)
    if cached and isinstance(cached, dict) and cached.get("sig") == current_sig:
        return cached.get("data", {})

    if inv_path.is_file():
        try:
            if inv_path.stat().st_mtime >= current_sig:
                data = json.loads(inv_path.read_text(encoding="utf-8"))
                cache_service.set(cache_key, {"sig": current_sig, "data": data}, ttl_seconds=1800)
                return data
        except Exception as exc:
            logger.warning(
                "Could not read the cached inventory %s; it will be rebuilt from the scripts: %s", inv_path, exc
            )

    payload = json.loads(script_path.read_text(encoding="utf-8"))
    mappings, mapping_sources = load_pronunciation_dictionary(project_dir)
    mapping_by_folded = {word.casefold(): (word, replacement) for word, replacement in mappings.items()}
    source_by_folded = {word.casefold(): source for word, source in mapping_sources.items()}

    # Load cached recommendations if present
    recs_path = project_dir / "pronunciation_recommendations.json"
    cached_recs: dict[str, dict[str, str]] = {}
    if recs_path.is_file():
        try:
            cached_recs = json.loads(recs_path.read_text(encoding="utf-8"))
        except Exception:
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
                if not _is_sentence_initial(text, match.start()):
                    mid_sentence.add(key)

    all_keys = set(counts) | set(mapping_by_folded)
    valid_keys = []
    for key in all_keys:
        verified = key in mapping_by_folded
        occurrence_count = counts.get(key, 0)
        if not verified and occurrence_count < 2 and key not in character_aliases:
            continue
        if not verified and key not in character_aliases and key not in mid_sentence:
            continue
        valid_keys.append(key)

    # Batch-resolve missing terms with LLM if enabled
    recs_updated = False
    if use_llm:
        missing_llm_items: list[tuple[str, str]] = []
        for key in valid_keys:
            if key not in mapping_by_folded and key not in cached_recs:
                mapped_word = mapping_by_folded.get(key, (display.get(key, key), None))[0]
                d_term = display.get(key, mapped_word)
                ctx = contexts.get(key, [""])[0]
                missing_llm_items.append((d_term, ctx))
        if missing_llm_items:
            llm_results = resolve_pronunciations_with_llm(
                missing_llm_items,
                client=client,
            )
            for raw_k, rec_data in llm_results.items():
                cached_recs[raw_k.casefold()] = rec_data
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
            if not rec_default:
                ctx = contexts.get(key, [""])[0]
                generated = generate_phonetic_recommendations(display_term, ctx)
                rec_default = generated.get("default", "")
                rec_alternate = generated.get("alternate", "")
                cached_recs[key] = {"default": rec_default, "alternate": rec_alternate}
                recs_updated = True

        candidates.append(
            {
                "term": display_term,
                "status": "verified" if verified else "review_required",
                "spoken_text": replacement,
                "recommendation_default": rec_default,
                "recommendation_alternate": rec_alternate,
                "mapping_source": source_by_folded.get(key),
                "occurrences": occurrence_count,
                "chapters": sorted(chapters.get(key, set())),
                "contexts": contexts.get(key, []),
            }
        )

    if recs_updated:
        try:
            recs_path.write_text(json.dumps(cached_recs, indent=2), encoding="utf-8")
        except Exception as exc:
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
    except Exception as exc:
        logger.warning(
            "Could not write the pronunciation inventory %s; the dashboard will show none: %s", inv_path, exc
        )

    cache_service.set(cache_key, {"sig": current_sig, "data": result}, ttl_seconds=1800)
    return result
