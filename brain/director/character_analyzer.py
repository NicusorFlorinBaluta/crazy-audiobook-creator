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
_CHARACTER_PROMPT = """You are an expert audiobook director preparing a novel for multi-voice narration. Your task is to analyze this book and create a character registry with voice descriptions for text-to-speech generation.

## Instructions

1. Read the following text carefully
2. Identify ALL speaking characters (anyone who has dialogue)
3. For each character, determine:
   - Their gender, approximate age, and key personality traits
   - A detailed voice description suitable for voice synthesis
4. Also create a narrator voice that fits the book's genre and tone
5. Output ONLY valid JSON — no explanation, no markdown code fences

## Voice Description Guidelines

Voice descriptions must be specific and actionable. Include:
- **Gender and age**: "young female, early 20s" or "elderly male, 70s"
- **Pitch**: "high-pitched", "deep baritone", "medium tenor"
- **Pace**: "fast-talking", "measured and deliberate", "slow and ponderous"
- **Quality**: "gravelly", "silky smooth", "raspy", "clear and bell-like"
- **Accent/Pronunciation**: "British RP", "no strong accent", "slight roughness"
- **Emotional baseline**: "warm and kind", "cold and calculating", "nervous energy"

Do NOT use real person names. Use archetypes instead.
Keep descriptions under 50 words each.

## Book Genre: {genre}

The narrator voice should suit {genre} storytelling — authoritative but warm, with gravitas for dramatic moments and warmth for intimate scenes.

## Output Schema

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

## Book Text

{book_text}"""


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
        """Analyze a book and produce a character registry.

        For books that fit in the LLM context window, sends the full text.
        For longer books, uses the chapter summary strategy:
        first 3 chapters + last chapter + summaries.

        Args:
            book: Extracted book with chapters.

        Returns:
            CharacterRegistry with all identified characters.
        """
        logger.info(
            "Starting character analysis for '%s' (%d chapters, %d words)",
            book.metadata.title,
            book.metadata.total_chapters,
            book.metadata.total_words,
        )

        book_text = self._prepare_book_text(book)
        prompt = self._build_prompt(book_text, book.metadata.title, book.metadata.author)

        logger.info("Sending character analysis to LLM (%.1f KB prompt)...", len(prompt) / 1024)
        raw_result = self.ollama.generate_json(
            prompt,
            temperature=self.temperature,
        )

        registry = self._parse_registry(raw_result, book.metadata.title, book.metadata.author)

        logger.info(
            "Character analysis complete: %d characters identified",
            len(registry.characters),
        )

        return registry

    def _prepare_book_text(self, book: ExtractedBook) -> str:
        """Prepare book text for the LLM, handling long books.

        Strategy for long books (>100K words):
        - Send first 3 chapters in full (establish main characters)
        - Send last chapter in full (character arc endpoints)
        - For remaining chapters, send a paragraph-level summary
        """
        total_text = "\n\n---\n\n".join(
            f"## {ch.title}\n\n{ch.text}" for ch in book.chapters
        )

        # If the text is reasonably sized (rough estimate: <80K chars ≈ fits context),
        # send everything
        if len(total_text) < 80_000:
            return total_text

        # For long books, use the summary strategy
        logger.info("Book is long (%d chars), using summary strategy", len(total_text))
        parts: list[str] = []

        # First 3 chapters in full
        for ch in book.chapters[:3]:
            parts.append(f"## {ch.title} [FULL TEXT]\n\n{ch.text}")

        # Middle chapters: first 500 chars as summary
        for ch in book.chapters[3:-1]:
            summary = ch.text[:500].rsplit(".", 1)[0] + "."
            parts.append(f"## {ch.title} [SUMMARY]\n\n{summary}")

        # Last chapter in full
        if len(book.chapters) > 3:
            last = book.chapters[-1]
            parts.append(f"## {last.title} [FULL TEXT]\n\n{last.text}")

        return "\n\n---\n\n".join(parts)

    def _build_prompt(self, book_text: str, title: str, author: str) -> str:
        """Build the character analysis prompt."""
        # Try to load from file first
        try:
            template = _load_prompt("character_extraction.md")
        except FileNotFoundError:
            template = _CHARACTER_PROMPT

        return template.format(
            genre=self.genre,
            book_text=book_text,
        )

    def _parse_registry(
        self,
        raw: dict,
        fallback_title: str,
        fallback_author: str,
    ) -> CharacterRegistry:
        """Parse LLM JSON output into a CharacterRegistry."""
        characters: dict[str, Character] = {}

        raw_chars = raw.get("characters", {})
        for char_id, char_data in raw_chars.items():
            if not isinstance(char_data, dict):
                continue

            # Normalize character ID
            char_id = char_id.lower().replace(" ", "_").replace("-", "_")

            # Parse gender
            gender_str = str(char_data.get("gender", "other")).lower()
            try:
                gender = Gender(gender_str)
            except ValueError:
                gender = Gender.OTHER

            characters[char_id] = Character(
                id=char_id,
                name=char_data.get("name", char_id.replace("_", " ").title()),
                gender=gender,
                age_range=str(char_data.get("age_range", "unknown")),
                personality_traits=char_data.get("personality_traits", []),
                voice_description=str(char_data.get("voice_description", "")),
                speaking_style=str(char_data.get("speaking_style", "")),
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
            logger.warning("LLM didn't produce a narrator — using default")

        # Cap unique voices
        if len(characters) > self.max_unique_voices:
            logger.info(
                "Capping characters from %d to %d unique voices",
                len(characters),
                self.max_unique_voices,
            )
            # Keep narrator + most important characters (first N in LLM output order)
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
