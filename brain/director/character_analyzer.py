"""Character Analyzer — Pass 1 of the LLM Script Director.

Reads the full book text and produces a Character Registry with:
  - All speaking characters identified
  - Voice descriptions for TTS Voice Design
  - Personality traits and speaking style
  - A narrator voice suited to the book's genre/tone
"""

from __future__ import annotations

import logging
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
_SYSTEM_PROMPT = """You are an expert audiobook director and strict data extraction system. Analyze book text and extract all characters and their voice mappings.

### Character Extraction Guidelines
- CRITICAL: ONLY extract characters who ACTUALLY SPEAK SPOKEN DIALOGUE in quotation marks ("...").
- Do NOT extract personified locations, islands, ships, animals, or non-speaking entities (e.g. islands like Vathi/Patji, ships, inanimate objects, or creatures that never speak dialogue).
- If an entity is mentioned in narration but NEVER speaks spoken dialogue, do NOT include them in the character registry.

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
    "character_id": {{
      "name": "Character Display Name",
      "gender": "male|female|other",
      "age_range": "string",
      "personality_traits": ["trait1", "trait2"],
      "voice_description": "detailed voice description for TTS",
      "speaking_style": "how this character typically speaks"
    }}
  }}
}}
"""

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
    ):
        self.ollama = ollama
        self.temperature = temperature
        self.genre = genre
        self.max_unique_voices = max_unique_voices

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

        if total_chars <= 25_000 or len(book.chapters) <= 1:
            # Single pass for short books
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

            for idx, ch in enumerate(book.chapters):
                # Format current accumulated characters for context
                existing_summary = ""
                if accumulated_chars:
                    existing_summary = "\nExisting Characters:\n" + "\n".join(
                        f"- {cid}: {info.get('name', cid)} ({info.get('gender', 'other')}, {info.get('voice_description', '')[:50]})"
                        for cid, info in accumulated_chars.items()
                    )

                ch_prompt = f"Chapter {ch.number}: {ch.title}\n{existing_summary}\n\nChapter Text:\n{ch.text[:12000]}"
                system_prompt = _SYSTEM_PROMPT.format(genre=self.genre)

                try:
                    logger.info("[CharacterAnalyzer] Analyzing chapter %d/%d: '%s' (%d words)...", idx + 1, len(book.chapters), ch.title, ch.word_count)
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
                        norm_id = cid.lower().replace(" ", "_").replace("-", "_")
                        if norm_id not in accumulated_chars:
                            accumulated_chars[norm_id] = cinfo
                        else:
                            # Update if existing info is sparse
                            old_desc = accumulated_chars[norm_id].get("voice_description", "")
                            new_desc = cinfo.get("voice_description", "")
                            if len(new_desc) > len(old_desc):
                                accumulated_chars[norm_id] = cinfo
                except Exception as e:
                    logger.warning("[CharacterAnalyzer] Failed to analyze chapter %d: %s", idx + 1, e)

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

    def _prepare_book_text(self, book: ExtractedBook) -> str:
        """Prepare book text for the LLM, handling long books."""
        total_text = "\n\n---\n\n".join(
            f"## {ch.title}\n\n{ch.text}" for ch in book.chapters
        )
        total_len = len(total_text)

        if total_len < 25_000:
            logger.info(
                "[CharacterAnalyzer] Book fits in context (%.1f KB) — sending full text",
                total_len / 1024,
            )
            return total_text

        logger.info(
            "[CharacterAnalyzer] Book is large (%.1f KB > 25 KB limit) — using strict summary strategy",
            total_len / 1024,
        )
        parts: list[str] = []
        current_len = 0

        for idx, ch in enumerate(book.chapters):
            if idx == 0 and len(ch.text) < 15_000:
                text_to_add = f"## {ch.title} [FULL TEXT]\n\n{ch.text}"
                strategy = "FULL TEXT"
            else:
                summary = ch.text[:500].rsplit(".", 1)[0] + "."
                text_to_add = f"## {ch.title} [SUMMARY]\n\n{summary}"
                strategy = "SUMMARY"

            if current_len + len(text_to_add) > 25_000:
                logger.warning(
                    "[CharacterAnalyzer] Truncated at chapter %d/%d (%.1f KB used) — context limit reached",
                    idx,
                    len(book.chapters),
                    current_len / 1024,
                )
                break

            logger.info(
                "[CharacterAnalyzer] Ch%d '%s': %s (+%.1f KB, total %.1f KB)",
                idx + 1,
                ch.title[:40],
                strategy,
                len(text_to_add) / 1024,
                (current_len + len(text_to_add)) / 1024,
            )
            parts.append(text_to_add)
            current_len += len(text_to_add)

        logger.info(
            "[CharacterAnalyzer] Prepared %d/%d chapters for LLM (%.1f KB)",
            len(parts),
            len(book.chapters),
            current_len / 1024,
        )
        return "\n\n---\n\n".join(parts)

    def _build_prompt(self, book_text: str, title: str, author: str) -> str:
        return ""

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
            normalized_id = char_id.lower().replace(" ", "_").replace("-", "_")

            # Parse gender
            gender_str = str(char_data.get("gender", "other")).lower()
            try:
                gender = Gender(gender_str)
            except ValueError:
                logger.warning(
                    "[CharacterAnalyzer] Unknown gender '%s' for '%s' — defaulting to OTHER",
                    gender_str, normalized_id,
                )
                gender = Gender.OTHER

            characters[normalized_id] = Character(
                id=normalized_id,
                name=char_data.get("name", normalized_id.replace("_", " ").title()),
                gender=gender,
                age_range=str(char_data.get("age_range", "unknown")),
                personality_traits=char_data.get("personality_traits", []),
                voice_description=str(char_data.get("voice_description", "")),
                speaking_style=str(char_data.get("speaking_style", "")),
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

        # Cap unique voices
        if len(characters) > self.max_unique_voices:
            logger.info(
                "[CharacterAnalyzer] Capping %d → %d unique voices",
                len(characters),
                self.max_unique_voices,
            )
            important = {"narrator"}
            for k in list(raw_chars.keys()):
                normalized = k.lower().replace(" ", "_").replace("-", "_")
                important.add(normalized)
                if len(important) >= self.max_unique_voices:
                    break
            characters = {k: v for k, v in characters.items() if k in important}

        return CharacterRegistry(
            book_title=raw.get("book_title", fallback_title),
            book_author=raw.get("book_author", fallback_author),
            genre=raw.get("genre", self.genre),
            tone=raw.get("tone", ""),
            characters=characters,
        )
