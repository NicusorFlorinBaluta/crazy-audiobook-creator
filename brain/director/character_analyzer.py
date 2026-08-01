"""Character Analyzer — Pass 1 of the LLM Script Director.

Reads the full book text and produces a Character Registry with:
  - All speaking characters identified
  - Voice descriptions for TTS Voice Design
  - Personality traits and speaking style
  - A narrator voice suited to the book's genre/tone
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

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

### Character ID Guidelines
- CRITICAL: Use the character's actual name as the character_id in snake_case.
  For example: "starling", "sixth_of_dusk", "mother_frond".
- Do NOT use generic IDs like "character_1", "character_2", "char_a".
- For unnamed speakers, use a descriptive ID: "child_female", "old_merchant",
  "guard_captain".

### Voice Description Guidelines

Voice descriptions must be specific and actionable. Include:
- **Gender and age**: "young female, early 20s" or "elderly male, 70s"
- **Pitch**: "high-pitched", "deep baritone", "medium tenor"
- **Pace**: "fast-talking", "measured and deliberate", "slow and ponderous"
- **Quality**: "gravelly", "silky smooth", "raspy", "clear and bell-like"
- **Accent/Pronunciation**: "British RP", "no strong accent", "slight roughness"
- **Emotional baseline**: "warm and kind", "cold and calculating", "nervous energy"

Do NOT use real person names. Use archetypes instead.
Keep descriptions under 50 words each.

### Book Genre: {genre}

The narrator voice should suit {genre} storytelling - authoritative but warm, with gravitas for dramatic moments and warmth for intimate scenes.

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
      "voice_description": "detailed voice description for TTS",
      "speaking_style": "how the narrator typically speaks"
    }},
    "character_name_in_snake_case": {{
      "name": "Character Display Name",
      "gender": "male|female|other",
      "age_range": "string",
      "personality_traits": ["trait1", "trait2"],
      "voice_description": "detailed voice description for TTS",
      "speaking_style": "how this character typically speaks",
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
                        f"- {cid}: {info.get('name', cid)} ({info.get('gender', 'other')}, {info.get('voice_description', '')[:50]})"
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

            # Consolidate short-name variants (e.g., 'dusk' into 'sixth_of_dusk')
            accumulated_chars = self._consolidate_accumulated_characters(accumulated_chars)

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
                    "A warm, mature male baritone, early 40s, with a measured "
                    "storytelling cadence. Rich and clear with natural gravitas. "
                    "Thoughtful pauses between phrases."
                ),
                speaking_style="Flowing descriptive prose, unhurried",
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
        """Merge short title variants (e.g. 'dusk' into 'sixth_of_dusk') into canonical entries."""
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

                is_variant = False
                target_id, variant_id, target_info, variant_info = None, None, None, None

                if cid1.endswith(f"_{cid2}"):
                    target_id, variant_id = cid1, cid2
                    target_info, variant_info = cinfo1, cinfo2
                    is_variant = True
                elif cid2.endswith(f"_{cid1}"):
                    target_id, variant_id = cid2, cid1
                    target_info, variant_info = cinfo2, cinfo1
                    is_variant = True

                if is_variant and target_id and variant_id:
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
