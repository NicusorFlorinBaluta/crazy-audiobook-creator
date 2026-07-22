# SQLite Embedding Cache & Comprehensive Pipeline Improvements

## Background

The pipeline currently relies on in-memory caching for speaker embeddings and voice references, re-reads `voices.json` from disk on every line, re-extracts audio features on every server restart, and has several architectural inefficiencies that hurt speed, consistency, and quality. This plan introduces a unified SQLite DB as the central state store for embeddings, quality data, and generation metadata, alongside targeted performance and quality improvements identified through a deep codebase audit.

---

## User Review Required

> [!IMPORTANT]
> **Whisper STT Model Size:** The current config uses `whisper_model: "tiny"` for speed, but `"small"` or `"base"` would significantly improve WER accuracy (and reduce false validation failures). Upgrading to `"base"` adds ~150 MB VRAM. Upgrading to `"small"` adds ~500 MB VRAM. Recommend `"base"` as the sweet spot for your 7900 XTX (24 GB VRAM).

> [!IMPORTANT]  
> **WER Constant Mismatch:** [constants.py](file:///e:/Projects/crazy-audiobook-creator/shared/constants.py#L91) still says `DEFAULT_WER_THRESHOLD = 0.05` (5%) while the actual runtime uses `0.35` from config.yaml. Should I update the constant to `0.35` to match, preventing confusion if anything falls back to the default?

> [!WARNING]
> **Noise Gate Performance:** The current noise gate in [normalizer.py](file:///e:/Projects/crazy-audiobook-creator/voice/mastering/normalizer.py#L171-L202) uses a **per-sample Python loop** (`for i in range(len(gate))`) which is extremely slow on long chapters (~6M samples = 6 million loop iterations). This will be replaced with a vectorized NumPy implementation.

---

## Proposed Changes

### Component 1: SQLite Embedding & Voice Cache DB

Creates a new `voice_cache.db` SQLite database that stores pre-computed speaker embeddings, voice metadata, and generation fingerprints. This eliminates cold-start penalties, guarantees bit-perfect voice consistency across sessions, and enables instant speaker switching.

#### [NEW] [embedding_store.py](file:///e:/Projects/crazy-audiobook-creator/voice/tts_server/embedding_store.py)

New SQLite-backed store with the following schema:

```sql
-- Pre-computed speaker embeddings (the core win)
CREATE TABLE speaker_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    embedding_blob BLOB NOT NULL,         -- torch.save() bytes
    ref_audio_hash TEXT NOT NULL,          -- SHA-256 of source .wav
    ref_text TEXT DEFAULT '',              -- ICL reference transcript
    voice_description TEXT DEFAULT '',
    embedding_shape TEXT,                  -- e.g. "[1, 512]"
    sample_rate INTEGER DEFAULT 24000,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, character_id, ref_audio_hash)
);

-- Voice FX prompt audio cache (pre-pitched reference clips)
CREATE TABLE fx_prompt_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_audio_hash TEXT NOT NULL,
    fx_settings_hash TEXT NOT NULL,        -- hash of VoiceFXSettings
    processed_audio_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_audio_hash, fx_settings_hash)
);

-- Line generation fingerprints (skip re-generation of unchanged lines)
CREATE TABLE generation_fingerprints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    line_id TEXT NOT NULL,
    text_hash TEXT NOT NULL,               -- SHA-256 of line text
    speaker_id TEXT NOT NULL,
    emotion TEXT DEFAULT '',
    speed REAL DEFAULT 1.0,
    fx_hash TEXT DEFAULT '',
    output_path TEXT NOT NULL,
    duration_seconds REAL,
    wer REAL,
    quality_score REAL,
    validation_status TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, line_id)
);
```

**Key methods:**
- `get_embedding(project_id, character_id)` → Returns cached PyTorch tensor or `None`
- `save_embedding(project_id, character_id, tensor, ref_audio_path)` → Computes SHA-256 of source `.wav`, serializes tensor via `torch.save()` to `io.BytesIO`, stores as BLOB
- `get_generation_fingerprint(project_id, line_id)` → Returns fingerprint dict or `None`
- `save_generation_fingerprint(...)` → Stores text hash + speaker + emotion + speed + quality results
- `line_needs_regeneration(project_id, line_id, text, speaker, emotion, speed)` → Returns `True` if any input changed vs stored fingerprint

---

#### [MODIFY] [qwen3_engine.py](file:///e:/Projects/crazy-audiobook-creator/voice/tts_server/qwen3_engine.py)

- Import `EmbeddingStore` and accept it as optional `__init__` parameter
- In `_generate()` (line 263-305): Before calling `generate_voice_clone()`, check `embedding_store.get_embedding()`. If found, pass the pre-loaded tensor directly (skipping audio file I/O and feature extraction). If not found, generate normally, then call `embedding_store.save_embedding()` to cache for future use
- Replace the in-memory `self._fx_prompt_cache` dict (line 204-212) with `EmbeddingStore.get_fx_prompt()` / `save_fx_prompt()`

**Impact:** Zero cold-start penalty after first run. Bit-perfect voice consistency. ~200ms saved per speaker switch.

---

#### [MODIFY] [validation_loop.py](file:///e:/Projects/crazy-audiobook-creator/voice/validator/validation_loop.py)

- Accept `EmbeddingStore` in `__init__`
- In Phase 1 `process_chapter()` (line 110-119): Replace the existing `output_path.exists() and st_size > 1000` skip check with a smarter fingerprint check via `embedding_store.line_needs_regeneration()`. This detects cases where the text was edited, the speaker changed, or the emotion was adjusted — and only re-generates those specific lines
- After validation in `_validate_segment()` (line 313-368): Save the WER, quality score, and validation status to `generation_fingerprints` so the dashboard can query historical quality trends

**Impact:** Editing a single line in a 150-line chapter only re-generates that one line instead of the whole chapter.

---

#### [MODIFY] [voice/tts_server/main.py](file:///e:/Projects/crazy-audiobook-creator/voice/tts_server/main.py)

- In `lifespan()`: Instantiate `EmbeddingStore(db_path="voice_cache.db")` and pass it to `Qwen3TTSEngine` and `ValidationLoop`

---

### Component 2: Noise Gate Vectorization (Performance Critical)

#### [MODIFY] [normalizer.py](file:///e:/Projects/crazy-audiobook-creator/voice/mastering/normalizer.py)

Replace the per-sample Python loop in `_apply_noise_gate()` (lines 190-200) with a fully vectorized NumPy implementation using `np.convolve()` or `scipy.signal.lfilter()` for the attack/release envelope follower.

**Current (slow):**
```python
for i in range(len(gate)):  # 6 million iterations per chapter
    if gate[i] > current:
        current = min(1.0, current + rate)
    else:
        current = max(0.0, current - rate)
```

**Proposed (fast):**
```python
# Vectorized IIR-style envelope follower
from scipy.signal import lfilter
attack_coeff = 1.0 / max(1, attack_samples)
release_coeff = 1.0 / max(1, release_samples)
# Use lfilter for single-pass envelope smoothing
```

**Impact:** ~50-100x speedup on chapter mastering (from seconds to milliseconds).

---

### Component 3: Quality & Accuracy Improvements

#### [MODIFY] [constants.py](file:///e:/Projects/crazy-audiobook-creator/shared/constants.py)

- Update `DEFAULT_WER_THRESHOLD` from `0.05` to `0.35` (line 91) to match the actual runtime value in `voice/config.yaml`. This prevents any code path that falls back to the constant from triggering the infinite-retry bug.

#### [MODIFY] [whisper_validator.py](file:///e:/Projects/crazy-audiobook-creator/voice/validator/whisper_validator.py)

- **Text normalization improvements** in `_normalize_text()` (line 172-189):
  - Add number expansion: `"6th"` → `"sixth"`, `"100"` → `"one hundred"` (the #1 cause of WER inflation)
  - Add contraction normalization: `"don't"` → `"do not"` (ensures Whisper and source text match)
  - Strip stage directions: Remove `[pause]`, `[sigh]`, `(whispering)` etc. that appear in emotional scripts but not in speech

**Impact:** Reduces WER by 5-15% on lines with numbers, significantly reducing false retry loops.

#### [MODIFY] [voice/config.yaml](file:///e:/Projects/crazy-audiobook-creator/voice/config.yaml)

- Change `whisper_model` from `"tiny"` to `"base"` — ~3x better accuracy for only ~150 MB additional VRAM
- Add new `embedding_cache` section:
  ```yaml
  embedding_cache:
    enabled: true
    db_path: "voice_cache.db"
    preload_on_startup: true
  ```

---

### Component 4: Pipeline Performance Optimizations

#### [MODIFY] [validation_loop.py](file:///e:/Projects/crazy-audiobook-creator/voice/validator/validation_loop.py)

**TTS/Whisper VRAM Coexistence:**

Currently the validation loop does this wasteful dance (lines 165-267):
```
Generate all lines → unload TTS → load Whisper → validate → unload Whisper → reload TTS → retry failed → unload TTS → load Whisper → ...
```

With `whisper_model: "base"` (~150 MB VRAM) and Qwen3-TTS (~3.5 GB VRAM), both models can **coexist in VRAM simultaneously** on the 24 GB 7900 XTX. Eliminate the load/unload cycle entirely:
- Load Whisper at startup alongside TTS (keep both resident)
- Validate each line immediately after generation (inline validation)
- Retry failed lines immediately without model swapping

**Impact:** Eliminates ~20-30 seconds of model load/unload overhead per chapter. Enables instant per-line quality feedback.

#### [MODIFY] [pipeline.py](file:///e:/Projects/crazy-audiobook-creator/brain/orchestrator/pipeline.py)

**Line merging word limit increase:**
- Line 617: Change `under_limit = len(prev.text.split()) + len(line.text.split()) < 180` to `< 250`
- The Qwen3-TTS model supports `max_new_tokens: 4096` (line 22 of config), which can handle ~300+ words. The current 180-word limit creates unnecessary TTS calls for long narrator passages. Increasing to 250 reduces total inference calls by ~15-20%.

**Checkpoint frequency:**
- Line 81 of `brain/config.yaml`: Change `checkpoint_frequency: 10` to `5` — saves pipeline state every 5 lines instead of 10, reducing re-work on crash by 50%.

---

### Component 5: Voice Library Disk I/O Reduction

#### [MODIFY] [voice_library.py](file:///e:/Projects/crazy-audiobook-creator/voice/tts_server/voice_library.py)

- Add an in-memory LRU cache for `_load_registry()` (line 104-110). Currently, every call to `get_voice_path()` or `get_voice_ref_text()` reads and parses `voices.json` from disk. For a 150-line chapter with 5 speakers, this means 150 file reads.
- Use `functools.lru_cache` or a simple dict cache that invalidates on `register_voice()` / `delete_voice()`.

**Impact:** Eliminates ~150 redundant disk reads per chapter.

---

## Summary of All Changes

| File | Change Type | Impact |
|------|-------------|--------|
| [embedding_store.py](file:///e:/Projects/crazy-audiobook-creator/voice/tts_server/embedding_store.py) | **NEW** | Core embedding cache DB |
| [qwen3_engine.py](file:///e:/Projects/crazy-audiobook-creator/voice/tts_server/qwen3_engine.py) | MODIFY | Use cached embeddings, remove in-memory FX cache |
| [validation_loop.py](file:///e:/Projects/crazy-audiobook-creator/voice/validator/validation_loop.py) | MODIFY | Smart fingerprint skip, inline validation, save quality to DB |
| [whisper_validator.py](file:///e:/Projects/crazy-audiobook-creator/voice/validator/whisper_validator.py) | MODIFY | Better text normalization (numbers, contractions) |
| [normalizer.py](file:///e:/Projects/crazy-audiobook-creator/voice/mastering/normalizer.py) | MODIFY | Vectorized noise gate (~100x speedup) |
| [voice_library.py](file:///e:/Projects/crazy-audiobook-creator/voice/tts_server/voice_library.py) | MODIFY | In-memory registry cache |
| [voice/tts_server/main.py](file:///e:/Projects/crazy-audiobook-creator/voice/tts_server/main.py) | MODIFY | Initialize EmbeddingStore, keep Whisper loaded |
| [constants.py](file:///e:/Projects/crazy-audiobook-creator/shared/constants.py) | MODIFY | Fix WER threshold constant |
| [voice/config.yaml](file:///e:/Projects/crazy-audiobook-creator/voice/config.yaml) | MODIFY | Whisper "base", embedding cache config |
| [brain/config.yaml](file:///e:/Projects/crazy-audiobook-creator/brain/config.yaml) | MODIFY | Checkpoint frequency |
| [pipeline.py](file:///e:/Projects/crazy-audiobook-creator/brain/orchestrator/pipeline.py) | MODIFY | Increase merge word limit |

---

## Verification Plan

### Automated Tests
```bash
# Test embedding store CRUD operations
E:\PYTORC~1\my_venv\Scripts\python.exe -c "from voice.tts_server.embedding_store import EmbeddingStore; store = EmbeddingStore(':memory:'); print('OK')"

# Test that voice server starts with new embedding store
E:\PYTORC~1\my_venv\Scripts\python.exe -c "from voice.tts_server.embedding_store import EmbeddingStore; s = EmbeddingStore(':memory:'); s.save_embedding('test', 'narrator', b'dummy', 'abc123'); print(s.get_embedding('test', 'narrator'))"
```

### Manual Verification
- Start the Voice Server and confirm `voice_cache.db` is created and embeddings are cached on first chapter generation
- Run a second chapter and confirm embeddings are loaded from DB (log message: "Loaded cached embedding for...")
- Verify chapter mastering is noticeably faster after noise gate vectorization
- Confirm WER validation accuracy improves with `whisper_model: "base"`
- Monitor VRAM usage to ensure TTS + Whisper coexist within 24 GB
