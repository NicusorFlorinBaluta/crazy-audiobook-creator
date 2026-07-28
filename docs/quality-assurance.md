# Quality Assurance

Quality control is part of generation, not a reporting-only pass. A chapter cannot be marked generated unless every expected script line has one accepted audio file.

## Per-line checks

For each attempt the validator records:

- normalized Whisper transcript and word error rate (WER)
- duration and expected-duration deviation
- peak level and clipping
- measured noise floor
- excessive internal silence
- Qwen speaker-encoder similarity to the selected reference voice
- final `pass`, `flagged`, or `fail` status and notes
- text similarity, effective text error, attempt number, and the exact acceptance reason

Reference audio and reference text are always paired. If a character reference lacks a trustworthy transcript, the validator does not combine it with the narrator transcript; synthesis falls back to supported speaker conditioning.

## Outcome rules

Hard failures include:

- WER above `validation.wer_threshold`
- clipped samples beyond the configured peak threshold
- excessive silence
- severe duration/pacing anomaly
- speaker similarity below `speaker_similarity_threshold`
- missing, empty, or unreadable audio

Compact spelling similarity is not a general escape hatch for transcription failures. It is considered only when the expected line contains an explicitly approved character name or pronunciation-dictionary term. This accommodates predictable ASR spellings such as fantasy names without allowing unrelated words to pass.

Less severe duration or noise anomalies may be flagged. Both failed and flagged attempts are retried up to `max_retries`. The best attempt is chosen by status first and quality score second.

If the selected attempt is still not `pass`, its line ID appears in `failed_line_ids`, chapter status is failure, no valid generation fingerprint is committed, and mastering is blocked.

## Cache correctness

A cached segment is eligible only when:

1. Its generation fingerprint matches the current text, speaker/voice ID, voice-reference content, reference text, emotion, speed, pronunciation substitutions, FX, and generation configuration.
2. The file content hash still matches the recorded hash.
3. The previous validation status was acceptable.
4. The file is readable audio with a nonzero waveform.

The cache is keyed by line identity but validated by dependencies. Editing one line can regenerate only that line; changing a character reference invalidates every dependent line.

## Speaker consistency

Before loading Whisper, the voice service uses Qwen’s speaker encoder to compare generated segments to their reference voices. This stage is run while the Qwen model is loaded. Qwen is then unloaded before Whisper validation to keep GPU lifecycle predictable.

The default similarity threshold is a starting point, not a universal calibration. Calibrate it against known-good reference/generated pairs from the actual model and hardware before raising it.

## Reference-voice validation

Qwen VoiceDesign creates each reusable reference by speaking a known gender-appropriate sentence from a compiled, metadata-checked direction. After VoiceDesign releases GPU memory, Whisper transcribes the file. References over the bootstrap WER limit are removed and the bootstrap fails rather than registering a mismatched transcript.

The exact spoken test sentence is stored as `ref_text`; it is not inferred from the voice description.

## Chapter completeness

Chapter requests and responses are reconciled by ID, never by `zip()` position:

- expected IDs must be unique
- returned IDs must be unique
- returned ID set must equal the expected set
- `failed_line_ids` must be empty
- output paths must resolve to valid segment files

The generated manifest then captures the exact order and dependencies. Mastering independently verifies that manifest and every line file.

## Mastering checks

The assembler refuses missing/empty segments. It uses one pause at each boundary—the larger adjacent request—to prevent double-counting. Crossfades are restricted to pause-free adjacency.

The normalizer:

- measures integrated chapter loudness
- applies gain toward `mastering.target_lufs`
- enforces the peak ceiling using a 4× oversampled estimate
- optionally applies an attack/release noise gate
- writes the configured mono sample rate/bit depth

The default output is intended for personal listening. The project does not currently produce ACX MP3 deliverables or certify ACX RMS, peak, bitrate, room-tone, and submission requirements.

## Interpreting the dashboard

- **Scripted**: source-preserving script exists.
- **Generated**: every segment and generated manifest are current.
- **Done/mastered**: the chapter WAV and master manifest are current.
- **Selection complete**: the selected partial batch and partial M4B completed; it does not imply the whole book is finished.
- **Complete**: all chapters are mastered and the canonical full M4B was exported.

Raw `.wav` counts shown during an active chapter are progress only. Durable completion comes from validated manifests.
The Quality tab shows final retry/non-pass outcomes, while the database retains each attempt even when the chapter ultimately fails.

## Recommended smoke test

Before a long book:

1. Use a small EPUB with narration plus at least two dialogue styles.
2. Run book-wide scripting and inspect source coverage.
3. Select one chapter and produce a partial M4B.
4. Re-run it unchanged and confirm cache reuse.
5. Change one pronunciation or reference voice and confirm only dependent audio is regenerated.
6. Select a later chapter batch and confirm earlier mastered WAVs remain downloadable.
7. Select All and confirm the full export is refused until every chapter is valid, then succeeds.

Unit tests use fake engines and do not replace this model-level smoke test.
