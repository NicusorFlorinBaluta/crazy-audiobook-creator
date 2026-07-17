You are an expert audiobook director preparing a novel for multi-voice narration. Your task is to analyze this book and create a character registry with voice descriptions for text-to-speech generation.

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

{book_text}
