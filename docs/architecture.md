# Architecture Guide

## Overview

The Crazy Audiobook Creator is a **two-machine pipeline** that splits the workload between a "Brain" (Windows PC with AMD 7900 XTX) and a "Voice" (Ubuntu PC with NVIDIA RTX 2080 Super). This separation exploits each machine's strengths:

- **Windows (24GB VRAM)**: Runs a large LLM that analyzes books, detects characters, and generates emotionally-tagged scripts
- **Ubuntu (8GB VRAM + CUDA)**: Runs Qwen3-TTS for high-quality speech synthesis with native GPU acceleration

The machines communicate over the local network via REST APIs and WebSocket connections.

---

## Pipeline Stages

The pipeline has 9 stages, executing sequentially with automatic retry loops for quality assurance.

```
EPUB → ① Extract → ② Script → ③ Bootstrap Voices → ④ Generate Audio
                                                          ↓
                          M4B ← ⑦ Export ← ⑥ Master ← ⑤ Validate
                                                     ↗ (retry if failed)
```

### Stage ① — Text Extraction (Windows)

**Purpose**: Convert EPUB files into clean, structured text organized by chapters.

**Process**:
1. Parse EPUB using `ebooklib` to extract HTML content
2. Process HTML with `BeautifulSoup4` to strip tags and extract clean text
3. Detect chapter boundaries using heading patterns (h1/h2 tags, "Chapter X" patterns, numbered sections)
4. Handle fantasy-specific content:
   - Skip/separate maps, appendices, glossaries, dramatis personae
   - Preserve special names, places, and invented words
   - Handle epigraphs and in-chapter poetry/songs
5. Clean text artifacts: page numbers, headers/footers, ligatures, smart quotes
6. Output: Array of `Chapter` objects, each containing title and clean text

**Output Schema**:
```json
{
  "book": {
    "title": "The Name of the Wind",
    "author": "Patrick Rothfuss",
    "total_chapters": 92
  },
  "chapters": [
    {
      "number": 1,
      "title": "A Place for Demons",
      "text": "It was night again. The Waystone Inn lay in silence..."
    }
  ]
}
```

**Key Decisions**:
- We preserve paragraph structure (double newlines) since it affects pacing
- Chapter detection uses a priority system: explicit HTML markers > heading tags > text patterns
- Tables of contents and front matter are stripped automatically
- If chapter detection fails, the entire book is treated as a single chapter with a warning

---

### Stage ② — LLM Script Director (Windows)

**Purpose**: Transform raw chapter text into a structured audiobook script with speaker attribution, emotion tags, and pacing instructions.

**LLM**: Qwen3 32B Q4_K_M via Ollama (Vulkan backend for AMD GPU)

**Two-Pass Analysis**:

#### Pass 1 — Character & World Analysis (once per book)

The LLM reads the full book text (or chapter summaries if context window is limited) to build a comprehensive character registry.

**Outputs per character**:
- `id`: Unique identifier (lowercase, underscore-separated)
- `name`: Display name
- `gender`: male/female/other
- `age_range`: Approximate age bracket
- `personality_traits`: Key personality characteristics
- `voice_description`: Detailed natural language description for Qwen3-TTS Voice Design
- `speaking_style`: How the character typically speaks (formal, casual, clipped, etc.)

**Narrator voice**:
- Always generated as a separate "character"
- Voice description crafted to suit the book's genre and tone
- Fantasy books typically get a warm, measured, storyteller-type narrator

**Example Output**:
```json
{
  "characters": {
    "narrator": {
      "name": "Narrator",
      "gender": "male",
      "age_range": "40s",
      "personality_traits": ["wise", "reflective", "measured"],
      "voice_description": "A warm, mature male voice, early 40s, with a rich baritone quality. Measured pace with a natural storyteller's cadence. Slight gravitas but not overly dramatic. Clear British RP pronunciation.",
      "speaking_style": "Descriptive, flowing prose with natural pauses at paragraph breaks"
    },
    "kvothe": {
      "name": "Kvothe",
      "gender": "male",
      "age_range": "late teens to early 20s",
      "personality_traits": ["clever", "passionate", "arrogant", "vulnerable"],
      "voice_description": "A young male voice, late teens to early 20s. Quick and clever-sounding with a slightly musical quality. Medium pitch, confident delivery that occasionally cracks with vulnerability. No strong accent.",
      "speaking_style": "Articulate, sometimes rambling when excited, precise when angry"
    }
  }
}
```

#### Pass 2 — Line-by-Line Script Generation (per chapter)

For each chapter, the LLM processes the text with a **10-paragraph sliding context window** to maintain emotional awareness.

**For each text segment, the LLM determines**:
- `speaker`: Who is speaking (narrator for non-dialogue, character ID for dialogue)
- `text`: The exact text to speak (with dialogue attribution stripped)
- `emotion`: Current emotional state described in natural language
- `speed`: Delivery speed multiplier (0.8x for slow/dramatic, 1.0x for normal, 1.2x for excited/fast)
- `pause_before_ms`: Silence before this segment (0-2000ms)
- `pause_after_ms`: Silence after this segment (0-2000ms)

**Context Window Strategy**:
The LLM sees the current paragraph plus 5 paragraphs before and 5 after. This is critical because:
- "Don't go" is desperate if the previous paragraph describes a lover leaving
- "Don't go" is menacing if the previous paragraph describes a villain cornering someone
- Emotion isn't in the words alone — it's in the narrative context

**Dialogue Detection Rules**:
1. Text within quotation marks → dialogue (assigned to detected speaker)
2. Text with dialogue attribution ("he said", "she whispered") → dialogue
3. Internal monologue (often in italics) → character with "internal, thoughtful" emotion
4. Everything else → narrator

**Segment Granularity**:
- Each dialogue line becomes one segment
- Narration paragraphs become one segment (unless very long, then split at sentence boundaries)
- Keep segments between 1-4 sentences for optimal TTS quality
- Never split mid-sentence

---

### Stage ③ — Voice Bootstrapping (Ubuntu)

**Purpose**: Generate a unique, consistent voice reference clip for each character using Qwen3-TTS Voice Design mode.

**Process**:
1. Receive character registry from Stage ②
2. For each character, send their `voice_description` to Qwen3-TTS in VoiceDesign mode
3. Generate a 10-second reference clip of the voice speaking a neutral test sentence
4. Save the clip to the project's Voice Library as `{character_id}.wav`
5. These clips are the "voice fingerprint" — every subsequent generation for that character uses this exact clip

**Voice Design → Clone Workflow**:
```
Text Description ──→ Qwen3-TTS VoiceDesign ──→ Reference .wav ──→ Saved to Library
                                                       ↓
                     All future generations use this clip as reference
                     via Qwen3-TTS Base model (voice cloning mode)
```

**Why This Ensures Consistency**:
- VoiceDesign is non-deterministic — running it twice produces slightly different voices
- By generating ONCE and saving, every line by that character uses the exact same voice identity
- Emotion and speed vary per line, but the underlying voice timbre stays constant
- This is the same technique professional audiobook studios use with real voice actors

**Test Sentences for Voice Generation**:
- Male: "The ancient tower stood against the darkening sky, its stones weathered by centuries of wind and rain."
- Female: "She walked through the moonlit garden, her footsteps barely disturbing the fallen leaves."
- These are chosen to exercise a range of phonemes while being emotionally neutral.

---

### Stage ④ — TTS Generation (Ubuntu)

**Purpose**: Generate audio for every line in the script using Qwen3-TTS with character voice references and per-line emotion instructions.

**Engine**: Qwen3-TTS 1.7B (~6-8GB VRAM on RTX 2080 Super)

**Per-Line Generation**:
```python
# Pseudocode for each line
audio = qwen3_tts.generate(
    text=line.text,
    voice_reference=voice_library.get(line.speaker),  # Character's saved .wav
    instruction=f"Speak with {line.emotion} emotion, at {line.speed}x speed",
    language="en"
)
save(audio, f"chapter_{ch}_line_{n}.wav")
```

**Emotion Instruction Format**:
Qwen3-TTS accepts natural language instructions. Our LLM generates these as emotion descriptions that map to TTS instructions:

| Script Emotion | TTS Instruction |
|---------------|-----------------|
| `"contemplative, somber"` | `"Speak in a contemplative, somber tone with measured pacing"` |
| `"fearful, whispering"` | `"Speak fearfully in a hushed whisper"` |
| `"angry, explosive"` | `"Speak with explosive anger, raised voice, sharp consonants"` |
| `"warm, gentle"` | `"Speak warmly and gently, with a soft, comforting quality"` |

**Batching Strategy**:
- Process all lines for a chapter sequentially
- Keep the model loaded in VRAM throughout (no swapping needed — single engine)
- Generate ~1-5 seconds of audio per line
- Average novel chapter (3000-5000 words) ≈ 200-400 segments ≈ 15-30 minutes of audio
- Generation speed: ~1-3x real-time on RTX 2080 Super

**Error Handling**:
- If generation fails (OOM, timeout), retry with smaller chunk
- If text contains unusual characters, normalize before sending
- Log all generation parameters for reproducibility

---

### Stage ⑤ — AI Quality Validator (Ubuntu)

**Purpose**: Automatically validate every generated audio segment without human intervention.

**Three-Layer Validation**:

#### Layer 1 — Intelligibility Check (Whisper STT + WER)
1. Run `faster-whisper` (medium model, ~2GB) on the generated audio
2. Transcribe audio back to text
3. Normalize both original and transcribed text (lowercase, strip punctuation)
4. Calculate Word Error Rate (WER) using `jiwer`
5. **Pass**: WER < 5%
6. **Fail**: WER ≥ 5% → regenerate

**Why 5% threshold**: Whisper itself has ~2% baseline error. A TTS segment with clear pronunciation should be transcribed almost perfectly. WER > 5% indicates the TTS garbled words, skipped phrases, or hallucinated.

#### Layer 2 — Audio Artifact Detection
Analyze the raw audio signal for technical issues:
- **Clipping**: Check if signal exceeds ±1.0 (or -0.5 dBFS peak)
- **Silence gaps**: Detect unnatural silences > 3 seconds within a segment
- **Noise floor**: Check that silence portions are below -50 dB
- **Duration sanity**: Compare actual duration vs expected (based on word count × average speaking rate)
  - Expected rate: ~150 words per minute (adjusted by speed parameter)
  - **Pass**: Actual duration within ±30% of expected
  - **Fail**: Duration wildly off → regenerate

#### Layer 3 — Quality Score Aggregation
Each segment receives a composite quality score:
```
quality_score = (1 - WER) * 0.6 + artifact_score * 0.3 + duration_score * 0.1
```
- Logged to SQLite for the quality dashboard
- Segments below 0.7 score are flagged for optional manual review

**Retry Logic**:
```
attempt = 1
while attempt <= 3:
    audio = generate(line)
    if validate(audio).passed:
        save(audio)
        break
    attempt += 1
if attempt > 3:
    save(audio)  # Save best attempt
    flag_for_review(line)  # Mark in dashboard
    log_warning(f"Line {line.id} failed validation after 3 attempts")
```

**VRAM Management**:
- TTS and Whisper are run sequentially, not concurrently
- Option A: Generate all chapter segments → validate all (batch mode, recommended)
- Option B: Generate one → validate one → next (streaming mode, slower but catches issues early)
- Default: Batch mode per chapter

---

### Stage ⑥ — Audio Mastering (Ubuntu)

**Purpose**: Assemble individual segments into polished chapter-length audio files.

**Process**:

#### Step 1 — Concatenation with Silence
- Insert configured `pause_before_ms` and `pause_after_ms` between segments
- Add chapter-start silence: 1000ms
- Add chapter-end silence: 2000ms

#### Step 2 — Cross-fading
- Apply 25-50ms cross-fade between adjacent segments
- This prevents audible "clicks" at segment boundaries
- Use raised-cosine fade curve for smoothness

#### Step 3 — Loudness Normalization
- Target: **-19 LUFS** (audiobook standard, between ACX's -18 and -23 range)
- Use integrated loudness measurement (full chapter)
- Apply gain to reach target
- Library: `pyloudnorm`

#### Step 4 — Peak Limiting
- True peak limit: **-1.0 dBTP**
- Prevents digital clipping on any playback system
- Use FFmpeg's `loudnorm` filter or `pyloudnorm`

#### Step 5 — Noise Gate
- Threshold: -50 dB
- Attack: 5ms, Release: 50ms
- Cleans up any low-level hiss or model artifacts during pauses

#### Step 6 — Sample Rate & Format
- Intermediate: 24kHz WAV (Qwen3-TTS native output rate)
- Upsample to 44.1kHz for final output (audiobook standard)
- Bit depth: 16-bit

**Output**: One WAV file per chapter, named `chapter_001.wav`, `chapter_002.wav`, etc.

---

### Stage ⑦ — M4B Export (Ubuntu)

**Purpose**: Package all chapter audio into a single, chaptered M4B audiobook file.

**M4B Format**:
- Container: MP4/M4A with `.m4b` extension
- Codec: AAC-LC at 64-128 kbps (mono) — standard for audiobooks
- Chapters: Embedded chapter markers with titles
- Metadata: Title, Author, Narrator, Genre, Year, Description
- Cover art: Embedded as JPEG/PNG (if available in EPUB or provided)

**Process**:
1. Encode each chapter WAV → AAC using FFmpeg
2. Concatenate chapters into single M4B with chapter markers
3. Embed metadata from the original EPUB + project config
4. Embed cover art if available
5. Verify playback in standard players

**FFmpeg Command (simplified)**:
```bash
ffmpeg -f concat -i chapters.txt \
  -c:a aac -b:a 128k -ar 44100 -ac 1 \
  -metadata title="Book Title" \
  -metadata artist="Author Name" \
  output.m4b
```

**Chapter markers** are written as an FFmpeg metadata file:
```ini
;FFMETADATA1
[CHAPTER]
TIMEBASE=1/1000
START=0
END=1834000
title=Chapter 1: A Place for Demons

[CHAPTER]
TIMEBASE=1/1000
START=1834000
END=3421000
title=Chapter 2: A Beautiful Day
```

---

### Stage ⑧ — Web Dashboard (Windows)

**Purpose**: Provide a monitoring interface for the pipeline with the ability to review and override.

**The dashboard is NOT required for the pipeline to run** — the pipeline is fully automated. The dashboard is for monitoring, reviewing quality reports, and making manual corrections when needed.

**Features**:

| Feature | Description |
|---------|-------------|
| **Project Manager** | Create projects, import EPUBs, configure settings |
| **Pipeline Monitor** | Real-time progress: current chapter, current line, ETA |
| **Script Viewer** | Color-coded by speaker, with emotion/speed annotations |
| **Quality Report** | WER scores per segment, flagged segments, overall stats |
| **Voice Library** | Listen to character reference clips, regenerate if needed |
| **Manual Override** | Re-trigger generation for specific segments with tweaked params |
| **Export History** | Past projects with download links and stats |

**Technology**: FastAPI backend serving a vanilla HTML/CSS/JS frontend. WebSocket for real-time updates from the Ubuntu machine.

---

## Data Flow

```
Windows                              Network                    Ubuntu
────────                              ───────                    ──────

EPUB file
    ↓
Text Extraction
    ↓
Chapter JSON
    ↓
LLM Analysis ───→ Character Registry ──→ POST /voices/bootstrap
                                                    ↓
                                         Voice Design generation
                                                    ↓
                                         Voice Library (.wav files)
    ↓
LLM Script Gen ──→ Script JSON ─────────→ POST /generate/chapter
                                                    ↓
                                         TTS generation (per line)
                                                    ↓
                                         Whisper validation
                                                    ↓
                                         Retry loop (if needed)
                                                    ↓
                                         Audio mastering
                                                    ↓
                                         M4B export
                                                    ↓
Dashboard ←──── WebSocket status ←──────── Progress updates
    ↓
GET /download/audiobook ←───────────────── Final .m4b file
```

---

## Voice Consistency Model

This is the most critical architectural decision. Here's why we use a single engine with the Voice Design → Clone pattern:

### The Problem
If you generate Character A's voice fresh for every line, Qwen3-TTS might produce slightly different timbres each time (it's non-deterministic). Over 10+ hours of audio, these small variations accumulate and the character sounds inconsistent.

### The Solution
```
                      ┌─────────────────────────────────┐
                      │       Voice Library              │
                      │                                  │
  Voice Description ──→  VoiceDesign ──→ narrator.wav   │
  "warm male, 40s"   │  (ONE TIME)      (10 seconds)   │
                      │                                  │
                      │  Every line by "narrator":       │
                      │  generate(text, ref=narrator.wav) │
                      │  ↑ Same voice, different emotion │
                      └─────────────────────────────────┘
```

- **Voice identity** = locked to the reference clip (generated once, reused forever)
- **Emotion** = varies per line via instruction parameter
- **Speed/pacing** = varies per line via speed parameter
- **Result**: Same person, different performances. Exactly like a real voice actor.

### Why Not Multiple Engines?
Even if Engine A and Engine B both produce great audio independently, they have fundamentally different acoustic signatures. Mixing them means:
- Different room acoustics
- Different formant characteristics
- Different noise profiles
- Different prosody patterns

A listener would subconsciously notice — it's like cutting between two different recording studios mid-conversation.

---

## Error Recovery & Resilience

### Pipeline State Persistence
The pipeline's state is persisted to SQLite after every stage completion:
```
project_state = {
    "stage": "tts_generation",
    "chapter": 15,
    "line": 247,
    "total_lines": 3850,
    "started_at": "2026-07-13T22:30:00Z",
    "voice_library": "bootstrapped",
    "completed_chapters": [1, 2, ..., 14]
}
```

If the pipeline crashes or is interrupted:
- Restart picks up from the last completed segment
- Already-generated audio is not regenerated
- Voice library is preserved across restarts

### Network Failure
- REST API calls have 30-second timeout with 3 retries
- If Ubuntu machine is unreachable, pipeline pauses and retries every 60 seconds
- WebSocket reconnects automatically

### GPU OOM
- If Qwen3-TTS hits OOM on a long segment, the segment is split at the nearest sentence boundary and retried
- If Whisper hits OOM, validation falls back to CPU (slower but functional)

---

## Performance Estimates

Based on Qwen3-TTS 1.7B benchmarks on RTX 2080 Super (8GB):

| Metric | Estimate |
|--------|----------|
| TTS generation speed | ~1-3x real-time |
| Whisper validation speed | ~10-30x real-time |
| Audio mastering speed | ~50-100x real-time |
| Full novel (80,000 words, ~10 hrs audio) | ~5-15 hours total |
| Short story (10,000 words, ~1.5 hrs audio) | ~30-90 minutes total |

**Bottleneck**: TTS generation is by far the slowest stage. All other stages are negligible in comparison.

**LLM script generation** (on Windows): Qwen3 32B Q4 processes ~20-40 tokens/second on 7900 XTX. A full novel (~100K tokens) takes ~30-60 minutes for the full script.

---

## Configuration

All settings are configurable via YAML files:

### `brain/config.yaml`
```yaml
ollama:
  host: "http://localhost:11434"
  model: "qwen3:32b"
  context_window: 10  # paragraphs before/after for emotion tagging

ubuntu:
  host: "http://192.168.1.XXX:8100"  # Ubuntu machine IP
  timeout: 30
  retries: 3

extraction:
  skip_toc: true
  skip_appendices: true
  min_chapter_words: 100

script:
  max_segment_sentences: 4
  default_speed: 1.0
  narrator_pause_ms: 500
  dialogue_pause_ms: 300
  chapter_start_pause_ms: 1000
  chapter_end_pause_ms: 2000

dashboard:
  port: 8000
  host: "0.0.0.0"
```

### `voice/config.yaml`
```yaml
tts:
  model: "Qwen/Qwen3-TTS-1.7B"
  device: "cuda"
  dtype: "float16"
  sample_rate: 24000

validation:
  whisper_model: "medium"  # or "large-v3" for higher accuracy
  wer_threshold: 0.05
  max_retries: 3
  artifact_noise_threshold: -50  # dB
  duration_tolerance: 0.3  # ±30%

mastering:
  target_lufs: -19
  peak_limit_dbfs: -1.0
  crossfade_ms: 30
  noise_gate_threshold: -50  # dB
  output_sample_rate: 44100

export:
  codec: "aac"
  bitrate: "128k"
  channels: 1  # mono (audiobook standard)

server:
  port: 8100
  host: "0.0.0.0"
```
