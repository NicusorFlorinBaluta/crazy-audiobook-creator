"""Character Analyzer — Pass 1 of the LLM Script Director.

Reads the full book text and produces a Character Registry with:
  - All speaking characters identified
  - Voice descriptions for TTS Voice Design
  - Personality traits and speaking style
  - A narrator voice suited to the book's genre/tone
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
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

# Included in project dependency fingerprints. Increment whenever deterministic
# post-processing changes the contents or meaning of a character registry.
CHARACTER_ANALYSIS_REVISION = 4

_GENERIC_ROLE_DESCRIPTORS = {
    "stranger", "alien", "soldier", "guard", "captain", "officer",
    "attendant", "voice", "figure", "traveler", "shadow", "visitor",
    "servant", "priest", "doctor", "elder", "crewman", "fellow",
    "man", "woman", "boy", "girl", "child", "person", "someone", "speaker",
    "individual", "human", "entity", "presence", "inhabitant", "citizen",
    "driver", "pilot", "merchant", "trader", "bystander", "passerby",
    "guest", "host", "friend", "enemy", "leader", "chief", "master",
    "worker", "assistant", "aide", "deputy", "agent", "scout",
}

_UNSAFE_CHARACTER_ALIASES = {
    "a", "an", "and", "the", "of", "narrator", "she", "he", "it",
    "they", "him", "her", "his", "hers", "them", "male", "female",
    "character", "unidentified",
} | _GENERIC_ROLE_DESCRIPTORS

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

### Gender Resolution Guidelines
- Determine canonical gender (`male` or `female`) by actively scanning surrounding narrative text for explicit pronouns (`he`, `him`, `his`, `himself`, `man`, `boy`, `father`, `son`, `brother`, `husband`, `sir`, `lord` vs `she`, `her`, `hers`, `herself`, `woman`, `girl`, `mother`, `daughter`, `sister`, `wife`, `lady`).
- Do NOT mark a named character as `other` if surrounding narrative text uses `he` or `she`.
- Use `other` ONLY for true non-gendered collective entities, swarms, or when zero gender indicators exist in the entire text.

### Character ID & Narrator Guidelines
- CRITICAL: Use the character's actual name as the character_id in snake_case.
  For example: "starling", "sixth_of_dusk", "mother_frond", "breezy".
- Do NOT use generic IDs like "character_1", "character_2", "char_a".
- For unnamed speakers, use a descriptive ID: "child_female", "old_merchant",
  "guard_captain".
- Reuse an Existing Characters ID when later text uses a title, nickname, or
  shortened form for the same entity. Put every alternative name in `aliases`.
- Never merge identities merely because one name contains another. Family
  members, ranks, shared surnames, and similarly titled characters remain
  distinct unless the supplied text explicitly establishes identity.
- NARRATOR VS IN-WORLD CHARACTERS: The "narrator" entry is strictly the audiobook reader role for unquoted narrative prose. NEVER add in-world character names or aliases to "narrator". If the book is written in the first person or includes POV journal entries/reflections (e.g. Breezy, Katniss, Percy), the protagonist MUST have their own distinct character card (e.g. "breezy") for their spoken dialogue turns.

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
      "aliases": [],
      "voice_description": "detailed voice description for TTS",
      "speaking_style": "how the narrator typically speaks",
      "test_sentence": "An INVENTED 15 to 25 word sentence showcasing the narrator's pacing and tone. DO NOT use fantasy names, places, or complex jargon."
    }},
    "character_name_in_snake_case": {{
      "name": "Character Display Name",
      "gender": "male|female|other",
      "age_range": "string",
      "personality_traits": ["trait1", "trait2"],
      "aliases": ["nickname or alternative name"],
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
        max_unique_voices: int = 0,
        single_pass_threshold: int = 15_000,
        external_validator: Any | None = None,
    ):
        self.ollama = ollama
        self.temperature = temperature
        self.genre = genre
        self.max_unique_voices = max_unique_voices
        self.single_pass_threshold = single_pass_threshold
        self.external_validator = external_validator

    def analyze(
        self,
        book: ExtractedBook,
        check_callback: Callable[[], None] | None = None,
        checkpoint_path: Path | str | None = None,
        checkpoint_fingerprint: str = "",
        reference_audit_path: Path | str | None = None,
        project_dir: Path | str | None = None,
        progress_callback: Callable[[float, str, int, int, int], None] | None = None,
    ) -> CharacterRegistry:
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

        # Format any author-provided reference material (Glossary, Dramatis Personae, Character Lists)
        reference_summary = ""
        if getattr(book, "reference_material", None):
            guide_parts: list[str] = []
            remaining_reference_chars = 12_000
            for title, text in book.reference_material.items():
                cleaned = text.strip() if text else ""
                if not cleaned or remaining_reference_chars <= 0:
                    continue
                excerpt = cleaned[: min(6000, remaining_reference_chars)]
                guide_parts.append(f"### {title}\n{excerpt}")
                remaining_reference_chars -= len(excerpt)
            if guide_parts:
                reference_summary = (
                    "\n\nSupplemental author reference material (use for canonical spellings and roles, but prefer direct narrative evidence when it conflicts):\n"
                    + "\n\n".join(guide_parts)
                )
                logger.info(
                    "[CharacterAnalyzer] Using %d reference section(s) to seed character analysis",
                    len(guide_parts),
                )
        if total_chars <= self.single_pass_threshold:
            # Single pass for standard books (fits within Qwen 32k context)
            if progress_callback:
                progress_callback(0.0, "Pass 1 Character Discovery: analyzing full book...", 1, 1, 1)
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
            start_unit_idx = 0

            if checkpoint_path:
                checkpoint_path = Path(checkpoint_path)
                if checkpoint_path.exists():
                    try:
                        ckpt_data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                        if (
                            not checkpoint_fingerprint
                            or ckpt_data.get("fingerprint") != checkpoint_fingerprint
                        ):
                            raise ValueError("checkpoint dependencies changed")
                        accumulated_chars = ckpt_data.get("accumulated_chars", {})
                        tone_desc = ckpt_data.get("tone_desc", "")
                        start_unit_idx = int(ckpt_data.get("last_completed_unit", -1)) + 1
                        logger.info(
                            "[CharacterAnalyzer] Restored checkpoint from %s: resuming at unit %d with %d known characters",
                            checkpoint_path.name,
                            start_unit_idx + 1,
                            len(accumulated_chars),
                        )
                    except Exception as exc:
                        logger.warning("[CharacterAnalyzer] Could not load checkpoint %s: %s", checkpoint_path, exc)
                        accumulated_chars = {}
                        start_unit_idx = 0

            analysis_units = [
                (ch, part_index, part)
                for ch in book.chapters
                for part_index, part in enumerate(
                    self._iter_text_chunks(ch.text, 12_000),
                    1,
                )
            ]
            for idx, (ch, part_index, part) in enumerate(analysis_units):
                if idx < start_unit_idx:
                    continue
                if check_callback:
                    check_callback()
                # Format current accumulated characters for context
                existing_summary = ""
                if accumulated_chars:
                    existing_summary = "\nExisting Characters:\n" + "\n".join(
                        f"- {cid}: {info.get('name', cid)}; aliases={info.get('aliases', [])} "
                        f"({info.get('gender', 'other')}, {info.get('voice_description', '')[:50]})"
                        for cid, info in accumulated_chars.items()
                    )

                ref_block = ""
                if idx == 0 and reference_summary:
                    ref_block = f"{reference_summary}\n\n"

                ch_prompt = (
                    f"Chapter {ch.number}: {ch.title} "
                    f"(part {part_index})\n{ref_block}{existing_summary}\n\n"
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
                    if progress_callback:
                        pct = round((idx / len(analysis_units)) * 100.0, 1)
                        msg = f"Pass 1 Character Discovery: unit {idx + 1} of {len(analysis_units)} (Ch {ch.number}: {ch.title})"
                        try:
                            progress_callback(pct, msg, ch.number, idx + 1, len(analysis_units))
                        except Exception:
                            pass
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

                    if checkpoint_path:
                        try:
                            from shared.artifacts import atomic_write_json
                            atomic_write_json(
                                checkpoint_path,
                                {
                                    "fingerprint": checkpoint_fingerprint,
                                    "last_completed_unit": idx,
                                    "accumulated_chars": accumulated_chars,
                                    "tone_desc": tone_desc,
                                },
                            )
                        except Exception as ckpt_err:
                            logger.debug("[CharacterAnalyzer] Checkpoint save failed: %s", ckpt_err)
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

            if checkpoint_path and checkpoint_path.exists():
                try:
                    checkpoint_path.unlink(missing_ok=True)
                except Exception:
                    pass

        registry = self._ensure_explicit_unnamed_speakers(registry, book)
        registry = self._augment_characters_with_gemini(
            registry,
            book,
            Path(project_dir) if project_dir else None,
        )
        self._assign_voice_ids(registry.characters)

        if reference_audit_path and getattr(book, "reference_material", None):
            self._write_reference_audit(registry, book, reference_audit_path)

        return registry

    def _build_character_evidence_dossier(
        self,
        registry: CharacterRegistry,
        book: ExtractedBook,
    ) -> dict[str, Any]:
        """Collect whole-book narrative evidence, dialogue samples, and pronoun counts for all characters."""
        all_text = "\n".join(ch.text for ch in book.chapters)
        dossier: dict[str, Any] = {}
        for cid, char in registry.characters.items():
            if cid == "narrator":
                continue
            name = char.name or cid
            search_terms = list(dict.fromkeys([name, cid.replace("_", " "), *(char.aliases or [])]))
            search_terms = [
                t for t in search_terms
                if len(t) >= 3 and t.lower() not in _UNSAFE_CHARACTER_ALIASES
            ]
            pattern = (
                re.compile(rf"\b(?:{'|'.join(re.escape(t) for t in search_terms)})\b", re.IGNORECASE)
                if search_terms
                else re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
            )
            contexts: list[str] = []
            for m in pattern.finditer(all_text):
                start = max(0, m.start() - 150)
                end = min(len(all_text), m.end() + 150)
                snippet = all_text[start:end].replace("\n", " ").strip()
                contexts.append(snippet)

            combined = " ".join(contexts[:15])
            he_count = len(re.findall(r"\b(he|his|him|himself|man|boy)\b", combined, re.IGNORECASE))
            she_count = len(re.findall(r"\b(she|her|hers|herself|woman|girl)\b", combined, re.IGNORECASE))

            dossier[cid] = {
                "current_name": name,
                "current_gender": char.gender.value if hasattr(char.gender, "value") else str(char.gender),
                "current_age": char.age_range,
                "current_description": char.voice_description,
                "pronoun_counts": {"he_him": he_count, "she_her": she_count},
                "evidence_snippets": contexts[:4],
            }
        return dossier

    def _augment_characters_with_gemini(
        self,
        registry: CharacterRegistry,
        book: ExtractedBook,
        project_dir: Path | None = None,
    ) -> CharacterRegistry:
        """Apply only configured, confidence-aware, source-grounded enrichment."""
        if self.external_validator is None or project_dir is None:
            logger.info(
                "[CharacterAnalyzer] External character augmentation is not configured."
            )
            return registry

        dossier = self._build_character_evidence_dossier(registry, book)
        if not dossier:
            return registry

        try:
            result = self.external_validator.augment_characters(
                project_dir=project_dir,
                dossier=dossier,
            )
            for cid, char_patch in result.get("accepted", {}).items():
                if cid not in registry.characters or cid == "narrator":
                    continue
                char = registry.characters[cid]
                gender_str = str(char_patch.get("gender", "")).lower()
                if char.gender == Gender.OTHER and gender_str == "male":
                    char.gender = Gender.MALE
                elif char.gender == Gender.OTHER and gender_str == "female":
                    char.gender = Gender.FEMALE

                if char_patch.get("age_range"):
                    char.age_range = char_patch["age_range"]
                if char_patch.get("voice_description"):
                    char.voice_description = char_patch["voice_description"]
                if char_patch.get("speaking_style"):
                    char.speaking_style = char_patch["speaking_style"]
                if char_patch.get("personality_traits"):
                    char.personality_traits = char_patch["personality_traits"]
                if char_patch.get("test_sentence"):
                    char.test_sentence = char_patch["test_sentence"]

            if result.get("review"):
                logger.info(
                    "[CharacterAnalyzer] Deferred %d uncertain character enrichments to review",
                    len(result["review"]),
                )
        except Exception as exc:
            logger.warning("[CharacterAnalyzer] Gemini character augmentation encountered an error: %s", exc)

        return registry

    def _write_reference_audit(
        self,
        registry: CharacterRegistry,
        book: ExtractedBook,
        reference_audit_path: Path | str,
    ) -> None:
        from shared.artifacts import atomic_write_json
        ref_text = "\n".join(book.reference_material.values()) if book.reference_material else ""
        accepted = []
        rejected = []
        try:
            prompt = (
                "AUTHOR REFERENCE:\n" + ref_text + "\n\n"
                "Extract canonical updates only for registered characters. "
                "Every patch must include confidence from 0 to 1 and an evidence "
                "string copied verbatim from AUTHOR REFERENCE. Abstain by omitting "
                "a character when evidence is absent. Direct narrative character "
                "identity remains authoritative. Registered characters: "
                + ", ".join(cid for cid in registry.characters if cid != "narrator")
            )
            raw = self.ollama.generate_json(prompt, temperature=0.0)
            patches = raw.get("patches", []) if isinstance(raw, dict) else []
            for patch in patches:
                cid = patch.get("character_id")
                evidence = " ".join(str(patch.get("evidence") or "").split())
                confidence = float(patch.get("confidence", 0.0) or 0.0)
                grounded = (
                    len(evidence) >= 12
                    and evidence.casefold()
                    in " ".join(ref_text.split()).casefold()
                )
                if (
                    cid in registry.characters
                    and cid != "narrator"
                    and confidence >= 0.9
                    and grounded
                ):
                    char = registry.characters[cid]
                    if patch.get("revised_voice_description"):
                        char.voice_description = patch["revised_voice_description"]
                    if patch.get("aliases"):
                        char.aliases = sorted(set(char.aliases + patch["aliases"]))
                    if patch.get("personality_traits"):
                        char.personality_traits = patch["personality_traits"]
                    if patch.get("speaking_style"):
                        char.speaking_style = patch["speaking_style"]
                    accepted.append(patch)
                else:
                    rejected.append({
                        **patch,
                        "reason": (
                            "unknown_or_unproven_character"
                            if cid not in registry.characters or cid == "narrator"
                            else "low_confidence_or_ungrounded_evidence"
                        ),
                    })
            audit_data = {"status": "completed", "accepted": accepted, "rejected": rejected}
        except Exception:
            logger.warning("[CharacterAnalyzer] Supplemental reference augmentation unavailable: supplement unavailable")
            audit_data = {"status": "unavailable", "accepted": [], "rejected": []}
        atomic_write_json(Path(reference_audit_path), audit_data)

    def create_joint_seed_registry(self, book: ExtractedBook) -> CharacterRegistry:
        """Create initial character registry for joint director pass."""
        narrator = Character(
            id="narrator",
            name="Narrator",
            gender=Gender.OTHER,
            age_range="adult",
            voice_description="male speaker, 40s age. low pitch, moderate volume, measured speed. Standard American accent. clear texture, high clarity, natural fluency. warm emotion, authoritative tone, thoughtful personality.",
            speaking_style="Flowing descriptive prose, unhurried",
            test_sentence="The forest was old, its roots deep in the forgotten history of the world.",
        )
        reg = CharacterRegistry(
            book_title=book.metadata.title,
            book_author=book.metadata.author,
            characters={"narrator": narrator},
        )
        return self._ensure_explicit_unnamed_speakers(reg, book)

    def finalize_joint_registry(
        self,
        registry: CharacterRegistry,
        book: ExtractedBook,
        check_callback: Callable[[], None] | None = None,
        reference_audit_path: Path | str | None = None,
        project_dir: Path | str | None = None,
    ) -> tuple[CharacterRegistry, dict[str, str]]:
        """Reconcile and consolidate discovered characters from joint director pass."""
        remap = {cid: cid for cid in registry.characters}
        reconciled_chars: dict[str, Character] = {}

        # Sort characters so longer / alias-owning characters take precedence
        sorted_chars = sorted(
            registry.characters.values(),
            key=lambda c: (c.id == "narrator", len(c.name), len(c.aliases)),
            reverse=True,
        )
        for char in sorted_chars:
            if char.id == "narrator":
                reconciled_chars["narrator"] = char
                continue
            matched = False
            for target_id, target_char in list(reconciled_chars.items()):
                if target_id == "narrator":
                    continue
                if (
                    char.name in target_char.aliases
                    or any(a.lower() == char.name.lower() for a in target_char.aliases)
                    or char.id in target_char.aliases
                ):
                    remap[char.id] = target_id
                    matched = True
                    break
            if not matched:
                reconciled_chars[char.id] = char

        reconciled = CharacterRegistry(
            book_title=registry.book_title,
            book_author=registry.book_author,
            genre=registry.genre,
            tone=registry.tone,
            characters=reconciled_chars,
        )
        reconciled = self._ensure_explicit_unnamed_speakers(reconciled, book)
        reconciled = self._augment_characters_with_gemini(
            reconciled,
            book,
            Path(project_dir) if project_dir else None,
        )
        self._assign_voice_ids(reconciled.characters)
        if reference_audit_path and getattr(book, "reference_material", None):
            self._write_reference_audit(reconciled, book, reference_audit_path)
        return reconciled, remap

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

    @staticmethod
    def _derive_character_aliases(
        char_id: str,
        name: str,
        raw_aliases: list[str],
    ) -> list[str]:
        """Derive standard aliases and nicknames from ID, name, and raw aliases."""
        aliases: list[str] = []
        seen: set[str] = set()

        def add_alias(candidate: str) -> None:
            c = candidate.strip().strip("\"'").strip()
            c_norm = c.lower().replace("_", " ").strip()
            if not c or len(c) < 2 or c_norm in seen:
                return
            if c_norm in _UNSAFE_CHARACTER_ALIASES or c_norm in {
                "mr", "mrs", "ms", "dr"
            }:
                return
            seen.add(c_norm)
            aliases.append(c)

        for a in raw_aliases:
            if isinstance(a, str):
                add_alias(a)

        name_clean = re.sub(r"\s+", " ", name).strip()
        add_alias(name_clean)

        title_stripped = re.sub(
            r"^(?:President|Senator|Lord|Lady|Mother|Father|Brother|Sister|Captain|Doctor|Dr\.|Officer|Priest|Master|King|Queen|Prince|Princess)\s+",
            "",
            name_clean,
            flags=re.IGNORECASE,
        ).strip()
        if title_stripped and title_stripped != name_clean:
            add_alias(title_stripped)

        of_match = re.search(r"\bof\s+(?:the\s+)?([A-Za-z]+)\b", name_clean, flags=re.IGNORECASE)
        if of_match:
            add_alias(of_match.group(1))

        words = [
            w for w in re.split(r"[\s_]+", name_clean)
            if len(w) >= 3
            and w.lower() not in _UNSAFE_CHARACTER_ALIASES
            and w.lower() not in {"for", "with", "from", "into"}
        ]
        if len(words) > 1:
            add_alias(words[0])
            add_alias(words[-1])

        id_words = [
            w for w in char_id.split("_")
            if len(w) >= 3
            and w.lower() not in _UNSAFE_CHARACTER_ALIASES
            and w.lower() not in {"for", "with", "from", "into"}
        ]
        for w in id_words:
            if len(w) >= 4 or w.lower() in ("dusk", "soil", "sak", "vathi", "koker"):
                add_alias(w.title())

        return aliases

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
            elif gender_str in ("female", "woman", "girl", "she", "her", "feminine", "lady", "ma'am", "mother", "daughter", "sister", "wife", "queen", "loremother") or re.search(r"\b(female|woman|girl|lady|she|her|mother|daughter)\b", gender_str):
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

            raw_aliases = char_data.get("aliases", [])
            if isinstance(raw_aliases, str):
                raw_aliases = [raw_aliases]
            elif not isinstance(raw_aliases, list):
                raw_aliases = []

            char_name = char_data.get("name", normalized_id.replace("_", " ").title())
            derived_aliases = self._derive_character_aliases(normalized_id, char_name, raw_aliases)

            characters[normalized_id] = Character(
                id=normalized_id,
                name=char_name,
                gender=gender,
                age_range=str(char_data.get("age_range", "unknown")),
                personality_traits=char_data.get("personality_traits", []),
                aliases=derived_aliases,
                voice_description=str(char_data.get("voice_description", "")),
                speaking_style=str(char_data.get("speaking_style", "")),
                test_sentence=char_data.get("test_sentence"),
                dialogue_count=dialogue_count,
            )
            logger.info(
                "[CharacterAnalyzer]   + '%s' (%s) | aliases: %s | %s | voice: %s",
                characters[normalized_id].name,
                normalized_id,
                derived_aliases,
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

        self._assign_voice_ids(characters)

        return CharacterRegistry(
            book_title=raw.get("book_title", fallback_title),
            book_author=raw.get("book_author", fallback_author),
            genre=raw.get("genre", self.genre),
            tone=raw.get("tone", ""),
            characters=characters,
        )

    def _assign_voice_ids(self, characters: dict[str, Character]) -> None:
        """Apply unique voice IDs, capping to generic archetypes only if a positive limit is configured."""
        if self.max_unique_voices and self.max_unique_voices > 0 and len(characters) > self.max_unique_voices:
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
            # NOTE: a per-gender `representatives` map used to be built here so
            # an overflow character could share a *compatible major character's*
            # voice. It was never read: the loop below assigns the dedicated
            # `minor_female`/`minor_male` archetypes (or the narrator) instead.
            # docs/architecture.md still describes the older behaviour; the
            # archetype fallback is what actually ships.
            for character_id, character in characters.items():
                if character_id in important:
                    character.voice_id = character_id
                    continue
                # Minor overflow characters fall back to generic minor archetypes, NEVER the lead protagonist
                if character.gender == Gender.FEMALE:
                    character.voice_id = "minor_female" if "minor_female" in characters else "narrator"
                elif character.gender == Gender.MALE:
                    character.voice_id = "minor_male" if "minor_male" in characters else "narrator"
                else:
                    character.voice_id = "narrator"
        else:
            for character_id, character in characters.items():
                character.voice_id = character_id

    @staticmethod
    def _ensure_explicit_unnamed_speakers(
        registry: CharacterRegistry,
        book: ExtractedBook,
    ) -> CharacterRegistry:
        """Add gendered unnamed speakers that are explicit in dialogue tags.

        This is deliberately narrow: it only reacts to a quoted utterance
        immediately followed by an unambiguous generic noun and speech verb.
        It does not infer identities or merge the role with a named character.
        """
        role_specs = {
            "boy": (
                "child_male",
                "Boy",
                Gender.MALE,
                "child",
                "male child speaker, child age. medium-high pitch, moderate volume, natural speed. clear texture, high clarity, natural fluency. curious emotion, direct tone, youthful personality.",
            ),
            "girl": (
                "child_female",
                "Girl",
                Gender.FEMALE,
                "child",
                "female child speaker, child age. high pitch, moderate volume, natural speed. clear texture, high clarity, natural fluency. curious emotion, direct tone, youthful personality.",
            ),
            "man": (
                "minor_male",
                "Unnamed Man",
                Gender.MALE,
                "adult",
                "male speaker, adult age. medium pitch, moderate volume, natural speed. clear texture, high clarity, natural fluency. neutral emotion, conversational tone, grounded personality.",
            ),
            "woman": (
                "minor_female",
                "Unnamed Woman",
                Gender.FEMALE,
                "adult",
                "female speaker, adult age. medium pitch, moderate volume, natural speed. clear texture, high clarity, natural fluency. neutral emotion, conversational tone, grounded personality.",
            ),
        }
        speech_verbs = (
            r"said|asked|replied|whispered|shouted|murmured|exclaimed|"
            r"continued|agreed|added|called|demanded|warned|answered|cried"
        )
        pattern = re.compile(
            r"(?:\"[^\"\n]+\"|\u201c[^\u201d\n]+\u201d)\s*"
            r"(?:,\s*)?(?:the|a)\s+(boy|girl|man|woman)\s+(?:"
            + speech_verbs
            + r")\b",
            re.IGNORECASE,
        )
        counts: dict[str, int] = {}
        for chapter in book.chapters:
            for match in pattern.finditer(chapter.text):
                noun = match.group(1).casefold()
                counts[noun] = counts.get(noun, 0) + 1

        for noun, count in counts.items():
            role_id, name, gender, age_range, description = role_specs[noun]
            existing = registry.characters.get(role_id)
            if existing is None:
                existing = next(
                    (
                        candidate
                        for candidate in registry.characters.values()
                        if candidate.gender == gender
                        and noun
                        in {
                            candidate.id.casefold(),
                            candidate.name.casefold(),
                            *(alias.casefold() for alias in candidate.aliases),
                        }
                    ),
                    None,
                )
            if existing is not None:
                existing.dialogue_count = max(existing.dialogue_count, count)
                continue
            registry.characters[role_id] = Character(
                id=role_id,
                name=name,
                gender=gender,
                age_range=age_range,
                personality_traits=["unnamed", "source-explicit"],
                voice_description=description,
                speaking_style="Natural dialogue matching the source context",
                dialogue_count=count,
                test_sentence=(
                    "I know what I saw, and I can explain it if you listen."
                ),
            )
            logger.warning(
                "[CharacterAnalyzer] Added explicit unnamed speaker '%s' from %d source tag(s)",
                role_id,
                count,
            )
        return registry

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
                    count1 = cinfo1.get("dialogue_count", 0) + cinfo1.get("mention_count", 0)
                    count2 = cinfo2.get("dialogue_count", 0) + cinfo2.get("mention_count", 0)
                    if count1 > count2:
                        target_id, variant_id = cid1, cid2
                        target_info, variant_info = cinfo1, cinfo2
                    elif count2 > count1:
                        target_id, variant_id = cid2, cid1
                        target_info, variant_info = cinfo2, cinfo1
                    elif (len(cid1.split("_")), len(cid1)) >= (
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
                    target_info["aliases"] = sorted(existing_aliases)
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
        def _distinctive_tokens(cid: str, data: dict[str, Any]) -> set[str]:
            tokens: set[str] = set()
            name_words = re.split(r"[\s_]+", str(data.get("name", cid)))
            for w in name_words:
                w_clean = w.lower().strip("'\".,;:-")
                if (
                    len(w_clean) >= 3
                    and w_clean not in _UNSAFE_CHARACTER_ALIASES
                    and w_clean not in _GENERIC_ROLE_DESCRIPTORS
                ):
                    tokens.add(w_clean)
            for a in data.get("aliases", []):
                for w in re.split(r"[\s_]+", str(a)):
                    w_clean = w.lower().strip("'\".,;:-")
                    if (
                        len(w_clean) >= 3
                        and w_clean not in _UNSAFE_CHARACTER_ALIASES
                        and w_clean not in _GENERIC_ROLE_DESCRIPTORS
                    ):
                        tokens.add(w_clean)
            return tokens

        ids = sorted(characters)
        candidate_pairs: list[tuple[str, str]] = []
        tokens_by_id = {cid: _distinctive_tokens(cid, characters[cid]) for cid in ids}

        for index, left in enumerate(ids):
            if left == "narrator":
                continue
            left_parts = left.split("_")
            left_tokens = tokens_by_id[left]
            for right in ids[index + 1:]:
                if right == "narrator":
                    continue
                right_parts = right.split("_")
                right_tokens = tokens_by_id[right]

                # Criterion 1: Suffix ID match (e.g. pwent <-> thibbledorf_pwent)
                suffix_match = (
                    (
                        len(left_parts) < len(right_parts)
                        and right_parts[-len(left_parts):] == left_parts
                    )
                    or (
                        len(right_parts) < len(left_parts)
                        and left_parts[-len(right_parts):] == right_parts
                    )
                )

                # Criterion 2: Distinctive non-generic token overlap (e.g. "Sixth" in Sixth of Dusk <-> "Sixth" in Drominadian)
                shared_tokens = left_tokens & right_tokens

                if suffix_match or shared_tokens:
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
            for a in characters[left].get("aliases", []):
                a_clean = str(a).strip()
                if (
                    len(a_clean) >= 3
                    and a_clean.lower() not in _UNSAFE_CHARACTER_ALIASES
                    and a_clean.lower() not in _GENERIC_ROLE_DESCRIPTORS
                ):
                    terms.add(a_clean)
            for a in characters[right].get("aliases", []):
                a_clean = str(a).strip()
                if (
                    len(a_clean) >= 3
                    and a_clean.lower() not in _UNSAFE_CHARACTER_ALIASES
                    and a_clean.lower() not in _GENERIC_ROLE_DESCRIPTORS
                ):
                    terms.add(a_clean)
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
            count_left = characters[left].get("dialogue_count", 0) + characters[left].get("mention_count", 0)
            count_right = characters[right].get("dialogue_count", 0) + characters[right].get("mention_count", 0)
            if count_left > count_right:
                target, variant = left, right
            elif count_right > count_left:
                target, variant = right, left
            else:
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
