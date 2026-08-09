"""Character Analyzer — Pass 1 of the LLM Script Director.

Reads the full book text and produces a Character Registry with:
  - All speaking characters identified
  - Voice descriptions for TTS Voice Design
  - Personality traits and speaking style
  - A narrator voice suited to the book's genre/tone
"""

from __future__ import annotations

import logging
import json
import re
from pathlib import Path
from typing import Any

from brain.director.ollama_client import OllamaClient
from shared.constants import Gender
from shared.models import (
    Character,
    CharacterRegistry,
    ExtractedBook,
)

logger = logging.getLogger(__name__)

# Load prompt template
_PROMPT_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    """Load a prompt template from the prompts directory."""
    path = _PROMPT_DIR / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Prompt template not found: {path}")


# Inline fallback prompt if file doesn't exist yet
_SYSTEM_PROMPT = """You are an expert audiobook director and strict data extraction system. Analyze book text and extract ALL characters and their voice mappings.

### Character Extraction Guidelines
- Extract EVERY entity that actually speaks, regardless of whether it is a
  person, animal, artificial intelligence, ship, location, or personified object.
- Include ALL speaking characters, even minor or unnamed ones who speak only
  once (e.g. "a child", "the merchant", "a passing guard"). Create a descriptive
  ID for unnamed speakers like "child_female", "merchant", "guard".
- Recognize dialogue in straight/curly quotes, single typographic quotes, and
  em-dash dialogue conventions.
- Exclude an entity only when the supplied text provides no spoken dialogue.
- Never infer an entity's type or ability to speak from its name. Determine it
  only from explicit evidence in the supplied text.
- A named or personified place/object is not a speaking character unless the
  text explicitly attributes spoken dialogue to that entity. Thoughts,
  descriptions, invocations, and figurative personification are not dialogue.
- Animal noises (e.g. chirping, barking, roaring, squawking) and mental 
  impressions do NOT count as spoken dialogue. An animal is only a character 
  if it speaks actual linguistic words in quotes.

### Character ID Guidelines
- CRITICAL: Use the character's actual name as the character_id in snake_case.
  For example: "starling", "sixth_of_dusk", "mother_frond".
- Do NOT use generic IDs like "character_1", "character_2", "char_a".
- For unnamed speakers, use a descriptive ID: "child_female", "old_merchant",
  "guard_captain".
- Reuse an Existing Characters ID when later text uses a title, nickname, or
  shortened form for the same entity. Put every alternative name in `aliases`.
- Never merge identities merely because one name contains another. Family
  members, ranks, shared surnames, and similarly titled characters remain
  distinct unless the supplied text explicitly establishes identity.

### Voice Description Guidelines

Voice descriptions must strictly follow the official 12-dimension prompt framework for TTS VoiceDesign. Include the following keywords and dimensions explicitly:
- **Gender & Age**: (e.g., "male speaker, 30s age")
- **Pitch & Volume**: (e.g., "low pitch, moderate volume")
- **Speed**: (e.g., "fast speed", "measured speed")
- **Accent**: (e.g., "British English accent", "Standard American accent")
- **Texture & Clarity**: (e.g., "clear texture, slightly hoarse texture", "high clarity")
- **Fluency**: (e.g., "natural fluency")
- **Emotion, Tone & Personality**: (e.g., "warm emotion, authoritative tone, measured personality")

Do NOT use real person names. Use archetypes instead.
Keep descriptions under 50 words each.

### Book Genre: {genre}

The narrator voice should suit {genre} storytelling - e.g., authoritative tone, warm emotion, measured speed, high clarity.

---
## Output Schema

CRITICAL REMINDER: You MUST output ONLY valid JSON matching the Output Schema below. Do NOT output any conversational text, essays, explanations, or markdown fences. Just the raw JSON object starting with {{ and ending with }}.

{{
  "book_title": "string",
  "book_author": "string",
  "genre": "{genre}",
  "tone": "description of the book's overall tone",
  "characters": {{
    "narrator": {{
      "name": "Narrator",
      "gender": "male|female",
      "age_range": "string",
      "personality_traits": ["trait1", "trait2"],
      "aliases": ["explicit nickname or title"],
      "voice_description": "detailed voice description for TTS",
      "speaking_style": "how the narrator typically speaks",
      "test_sentence": "An INVENTED 15 to 25 word sentence showcasing the narrator's pacing and tone. DO NOT use fantasy names, places, or complex jargon."
    }},
    "character_name_in_snake_case": {{
      "name": "Character Display Name",
      "gender": "male|female|other",
      "age_range": "string",
      "personality_traits": ["trait1", "trait2"],
      "voice_description": "detailed voice description for TTS",
      "speaking_style": "how this character typically speaks",
      "test_sentence": "An INVENTED 15 to 25 word line of dialogue showcasing their personality. DO NOT use fantasy names, places, or complex jargon.",
      "dialogue_count": 0
    }}
  }}
}}"""

_USER_PROMPT = """## Source Book Text

{book_text}

Extract the characters from the text above as a valid JSON object matching the Output Schema. Do not output anything else.
"""


class CharacterAnalyzer:
    """Pass 1: Analyze a book to create a character registry."""

    def __init__(
        self,
        ollama: OllamaClient,
        temperature: float = 0.3,
        genre: str = "fantasy",
        max_unique_voices: int = 20,
        single_pass_threshold: int = 15_000,
    ):
        self.ollama = ollama
        self.temperature = temperature
        self.genre = genre
        self.max_unique_voices = max_unique_voices
        self.single_pass_threshold = single_pass_threshold

    def analyze(self, book: ExtractedBook) -> CharacterRegistry:
        """Analyze a book and produce a character registry, using multi-pass for long books."""
        total_chars = sum(len(ch.text) for ch in book.chapters)
        logger.info(
            "[CharacterAnalyzer] Starting Pass 1 for '%s' | chapters=%d | words=%d | total_chars=%d",
            book.metadata.title,
            book.metadata.total_chapters,
            book.metadata.total_words,
            total_chars,
        )

        import time as _time
        t0 = _time.time()

        if total_chars <= self.single_pass_threshold:
            # Single pass for standard books (fits within Qwen 32k context)
            book_text = self._prepare_book_text(book)
            system_prompt = _SYSTEM_PROMPT.format(genre=self.genre)
            prompt = _USER_PROMPT.format(book_text=book_text)

            raw_result = self.ollama.generate_json(
                prompt,
                temperature=self.temperature,
                system=system_prompt,
            )
            raw_characters = raw_result.get("characters", {})
            if isinstance(raw_characters, dict):
                raw_characters = self._consolidate_accumulated_characters(
                    raw_characters
                )
                raw_result["characters"] = self._adjudicate_name_candidates(
                    raw_characters,
                    book,
                )
            registry = self._parse_registry(raw_result, book.metadata.title, book.metadata.author)
        else:
            # Iterative multi-pass chapter-by-chapter analysis for long books
            logger.info("[CharacterAnalyzer] Long book detected (total_chars=%d) — running iterative multi-pass analysis", total_chars)
            accumulated_chars: dict[str, dict] = {}
            book_title = book.metadata.title
            book_author = book.metadata.author
            tone_desc = ""

            analysis_units = [
                (ch, part_index, part)
                for ch in book.chapters
                for part_index, part in enumerate(
                    self._iter_text_chunks(ch.text, 12_000),
                    1,
                )
            ]
            for idx, (ch, part_index, part) in enumerate(analysis_units):
                # Format current accumulated characters for context
                existing_summary = ""
                if accumulated_chars:
                    existing_summary = "\nExisting Characters:\n" + "\n".join(
                        f"- {cid}: {info.get('name', cid)}; aliases={info.get('aliases', [])} "
                        f"({info.get('gender', 'other')}, {info.get('voice_description', '')[:50]})"
                        for cid, info in accumulated_chars.items()
                    )

                ch_prompt = (
                    f"Chapter {ch.number}: {ch.title} "
                    f"(part {part_index})\n{existing_summary}\n\n"
                    f"Chapter Text:\n{part}"
                )
                system_prompt = _SYSTEM_PROMPT.format(genre=self.genre)

                try:
                    logger.info(
                        "[CharacterAnalyzer] Analyzing unit %d/%d: chapter %d "
                        "part %d '%s'...",
                        idx + 1,
                        len(analysis_units),
                        ch.number,
                        part_index,
                        ch.title,
                    )
                    raw_ch = self.ollama.generate_json(
                        ch_prompt,
                        temperature=self.temperature,
                        system=system_prompt,
                    )

                    if not tone_desc and raw_ch.get("tone"):
                        tone_desc = raw_ch.get("tone", "")

                    new_chars = raw_ch.get("characters", {})
                    for cid, cinfo in new_chars.items():
                        if not isinstance(cinfo, dict):
                            continue
                        norm_id = self._normalize_id(cid)
                        display_key = self._normalize_id(
                            str(cinfo.get("name", norm_id))
                        )
                        canonical_id = next(
                            (
                                existing_id
                                for existing_id, existing in accumulated_chars.items()
                                if existing_id == norm_id
                                or self._normalize_id(
                                    str(existing.get("name", existing_id))
                                )
                                == display_key
                            ),
                            norm_id,
                        )
                        if canonical_id not in accumulated_chars:
                            accumulated_chars[canonical_id] = cinfo
                        else:
                            existing = accumulated_chars[canonical_id]
                            existing["dialogue_count"] = self._safe_dialogue_count(
                                existing.get("dialogue_count", 0)
                            ) + self._safe_dialogue_count(
                                cinfo.get("dialogue_count", 0)
                            )
                            existing["personality_traits"] = list(
                                dict.fromkeys(
                                    list(existing.get("personality_traits", []))
                                    + list(cinfo.get("personality_traits", []))
                                )
                            )
                            old_desc = existing.get("voice_description", "")
                            new_desc = cinfo.get("voice_description", "")
                            if len(new_desc) > len(old_desc):
                                preserved_count = existing["dialogue_count"]
                                preserved_traits = existing["personality_traits"]
                                accumulated_chars[canonical_id] = cinfo
                                accumulated_chars[canonical_id][
                                    "dialogue_count"
                                ] = preserved_count
                                accumulated_chars[canonical_id][
                                    "personality_traits"
                                ] = preserved_traits
                except Exception as e:
                    raise RuntimeError(
                        f"Character analysis failed for chapter {ch.number}, "
                        f"part {part_index}"
                    ) from e

            # Consolidate only identities explicitly linked through aliases or
            # exact display names. Name containment is candidate evidence for
            # the LLM, never sufficient proof by itself.
            accumulated_chars = self._consolidate_accumulated_characters(accumulated_chars)
            accumulated_chars = self._adjudicate_name_candidates(
                accumulated_chars,
                book,
            )

            # Build final raw dict for parser
            final_raw = {
                "book_title": book_title,
                "book_author": book_author,
                "genre": self.genre,
                "tone": tone_desc,
                "characters": accumulated_chars,
            }
            registry = self._parse_registry(final_raw, book_title, book_author)

        elapsed = _time.time() - t0
        logger.info(
            "[CharacterAnalyzer] Pass 1 complete in %.1fs | %d characters: %s",
            elapsed,
            len(registry.characters),
            list(registry.characters.keys()),
        )

        return registry

    @staticmethod
    def _extract_dialogue_lines(text: str, max_chars: int = 2000) -> str:
        """Extract dialogue lines with surrounding attribution tags from chapter text.

        Returns a compact string of dialogue excerpts for character discovery.
        Each line includes ~30 chars of context before/after for speaker attribution.
        """
        # Match quoted dialogue (straight, curly, and single typographic)
        pattern = re.compile(
            r'(?:([\w\s,;:]+\s+)?'
            r'(?:"[^"\n]{3,}?"|\u201c[^\u201d\n]{3,}?\u201d|\u2018[^\u2019\n]{3,}?\u2019)'
            r'(?:\s*[\w\s,;:]+)?)',
            re.UNICODE,
        )
        excerpts: list[str] = []
        total = 0
        for match in pattern.finditer(text):
            start = max(0, match.start() - 30)
            end = min(len(text), match.end() + 30)
            excerpt = text[start:end].strip()
            if total + len(excerpt) > max_chars:
                break
            excerpts.append(excerpt)
            total += len(excerpt)
        return "\n".join(excerpts)

    def _prepare_book_text(self, book: ExtractedBook) -> str:
        """Prepare book text for single-pass analysis."""
        total_text = "\n\n---\n\n".join(
            f"## {ch.title}\n\n{ch.text}" for ch in book.chapters
        )
        logger.info(
            "[CharacterAnalyzer] Single-pass analysis (%.1f KB) — sending full book text",
            len(total_text) / 1024,
        )
        return total_text

    def _build_prompt(self, book_text: str, title: str, author: str) -> str:
        return ""

    @staticmethod
    def _normalize_id(value: str) -> str:
        import re

        normalized = re.sub(r"[^\w]+", "_", value.lower()).strip("_")
        return normalized or "unknown"

    @staticmethod
    def _safe_dialogue_count(value: object) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _iter_text_chunks(text: str, max_chars: int) -> list[str]:
        """Cover complete chapter text without truncating later characters."""
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = min(start + max_chars, len(text))
            if end < len(text):
                boundary = max(
                    text.rfind("\n\n", start, end),
                    text.rfind("\n", start, end),
                    text.rfind(" ", start, end),
                )
                if boundary > start + max_chars // 2:
                    end = boundary
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            start = end
            while start < len(text) and text[start].isspace():
                start += 1
        return chunks

    def _parse_registry(
        self,
        raw: dict,
        fallback_title: str,
        fallback_author: str,
    ) -> CharacterRegistry:
        """Parse LLM JSON output into a CharacterRegistry."""
        characters: dict[str, Character] = {}

        raw_chars = raw.get("characters", {})
        logger.info(
            "[CharacterAnalyzer] Parsing %d raw characters from LLM output",
            len(raw_chars),
        )

        for char_id, char_data in raw_chars.items():
            if not isinstance(char_data, dict):
                logger.warning("[CharacterAnalyzer] Skipping invalid char_data for '%s': %r", char_id, char_data)
                continue

            # Normalize character ID
            normalized_id = self._normalize_id(char_id)

            # Parse gender with robust normalizer
            gender_str = str(char_data.get("gender", "other")).lower().strip()
            if gender_str in ("male", "man", "boy", "he", "him", "his", "masculine", "gentleman", "sir", "father", "son", "brother", "husband", "lord", "king"):
                gender = Gender.MALE
            elif gender_str in ("female", "woman", "girl", "she", "her", "feminine", "lady", "ma'am", "mother", "daughter", "sister", "wife", "queen", "loremother"):
                gender = Gender.FEMALE
            elif re.search(r"\b(female|woman|girl|lady|she|her|mother|daughter)\b", gender_str):
                gender = Gender.FEMALE
            elif re.search(r"\b(male|man|boy|he|his|him|father|son)\b", gender_str):
                gender = Gender.MALE
            else:
                gender = Gender.OTHER

            try:
                dialogue_count = max(
                    0,
                    int(char_data.get("dialogue_count", 0) or 0),
                )
            except (TypeError, ValueError):
                dialogue_count = 0

            characters[normalized_id] = Character(
                id=normalized_id,
                name=char_data.get("name", normalized_id.replace("_", " ").title()),
                gender=gender,
                age_range=str(char_data.get("age_range", "unknown")),
                personality_traits=char_data.get("personality_traits", []),
                voice_description=str(char_data.get("voice_description", "")),
                speaking_style=str(char_data.get("speaking_style", "")),
                test_sentence=char_data.get("test_sentence"),
                dialogue_count=dialogue_count,
            )
            logger.info(
                "[CharacterAnalyzer]   + '%s' (%s) | %s | voice: %s",
                characters[normalized_id].name,
                normalized_id,
                gender_str,
                str(char_data.get("voice_description", ""))[:60],
            )

        # Ensure we have a narrator
        if "narrator" not in characters:
            characters["narrator"] = Character(
                id="narrator",
                name="Narrator",
                gender=Gender.MALE,
                age_range="40s",
                personality_traits=["measured", "warm", "authoritative"],
                voice_description=(
                    "male speaker, 40s age. low pitch, moderate volume, measured speed. "
                    "Standard American accent. clear texture, high clarity, natural fluency. "
                    "warm emotion, authoritative tone, thoughtful personality."
                ),
                speaking_style="Flowing descriptive prose, unhurried",
                test_sentence="The forest was old, its roots deep in the forgotten history of the world.",
            )
            logger.warning("[CharacterAnalyzer] LLM didn't produce a narrator — using default")

        # Keep every speaking character for attribution, but cap distinct voice
        # references by assigning low-dialogue characters a stable shared voice.
        if len(characters) > self.max_unique_voices:
            logger.info(
                "[CharacterAnalyzer] Capping %d → %d unique voices",
                len(characters),
                self.max_unique_voices,
            )
            ranked = sorted(
                (
                    character
                    for character in characters.values()
                    if character.id != "narrator"
                ),
                key=lambda character: (
                    character.dialogue_count,
                    len(character.voice_description),
                ),
                reverse=True,
            )
            selected_order = ["narrator"] + [
                character.id
                for character in ranked[: self.max_unique_voices - 1]
            ]
            important = set(selected_order)
            representatives: dict[Gender, list[str]] = {
                gender: [
                    character_id
                    for character_id in selected_order
                    if characters[character_id].gender == gender
                ]
                for gender in Gender
            }
            for character_id, character in characters.items():
                if character_id in important:
                    character.voice_id = character_id
                    continue
                same_gender = representatives.get(character.gender, [])
                character.voice_id = (
                    same_gender[0] if same_gender else "narrator"
                )
        else:
            for character_id, character in characters.items():
                character.voice_id = character_id

        return CharacterRegistry(
            book_title=raw.get("book_title", fallback_title),
            book_author=raw.get("book_author", fallback_author),
            genre=raw.get("genre", self.genre),
            tone=raw.get("tone", ""),
            characters=characters,
        )

    @staticmethod
    def _consolidate_accumulated_characters(
        accumulated_chars: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Merge only explicit aliases and exact normalized display names.

        Substring/suffix matching is intentionally forbidden: ``king`` and
        ``red_king`` (or ``john`` and ``uncle_john``) may be different people.
        """
        keys = list(accumulated_chars.keys())
        merged_into: dict[str, str] = {}

        for i, cid1 in enumerate(keys):
            if cid1 in merged_into:
                continue
            cinfo1 = accumulated_chars[cid1]

            for j in range(i + 1, len(keys)):
                cid2 = keys[j]
                if cid2 in merged_into:
                    continue
                cinfo2 = accumulated_chars[cid2]

                aliases1 = {
                    CharacterAnalyzer._normalize_id(str(value))
                    for value in cinfo1.get("aliases", [])
                    if str(value).strip()
                }
                aliases2 = {
                    CharacterAnalyzer._normalize_id(str(value))
                    for value in cinfo2.get("aliases", [])
                    if str(value).strip()
                }
                name1 = CharacterAnalyzer._normalize_id(
                    str(cinfo1.get("name", cid1))
                )
                name2 = CharacterAnalyzer._normalize_id(
                    str(cinfo2.get("name", cid2))
                )
                explicitly_linked = (
                    cid2 in aliases1
                    or name2 in aliases1
                    or cid1 in aliases2
                    or name1 in aliases2
                    or name1 == name2
                )
                target_id = variant_id = None
                target_info = variant_info = None
                if explicitly_linked:
                    # Prefer the more descriptive canonical ID. Input order is
                    # only a deterministic tie breaker.
                    if (len(cid1.split("_")), len(cid1)) >= (
                        len(cid2.split("_")), len(cid2)
                    ):
                        target_id, variant_id = cid1, cid2
                        target_info, variant_info = cinfo1, cinfo2
                    else:
                        target_id, variant_id = cid2, cid1
                        target_info, variant_info = cinfo2, cinfo1

                if target_id and variant_id and target_info and variant_info:
                    logger.info(
                        "[CharacterAnalyzer] Consolidating short variant '%s' (%s) into canonical key '%s' (%s)",
                        variant_id,
                        variant_info.get("name"),
                        target_id,
                        target_info.get("name"),
                    )
                    target_info["dialogue_count"] = (
                        target_info.get("dialogue_count", 0)
                        + variant_info.get("dialogue_count", 0)
                    )
                    target_info["mention_count"] = (
                        target_info.get("mention_count", 0)
                        + variant_info.get("mention_count", 0)
                    )
                    existing_aliases = set(target_info.get("aliases", []))
                    existing_aliases.add(variant_info.get("name", variant_id))
                    existing_aliases.add(variant_id)
                    target_info["aliases"] = sorted(list(existing_aliases))
                    merged_into[variant_id] = target_id

        return {k: v for k, v in accumulated_chars.items() if k not in merged_into}

    def _adjudicate_name_candidates(
        self,
        characters: dict[str, dict[str, Any]],
        book: ExtractedBook,
    ) -> dict[str, dict[str, Any]]:
        """Resolve possible title/short-name identities from textual evidence.

        Name containment only proposes a pair. A merge requires both a positive
        LLM identity decision and source support: either a verbatim cited excerpt
        or a compact passage that introduces the long name and then continues
        with the short name. This tolerates imperfect citation formatting without
        turning name containment alone into merge evidence.
        """
        ids = sorted(characters)
        candidate_pairs: list[tuple[str, str]] = []
        for index, left in enumerate(ids):
            if left == "narrator":
                continue
            left_parts = left.split("_")
            for right in ids[index + 1:]:
                if right == "narrator":
                    continue
                right_parts = right.split("_")
                if (
                    len(left_parts) < len(right_parts)
                    and right_parts[-len(left_parts):] == left_parts
                ) or (
                    len(right_parts) < len(left_parts)
                    and left_parts[-len(right_parts):] == right_parts
                ):
                    candidate_pairs.append((left, right))
        if not candidate_pairs:
            return characters

        source = "\n".join(chapter.text for chapter in book.chapters)
        payload: list[dict[str, Any]] = []
        contexts: dict[tuple[str, str], str] = {}
        for left, right in candidate_pairs:
            terms = {
                str(characters[left].get("name", left)).strip(),
                str(characters[right].get("name", right)).strip(),
                left.replace("_", " "),
                right.replace("_", " "),
            }
            snippets: list[str] = []
            for term in sorted(terms, key=len, reverse=True):
                if len(term) < 3:
                    continue
                for match in re.finditer(re.escape(term), source, re.IGNORECASE):
                    start = max(0, match.start() - 220)
                    end = min(len(source), match.end() + 220)
                    snippet = source[start:end].strip()
                    if snippet and snippet not in snippets:
                        snippets.append(snippet)
                    if len(snippets) >= 8:
                        break
                if len(snippets) >= 8:
                    break
            context = "\n---\n".join(snippets)
            if not context:
                continue
            contexts[(left, right)] = context
            payload.append(
                {
                    "left_id": left,
                    "left": characters[left],
                    "right_id": right,
                    "right": characters[right],
                    "source_context": context,
                }
            )
        if not payload:
            return characters

        prompt = (
            "Determine whether each candidate pair is the same fictional "
            "entity. Similar names, shared titles, family names, and suffixes "
            "are not proof. Return JSON with a `decisions` array containing "
            "left_id, right_id, same_character, and evidence. For a true "
            "decision, evidence must be a verbatim source excerpt that "
            "establishes both names refer to one entity.\n\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        try:
            raw = self.ollama.generate_json(
                prompt,
                temperature=0.0,
                system=(
                    "You are a conservative entity-identity adjudicator. "
                    "False merges are more harmful than duplicate entries."
                ),
            )
        except Exception as exc:
            logger.warning("Character identity adjudication failed: %s", exc)
            return characters

        valid_pairs = set(contexts)
        decisions = raw.get("decisions", [])
        if not isinstance(decisions, list):
            logger.warning("Character identity adjudicator returned no decisions array")
            return characters
        for decision in decisions:
            if not isinstance(decision, dict) or not decision.get("same_character"):
                continue
            pair = (
                self._normalize_id(str(decision.get("left_id", ""))),
                self._normalize_id(str(decision.get("right_id", ""))),
            )
            if pair not in valid_pairs and pair[::-1] in valid_pairs:
                pair = pair[::-1]
            if pair not in valid_pairs:
                continue
            evidence = " ".join(str(decision.get("evidence", "")).split())
            normalized_context = " ".join(contexts[pair].split()).casefold()
            cited_verbatim = (
                len(evidence) >= 12
                and evidence.casefold() in normalized_context
            )
            derived_evidence = self._find_short_name_continuation(
                characters[pair[0]],
                characters[pair[1]],
                book,
            )
            if not cited_verbatim and not derived_evidence:
                logger.warning(
                    "Ignoring unsupported identity merge %s/%s",
                    *pair,
                )
                continue
            if not cited_verbatim:
                logger.info(
                    "Accepting identity merge %s/%s from verified short-name "
                    "continuation: %s",
                    *pair,
                    derived_evidence,
                )
            left, right = pair
            target, variant = max(
                (left, right),
                key=lambda value: (len(value.split("_")), len(value)),
            ), min(
                (left, right),
                key=lambda value: (len(value.split("_")), len(value)),
            )
            aliases = list(characters[target].get("aliases", []))
            aliases.extend(
                [variant, str(characters[variant].get("name", variant))]
            )
            characters[target]["aliases"] = list(dict.fromkeys(aliases))

        return self._consolidate_accumulated_characters(characters)

    @classmethod
    def _find_short_name_continuation(
        cls,
        left: dict[str, Any],
        right: dict[str, Any],
        book: ExtractedBook,
    ) -> str | None:
        """Return source evidence for a long-name to short-name continuation.

        The two mentions must be distinct, in that order, and in one paragraph.
        An optional article inside the long form handles titles such as
        ``Sixth of the Dusk`` when the registry normalized it to
        ``Sixth of Dusk``. This helper never decides identity by itself; callers
        also require the conservative LLM adjudicator to return
        ``same_character=true``.
        """
        names = [
            str(left.get("name", "")).strip(),
            str(right.get("name", "")).strip(),
        ]
        if not all(names):
            return None
        long_name, short_name = sorted(
            names,
            key=lambda value: (len(cls._normalize_id(value).split("_")), len(value)),
            reverse=True,
        )
        long_tokens = re.findall(r"[\w']+", long_name, flags=re.UNICODE)
        short_tokens = re.findall(r"[\w']+", short_name, flags=re.UNICODE)
        if len(long_tokens) <= len(short_tokens) or not short_tokens:
            return None

        separator = r"\W+(?:the\W+)?"
        long_pattern = re.compile(
            r"\b" + separator.join(map(re.escape, long_tokens)) + r"\b",
            re.IGNORECASE,
        )
        short_pattern = re.compile(
            r"\b" + r"\W+".join(map(re.escape, short_tokens)) + r"\b",
            re.IGNORECASE,
        )
        for chapter in book.chapters:
            for paragraph in re.split(r"\n\s*\n", chapter.text):
                if len(paragraph) > 1200:
                    continue
                for long_match in long_pattern.finditer(paragraph):
                    for short_match in short_pattern.finditer(
                        paragraph,
                        long_match.end(),
                    ):
                        if short_match.start() - long_match.end() > 500:
                            break
                        evidence = " ".join(paragraph.split())
                        if len(evidence) > 600:
                            evidence = evidence[:597].rstrip() + "..."
                        return evidence
        return None
