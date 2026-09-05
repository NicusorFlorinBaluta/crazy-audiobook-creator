You are an expert audiobook director preparing a novel for multi-voice narration. Your task is to analyze this book and create a character registry with voice descriptions for text-to-speech generation.

## Instructions

1. Read the following text carefully
2. Identify ALL speaking entities (anyone or anything that actually has dialogue)
   - Recognize straight/curly quotes, typographic single quotes, and em-dash dialogue
   - Do not exclude an animal, ship, place, AI, or object merely because of its type
   - Do not infer an entity's type or ability to speak from its name
   - A named or personified place/object is not a speaking entity unless the
     text explicitly attributes spoken dialogue to it; descriptions,
     invocations, thoughts, and figurative personification do not count
   - Animals that only make animal noises (e.g., squawking, barking, chirping) 
     without human-intelligible spoken dialogue MUST NOT be included
3. For each character, determine:
   - Their canonical **gender** (`male` or `female`): Carefully check surrounding narrative pronouns (`he`, `him`, `his`, `himself`, `man`, `boy` vs `she`, `her`, `hers`, `herself`, `woman`, `girl`). Do NOT default to `other` for named characters if pronouns exist in the surrounding text; use `other` ONLY for true non-gendered collective entities, swarms, or non-human deities.
   - Their approximate age, importance, and key personality traits
   - A detailed voice description suitable for voice synthesis
   - ACCURATELY count the number of spoken dialogue lines they have in this text
   - Extract or invent a highly representative line of dialogue for their `test_sentence`. **CRITICAL: The sentence MUST be at least 15 words long.** For characters with very short lines, you must invent a longer sentence or combine multiple lines that perfectly captures their personality and tone.
4. Also create a narrator voice that fits the book's genre and tone. **CRITICAL**: The `narrator` entry is strictly the audiobook reader role for unquoted narrative prose. NEVER add in-world character names or aliases to `narrator`. If the book is written in the first person or includes POV journal entries/reflections (e.g. Breezy, Katniss, Percy), the protagonist MUST have their own distinct character card (e.g. `breezy`) for their spoken dialogue turns.
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
      "importance": "major",
      "personality_traits": ["trait1", "trait2"],
      "voice_description": "detailed voice description for TTS",
      "speaking_style": "how the narrator typically speaks",
      "test_sentence": "A highly representative sentence showcasing the narrator's pacing, tone, and style."
    }},
    "character_id": {{
      "name": "Character Display Name",
      "gender": "male|female|other",
      "age_range": "string",
      "importance": "major|minor",
      "personality_traits": ["trait1", "trait2"],
      "voice_description": "detailed voice description for TTS",
      "speaking_style": "how this character typically speaks",
      "test_sentence": "A highly representative line of dialogue (extracted or invented) showcasing their personality and tone.",
      "dialogue_count": 0
    }}
  }}
}}

## Book Text

{book_text}
