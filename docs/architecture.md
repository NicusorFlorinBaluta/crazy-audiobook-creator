# Architecture

This document describes the current implementation. Historical implementation plans describe earlier two-machine and Ubuntu designs and are not authoritative.

## Runtime layout

The supported default is one Windows workstation:

```text
Browser / Electron shell
        │
        ▼
Dashboard API :8000 ─── Ollama :11434
        │
        ▼
Voice API :8100 ─── Qwen VoiceDesign bootstrap helper :8101
        │
        ├── Qwen3-TTS Base voice cloning
        ├── Whisper transcription
        ├── audio/speaker validation
        └── mastering + FFmpeg M4B export
```

All services bind to `127.0.0.1` by default. The dashboard serializes project runs, while the voice service also protects model lifecycle and GPU inference with a process-wide lock.

## Processing stages

1. **Extraction** parses the EPUB, preserves chapter order, and copies cover art into the project directory.
2. **Character analysis** scans all book text. Long chapters are divided into bounded analysis units; no chapter is omitted simply because it is large.
3. **Script generation** annotates immutable source fragments with speaker, emotion, speed, and pauses. It runs for the complete book before audio starts.
4. **Voice bootstrap** derives a speaking-only cast, compiles checked voice directions, and uses Qwen VoiceDesign to speak a known sentence. Whisper verifies that reference before it is registered. New projects pause once for manual casting approval. Qwen Base later caches a full voice-clone prompt derived from the reference audio and its exact transcript.
5. **Generation and validation** synthesize one file per script line, measure speaker similarity, transcribe it, inspect the waveform, retry failed or flagged attempts, and keep the best attempt.
6. **Mastering** assembles the exact ordered line set, adds a narrator chapter-title announcement, applies timing once, measures loudness, and writes a chapter WAV.
7. **Export** packages mastered chapter WAVs and metadata as AAC in an M4B.

## Why analysis is book-wide but audio is incremental

Speaker identity and characterization require book context. The pipeline therefore completes character analysis, the character registry, and scripts for all chapters as one logical phase.

Audio work is chapter-selectable. `generation_chapter_selection` has two meanings:

- `null`: all chapters; after missing chapters are complete, create the canonical full M4B.
- Nonempty list: generate/master those chapters and create a partial M4B named with the actual chapter set.

An empty list is invalid. Completed chapters remain usable while later batches run. Selecting a chapter again is safe: current fingerprints allow reuse, while any changed dependency invalidates the relevant artifact.

## Source-fidelity invariant

The script generator owns segmentation, not the LLM:

1. It converts a chapter into a non-overlapping sequence of immutable fragments.
2. Each fragment has an exact source start/end span.
3. The LLM may only return metadata for the supplied fragment IDs.
4. Missing, duplicate, or extra IDs reject the response.
5. Reassembling every script line must equal the normalized source exactly once.

This prevents overlap duplication, silent omissions, rewritten prose, and line-count drift. Dialogue recognition supports straight and curly quotes, guarded single quotes, and em-dash dialogue. Unknown speakers are rejected rather than invented during script parsing.

## Character and voice model

The character analyzer retains every speaking entity. It assigns a unique voice to the most important speakers up to `script.max_unique_voices`. Less prominent speakers deterministically share a compatible major-character voice or the narrator; the character remains distinct in script and metadata through its `voice_id`.

A character-analysis fingerprint includes the full extracted book, model, prompt, and voice cap. A change invalidates scripts and voice bootstrap. Voice reference hashes are included in generation fingerprints, so regenerated references invalidate dependent line audio.

## Artifact model

File existence is never sufficient evidence of completion.

- Script metadata records a dependency fingerprint and schema version.
- A generated-chapter manifest records every expected line ID, text hash, voice ID, source span, and generation fingerprint.
- A mastered-chapter manifest records the source segment-manifest hash and output hash.
- Output audio is checked for a readable, nonzero waveform.

At run start, generated and mastered chapter sets are independently reconstructed from these artifacts. A valid generated chapter does not imply a valid master, and a master is not trusted without its own matching manifest.

Atomic replacement is used for JSON state and final audio writes. Individual line cache entries are committed only after a selected attempt passes validation.

## Validation policy

Validation is fail-closed. A chapter response must contain exactly the expected unique line IDs and no failed lines. Hard failures include:

- word error rate over the configured threshold
- clipping
- excessive internal silence
- implausible duration/pacing
- speaker similarity below threshold
- missing, empty, or unreadable audio

Duration and noise anomalies may first be flagged, but both flagged and failed segments are retried. If no attempt passes, the chapter is not marked generated and mastering cannot proceed.

## Timing and mastering

There is one timing owner. When adjacent lines both request a boundary pause, the assembler uses the larger pause rather than summing both. Crossfades are applied only to directly adjacent audio, never across an intended silence.

Loudness normalization is chapter-integrated. The peak ceiling uses an oversampled true-peak estimate. The optional noise gate uses asymmetric attack/release smoothing and is disabled by default for generated audio.

The default `-19 LUFS` target is an internal playback choice, not a claim of ACX compliance.

## State, pause, and cancellation

The SQLite job queue performs atomic read-modify-write transactions and closes connections after each operation.

- User pause sets a transitional `pausing` state and requests cooperative cancellation.
- Generation checks cancellation at safe segment boundaries.
- Scheduled and deployment pauses park a live worker at chapter boundaries and preserve `active_stage`.
- Models unload only after the active operation releases the model lock, or after true idle time.
- A second project cannot start while another project owns the GPU pipeline.

## Storage

```text
brain/projects/<project-id>/
  book.json
  characters.json
  characters.meta.json
  script/
  book_script.json
  *.m4b

workspace/<project-id>/
  segments/
  manifests/
  chapters/
  output/

voice_library/<project-id>/
  voices.json
  *.wav

voice_cache.db
pipeline_state.db
```

Project and download paths are resolved beneath their configured roots. EPUB uploads are streamed with size limits and inspected for traversal, extreme expansion, and suspicious compression ratios.

## Privacy and network boundary

Book text is sent to the locally configured Ollama endpoint. Audio remains in local project/workspace storage. Google Books metadata lookup is off by default and only occurs after an explicit dashboard action or when `metadata.auto_fetch_external` is enabled.

Loopback binding is the supported default. If either service is bound beyond loopback, configure matching API tokens and restrictive CORS origins; startup otherwise refuses the unsafe bind.
