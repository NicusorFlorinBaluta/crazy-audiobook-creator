# Voice Design & Character Voices

## Overview

This document explains how the pipeline creates unique, consistent voices for each character in a fantasy novel using Qwen3-TTS's Voice Design feature.

---

## The Voice Design → Clone Workflow

This is the core technique that ensures every character sounds the same throughout a 10+ hour audiobook.

```
Step 1: LLM Analyzes Book
        ↓
Step 2: LLM Writes Voice Description per Character
        "Young male tenor, late teens, quick and clever..."
        ↓
Step 3: Qwen3-TTS VoiceDesign Mode
        Generates a 10-second reference clip (.wav)
        ↓
Step 4: Reference Clip Saved to Voice Library
        voice_library/project_name/kvothe.wav
        ↓
Step 5: All Future Lines by This Character
        Use the saved .wav as voice reference
        Qwen3-TTS clones from this reference
        Emotion varies per line, voice stays constant
```

### Why Not Just Use VoiceDesign Every Time?

VoiceDesign is **non-deterministic**. If you call it twice with the same description, you get slightly different voices. Over 3,000+ segments, these tiny differences accumulate into an inconsistent, unsettling listening experience.

By generating ONCE and cloning from that saved reference, every line sounds like the same "person" — just with different emotions, speed, and intensity. Exactly like a real voice actor.

---

## Writing Effective Voice Descriptions

The LLM generates voice descriptions during Pass 1, but understanding what makes a good description helps you troubleshoot or override.

### Anatomy of a Good Description

```
"A warm, mature male baritone, early 40s, with a measured 
storytelling cadence. Rich and clear, slight British RP 
pronunciation. Natural gravitas without being overly dramatic."
```

**Key dimensions**:
1. **Gender & Age**: "young female, early 20s" or "elderly male, 70s"
2. **Pitch/Register**: "deep baritone", "high soprano", "medium tenor"
3. **Pace/Rhythm**: "measured and deliberate", "quick and energetic", "slow and ponderous"
4. **Timbre/Quality**: "gravelly", "silky smooth", "raspy", "bell-like clarity"
5. **Accent/Pronunciation**: "British RP", "American neutral", "slight roughness"
6. **Emotional Baseline**: "warm and kind", "cold and calculating", "nervous energy"

### Descriptions by Fantasy Archetype

| Archetype | Example Description |
|-----------|-------------------|
| **Wise Mentor** | "Deep male baritone, 60s, slow and deliberate with gravitas. Warm but authoritative, like an experienced professor. Clear enunciation, measured pauses." |
| **Young Hero** | "Male tenor, late teens, energetic and curious. Quick delivery that speeds up when excited. Occasionally vulnerable, voice cracking with emotion." |
| **Mysterious Sorceress** | "Female contralto, ageless, low and melodic. Unhurried, each word chosen carefully. A hint of amusement in the undertones, as if she knows secrets." |
| **Gruff Warrior** | "Male bass, 40s, rough and gravelly. Clipped sentences, impatient delivery. Speaks as if every word costs energy. No-nonsense." |
| **Noble Queen** | "Female mezzo-soprano, 40s, composed and regal. Precise pronunciation, controlled pace. Warmth underneath formality. Commands without raising voice." |
| **Comic Relief** | "Male tenor, 30s, animated and expressive. Fast-talking with infectious energy. Dramatic pauses for comedic effect. Slightly higher pitch when excited." |
| **Dark Villain** | "Male low baritone, age indeterminate, smooth and calculated. Quietly menacing — never needs to shout. Savors words like fine wine. Slight sibilance." |
| **Innocent Child** | "Female soprano, 10-12, bright and clear. Earnest delivery with occasional breathlessness. Simple sentence patterns, pure and unguarded." |

### What NOT to Include

- ❌ Real person names: "sounds like Morgan Freeman" (use archetypes instead)
- ❌ Technical jargon: "formant frequency at 500Hz" (the model doesn't understand this)
- ❌ Negative descriptions: "doesn't sound old" (describe what it DOES sound like)
- ❌ Extremely long descriptions: Keep under 50 words

---

## Voice Library Structure

```
voice_library/
└── name-of-the-wind/
    ├── narrator.wav          # 10-second reference clip
    ├── kvothe_young.wav
    ├── kvothe_old.wav
    ├── denna.wav
    ├── chronicler.wav
    ├── bast.wav
    ├── ambrose.wav
    ├── ...
    └── voices.json           # Voice registry with descriptions
```

### voices.json
```json
{
  "project_id": "name-of-the-wind",
  "created_at": "2026-07-13T20:00:00Z",
  "voices": {
    "narrator": {
      "file": "narrator.wav",
      "description": "Warm mature male baritone...",
      "gender": "male",
      "generated_at": "2026-07-13T20:00:05Z",
      "duration_seconds": 10.2,
      "sample_rate": 24000
    }
  }
}
```

---

## Per-Line Emotion Instructions

While the voice stays consistent (via reference clip), each line gets unique emotion instructions:

### How Emotion Instructions Work

```python
# Voice reference = WHO the character sounds like (constant)
# Emotion instruction = HOW they deliver this specific line (varies)

audio = qwen3_tts.generate(
    text="You should be careful what questions you ask.",
    voice_reference="voice_library/kvothe_old.wav",  # Always the same
    instruction="Speak with quiet intensity and a warning undertone"  # Changes per line
)
```

### Emotion Instruction Examples

| Scene Context | Text | Emotion Instruction |
|---------------|------|-------------------|
| Tense confrontation | "Get out." | "Speak with cold, barely controlled fury. Quiet but cutting." |
| Romantic scene | "I've been looking for you." | "Speak warmly and softly, with gentle affection and slight nervousness." |
| Discovery | "It's real. It's actually real." | "Speak with breathless wonder, building from disbelief to excitement." |
| Grief | "She's gone." | "Speak with hollow, numb grief. Flat delivery, barely above a whisper." |
| Humor | "Well, that went according to plan." | "Speak with dry sarcasm and self-deprecating amusement." |
| Battle | "Hold the line!" | "Shout with fierce determination and urgency. Commanding." |

---

## Handling Many Characters

Fantasy novels can have dozens of speaking characters. Strategy for managing voices:

### Tier System

| Tier | Criteria | Voice Treatment |
|------|----------|----------------|
| **Major** | 50+ lines of dialogue | Full custom voice description |
| **Supporting** | 10-50 lines | Shorter voice description |
| **Minor** | 3-10 lines | Gender-matched generic voice |
| **Walk-on** | 1-2 lines | Narrator voice with character emotion |

### Maximum Voices

Limit to **15-20 unique voices** per book. Beyond this:
- Listeners can't distinguish so many voices
- Each additional voice reference uses ~500KB of disk
- Minor characters sharing a generic voice is fine — real audiobook narrators do this too

### Generic Voice Pool

For minor characters, maintain a small pool of generic voices:
```
generic_voices/
├── male_young.wav      # Young male, neutral
├── male_middle.wav     # Middle-aged male, neutral
├── male_old.wav        # Elderly male, neutral
├── female_young.wav    # Young female, neutral
├── female_middle.wav   # Middle-aged female, neutral
└── female_old.wav      # Elderly female, neutral
```

Minor characters are assigned from this pool based on gender and age:
- Guard #1 (male, 30s) → `male_middle.wav`
- Innkeeper's wife (female, 50s) → `female_middle.wav`

---

## Voice Regeneration

If a generated voice doesn't sound right for a character:

### Via Dashboard
1. Navigate to Voice Library → select character
2. Listen to the reference clip
3. Edit the voice description
4. Click "Regenerate Voice"
5. Listen to the new clip
6. If satisfied, confirm → all future generations use the new voice
7. If existing audio was generated with the old voice, re-render those segments

### Via API
```
POST /voices/regenerate
{
  "project_id": "name-of-the-wind",
  "character_id": "kvothe",
  "voice_description": "Updated description..."
}
```

### When to Regenerate
- Voice doesn't match the character's described personality
- Voice sounds too similar to another character
- Voice has unwanted artifacts or accents
- You want to experiment with different interpretations

---

## Tips for Fantasy Audiobooks

### Invented Languages
If the book contains phrases in invented languages (Elvish, Dragon-speech):
- The TTS will attempt to pronounce them phonetically
- Results may vary — unusual phoneme combinations can trip up the model
- Consider adding pronunciation hints in the script: `"Ainulindalë (eye-noo-LIN-da-lay)"`

### Songs and Poetry
Fantasy books often contain songs or poetry:
- These should be spoken, not sung (TTS can't sing)
- Use a slower speed (0.8-0.85x) with a more rhythmic emotion instruction
- Example instruction: "Speak in a lyrical, rhythmic cadence, as if reciting poetry. Measured pace with emphasis on rhyme and meter."

### Battle Scenes
- Multiple characters shouting requires strong voice differentiation
- Use more aggressive emotion instructions: "shout", "roar", "bark orders"
- Shorter segments for rapid dialogue exchanges
- Faster speed (1.1-1.15x) for urgent moments

### Narrated Internal Monologue
Many fantasy authors use italicized internal thoughts:
- Assign to the thinking character (not narrator)
- Emotion instruction: "Speak as internal thought, slightly softer and more intimate, as if talking to oneself"
- Slightly slower speed (0.9x)
