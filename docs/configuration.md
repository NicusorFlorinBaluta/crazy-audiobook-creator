# Configuration Reference

## Overview

The pipeline uses two YAML configuration files:
- `brain/config.yaml` — Windows machine settings (LLM, pipeline, dashboard)
- `voice/config.yaml` — Ubuntu machine settings (TTS, validation, mastering)

This document explains every configuration option.

---

## brain/config.yaml

### `ollama` — LLM Settings

```yaml
ollama:
  host: "http://localhost:11434"    # Ollama API endpoint
  model: "qwen3:32b"               # Model to use for script generation
  temperature_pass1: 0.3           # Lower = more consistent character analysis
  temperature_pass2: 0.4           # Slightly higher for creative emotion descriptions
  top_p: 0.9                       # Nucleus sampling parameter
  context_window: 10               # Paragraphs before/after for emotion context
  timeout: 120                     # Seconds to wait for LLM response
  max_retries: 3                   # Retry count on LLM failure
```

**Model Options** (ordered by quality, descending):

| Model | VRAM Required | Speed | Quality |
|-------|--------------|-------|---------|
| `qwen3:32b` | ~20 GB | Slow | Best |
| `qwen3:14b` | ~10 GB | Medium | Very Good |
| `llama3.3:70b-q2_K` | ~22 GB | Very Slow | Excellent |
| `qwen3:8b` | ~6 GB | Fast | Good |

### `ubuntu` — Ubuntu Machine Connection

```yaml
ubuntu:
  host: "http://192.168.1.100:8100"  # Ubuntu TTS server URL
  timeout: 30                         # HTTP request timeout (seconds)
  retries: 3                          # Retry count on network failure
  retry_delay: 5                      # Seconds between retries
  reconnect_interval: 60              # Seconds between reconnect attempts when offline
```

### `extraction` — EPUB Text Extraction

```yaml
extraction:
  skip_toc: true                    # Skip table of contents
  skip_appendices: true             # Skip appendices, glossaries, maps
  skip_front_matter: true           # Skip dedications, acknowledgments, etc.
  min_chapter_words: 100            # Minimum words to consider a valid chapter
  max_chapter_words: 20000          # Split chapters longer than this
  chapter_detection: "auto"         # "auto" | "heading" | "pattern" | "none"
  preserve_poetry: true             # Preserve line breaks in poetry/songs
  fantasy_name_threshold: 3         # Flag words with >N unusual character sequences
```

**Chapter Detection Modes**:
- `auto`: Try heading tags first, fall back to text patterns, then treat as single chapter
- `heading`: Only use HTML heading tags (h1, h2) to split chapters
- `pattern`: Use regex patterns ("Chapter X", "Part Y", etc.)
- `none`: Treat entire book as one chapter

### `script` — Script Generation

```yaml
script:
  max_segment_sentences: 4          # Max sentences per TTS segment
  min_segment_words: 3              # Min words per segment (avoid tiny fragments)
  default_speed: 1.0                # Default narration speed
  
  # Pause settings (milliseconds)
  narrator_pause_ms: 500            # Default pause after narrator segments
  dialogue_pause_ms: 300            # Default pause between dialogue lines
  scene_transition_pause_ms: 1500   # Pause at scene transitions
  chapter_start_pause_ms: 1000      # Silence at chapter start
  chapter_end_pause_ms: 2000        # Silence at chapter end
  paragraph_pause_ms: 600           # Pause between paragraphs
  
  # Character handling
  max_unique_voices: 20             # Max distinct character voices per book
  minor_character_threshold: 3      # Characters with ≤N lines get generic voices
  group_minor_characters: true      # Assign generic voices by gender for minor chars
  
  # Chunking for long chapters
  chunk_size_words: 5000            # Process this many words per LLM call
  chunk_overlap_words: 500          # Overlap between chunks for context continuity
```

### `dashboard` — Web Dashboard

```yaml
dashboard:
  port: 8000                        # Dashboard port
  host: "0.0.0.0"                   # Listen on all interfaces
  cors_origins: ["*"]               # CORS allowed origins
  static_dir: "dashboard/frontend"  # Path to frontend files
  
  # Project storage
  projects_dir: "projects"          # Directory for project data
  max_projects: 50                  # Max concurrent projects
  auto_cleanup_days: 30             # Delete projects older than N days (0 = never)
```

### `pipeline` — Pipeline Behavior

```yaml
pipeline:
  auto_start_tts: true              # Automatically start TTS after script generation
  auto_master: true                 # Automatically master after TTS generation
  auto_export: true                 # Automatically export M4B after mastering
  cleanup_intermediates: true       # Delete WAV segments after M4B export
  
  # State persistence
  state_db: "pipeline_state.db"     # SQLite database for pipeline state
  checkpoint_frequency: 10          # Save state every N lines
  
  # Generation batching
  batch_mode: "chapter"             # "chapter" | "line" | "engine"
  # chapter: Generate all lines in a chapter, then validate, then master
  # line: Generate + validate each line individually (slower, catches errors early)
  # engine: Group all lines by engine, generate each batch (only useful with multi-engine)
```

---

## voice/config.yaml

### `tts` — Text-to-Speech Engine

```yaml
tts:
  model: "Qwen/Qwen3-TTS-1.7B"     # HuggingFace model identifier
  device: "cuda"                     # "cuda" | "cpu"
  dtype: "float16"                   # "float16" | "bfloat16" | "float32"
  sample_rate: 24000                 # Native output sample rate (Hz)
  max_text_length: 500               # Max characters per generation call
  
  # Voice Design settings
  voice_design_duration: 10          # Duration of reference clips (seconds)
  voice_design_test_sentences:
    male: "The ancient tower stood against the darkening sky, its stones weathered by centuries of wind and rain."
    female: "She walked through the moonlit garden, her footsteps barely disturbing the fallen leaves."
    neutral: "The library was vast and silent, filled with the scent of old paper and forgotten memories."
  
  # Generation parameters
  generation:
    max_new_tokens: 4096             # Max tokens per generation
    do_sample: true
    temperature: 0.7
    top_p: 0.9
    repetition_penalty: 1.1
```

### `validation` — Quality Validation

```yaml
validation:
  enabled: true                      # Enable/disable validation
  whisper_model: "medium"            # "tiny" | "base" | "small" | "medium" | "large-v3"
  whisper_device: "auto"             # "auto" | "cuda" | "cpu"
  # "auto" = use GPU when TTS is not running, fall back to CPU
  
  # Thresholds
  wer_threshold: 0.05               # Word Error Rate threshold (5%)
  max_retries: 3                     # Max regeneration attempts
  
  # Audio checks
  artifact_noise_threshold: -50      # Noise floor threshold (dB)
  clipping_threshold: -0.5           # Peak threshold (dBFS)
  min_duration_seconds: 0.3          # Minimum segment duration
  max_silence_seconds: 3.0           # Maximum silence within a segment
  duration_tolerance: 0.3            # ±30% of expected duration
  
  # Text normalization for WER
  normalize_text: true               # Lowercase, strip punctuation
  expand_numbers: true               # "42" → "forty-two"
  expand_abbreviations: true         # "Dr." → "Doctor"
```

**Whisper Model Options**:

| Model | VRAM | Speed | Accuracy | Recommended |
|-------|------|-------|----------|-------------|
| `tiny` | ~1 GB | 32x | Low | Testing only |
| `base` | ~1 GB | 16x | OK | Fast validation |
| `small` | ~2 GB | 8x | Good | - |
| `medium` | ~5 GB | 4x | Very Good | ✅ Default |
| `large-v3` | ~10 GB | 2x | Best | CPU only (won't fit with TTS) |

### `mastering` — Audio Post-Processing

```yaml
mastering:
  # Loudness normalization
  target_lufs: -19                   # Target loudness (LUFS)
  # ACX standard: -18 to -23 LUFS. -19 is a safe middle ground.
  
  # Peak limiting
  peak_limit_dbfs: -1.0              # True peak limit (dBTP)
  
  # Cross-fading
  crossfade_ms: 30                   # Cross-fade between segments (ms)
  fade_curve: "cosine"               # "linear" | "cosine" | "exponential"
  
  # Noise gate
  noise_gate_enabled: true
  noise_gate_threshold: -50          # Threshold (dB)
  noise_gate_attack_ms: 5            # Attack time
  noise_gate_release_ms: 50          # Release time
  
  # Output format
  output_sample_rate: 44100          # Output sample rate (Hz)
  output_bit_depth: 16               # Output bit depth
  output_channels: 1                 # 1 = mono (audiobook standard)
```

### `export` — M4B Export

```yaml
export:
  # Audio encoding
  codec: "aac"                       # "aac" | "libopus" | "libmp3lame"
  bitrate: "128k"                    # Audio bitrate
  channels: 1                        # Mono
  
  # Metadata defaults
  default_narrator: "AI Generated"
  default_genre: "Fantasy"
  
  # Chapter handling
  chapter_silence_ms: 2000           # Silence between chapters in M4B
  embed_cover_art: true              # Embed cover art from EPUB if available
  
  # File naming
  filename_template: "{title}"       # Template for output filename
  # Available variables: {title}, {author}, {date}
```

### `server` — FastAPI Server

```yaml
server:
  port: 8100                         # Server port
  host: "0.0.0.0"                    # Listen on all interfaces
  workers: 1                         # Number of workers (keep at 1 for GPU)
  cors_origins: ["*"]                # CORS allowed origins
  max_upload_size_mb: 100            # Maximum file upload size
```

### `storage` — Disk Management

```yaml
storage:
  workspace_dir: "workspace"         # Working directory for intermediates
  voice_library_dir: "voice_library" # Voice reference clips
  auto_cleanup_intermediates: true   # Delete WAVs after M4B export
  keep_voice_library: true           # Always keep voice references
  max_workspace_gb: 50               # Warn when workspace exceeds this size
```

---

## Environment Variables

These override config file settings:

### Windows
```powershell
# Ollama
$env:OLLAMA_HOST = "http://localhost:11434"
$env:OLLAMA_VULKAN = "1"                      # Force Vulkan for AMD GPU
$env:HSA_OVERRIDE_GFX_VERSION = "11.0.0"      # ROCm GPU version (if using ROCm)

# Pipeline
$env:AUDIOBOOK_UBUNTU_HOST = "http://192.168.1.100:8100"
$env:AUDIOBOOK_DASHBOARD_PORT = "8000"
```

### Ubuntu
```bash
# CUDA
export CUDA_VISIBLE_DEVICES=0                  # Use first GPU

# TTS Server
export AUDIOBOOK_TTS_MODEL="Qwen/Qwen3-TTS-1.7B"
export AUDIOBOOK_TTS_DEVICE="cuda"
export AUDIOBOOK_SERVER_PORT=8100

# Storage
export AUDIOBOOK_WORKSPACE="/path/to/workspace"
```

---

## Profiles

For different use cases, you can create config profiles:

### `config.fast.yaml` — Speed Priority
```yaml
tts:
  model: "Qwen/Qwen3-TTS-0.6B"     # Smaller model, faster
validation:
  whisper_model: "tiny"              # Fastest validation
  wer_threshold: 0.10               # More lenient
mastering:
  crossfade_ms: 10                   # Minimal processing
```

### `config.quality.yaml` — Quality Priority (Default)
```yaml
tts:
  model: "Qwen/Qwen3-TTS-1.7B"     # Full model
validation:
  whisper_model: "medium"
  wer_threshold: 0.05
  max_retries: 3
mastering:
  crossfade_ms: 30
  noise_gate_enabled: true
```

### `config.paranoid.yaml` — Maximum Quality
```yaml
tts:
  model: "Qwen/Qwen3-TTS-1.7B"
  generation:
    temperature: 0.5                 # Less random
validation:
  whisper_model: "large-v3"          # Best accuracy (CPU only)
  whisper_device: "cpu"
  wer_threshold: 0.03               # Very strict
  max_retries: 5
mastering:
  crossfade_ms: 50
  noise_gate_enabled: true
```

To use a profile:
```bash
# Ubuntu
python -m tts_server.main --config config.paranoid.yaml

# Windows
python -m dashboard.api.main --config config.paranoid.yaml
```
