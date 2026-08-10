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
- final `pass`, `accepted_with_warning`, `flagged`, or `fail` status and notes
- text similarity, effective text error, attempt number, and the exact acceptance reason
- nonblocking pitch-variation/dynamic-range metrics and a calibrated monotone warning when prosody diagnostics are enabled

WER, clipping, speaker similarity, and prosody checks do not prove perceptual
cleanliness. The 2026-08-09 run passed those objective gates while many
phase-processed lines still sounded echoic. Output-changing DSP therefore also
requires a controlled listening gate; it cannot be promoted solely from ASR or
signal-level metrics.

Reference audio and reference text are always paired. If a character reference lacks a trustworthy transcript, the validator does not combine it with the narrator transcript; synthesis falls back to supported speaker conditioning.

Chapter validation passes the EPUB metadata language to Whisper. Auto-detection
is used only when the project has no language metadata, avoiding independent
language guesses for every short utterance.

## Outcome rules

Hard failures include:

- WER above `validation.wer_threshold` (post-FX emotion labels do not relax this unless an explicit nonzero `emotion_wer_allowance` is configured)
- clipped samples beyond the configured peak threshold
- excessive silence
- severe duration/pacing anomaly
- speaker similarity below `speaker_similarity_threshold`
- missing, empty, or unreadable audio

Compact spelling similarity is not a general escape hatch for transcription failures. It is considered only when the expected line contains an explicitly approved character name or pronunciation-dictionary term. This accommodates predictable ASR spellings such as fantasy names without allowing unrelated words to pass.

Pronunciation substitutions never replace authored script text. They are
carried as a separate synthesis-only spoken form, included in generation
fingerprints and manifests, while validation continues to compare audio with
the original line plus the explicitly approved glossary. Phrase replacement is
longest-first and performed in one pass so a shorter entry cannot rewrite a
longer replacement.

When transcription, speaker identity, clipping, silence, and pacing hard checks
pass, a marginal duration/noise/composite-score result is
`accepted_with_warning`. It is not retried merely to satisfy a soft heuristic.
Warnings remain visible and queryable. Hard failures and legacy unresolved
`flagged` attempts are retried up to `max_retries`.

Only `pass` and `accepted_with_warning` are accepted outcomes. Any other final
status appears in `failed_line_ids`, leaves the chapter incomplete, and blocks
mastering.

## Cache correctness

A cached segment is eligible only when:

1. Its generation fingerprint matches the current text, speaker/voice ID, voice-reference content, reference text, emotion, speed, pronunciation substitutions, FX, generation configuration, and versioned post-processing policy.
2. The file content hash still matches the recorded hash.
3. The previous validation status was acceptable.
4. The file is readable audio with a nonzero waveform.

The cache is keyed by line identity but validated by dependencies. Editing one line can regenerate only that line; changing a character reference invalidates every dependent line.

## Speaker consistency

Before metadata is accepted, deterministic evidence rejects contradictions
that do not require literary inference: a known male speaker cannot own a quote
whose attached tag says `she`, a known female speaker cannot own one tagged
`he`, and an explicitly named tag cannot name a different registered
character. Unknown/nonbinary metadata is not guessed from pronouns. These
checks request a corrected metadata pass; they do not choose among multiple
plausible characters.

Before loading Whisper, the voice service uses Qwen's speaker encoder to
compare generated segments to their reference voices. TTS is unloaded before
Whisper unless `keep_tts_and_whisper_resident` is explicitly enabled from a
hardware-specific benchmark. Whisper is always released at the chapter request
boundary. VoiceDesign, Qwen Base, and Whisper are loaded lazily so reference
bootstrap never requires all models to coexist in VRAM.

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

The assembler refuses missing/empty segments. It uses one pause at each
boundary—the larger adjacent request—to prevent double-counting. Crossfades
are restricted to pause-free adjacency. Dialogue/tag lines sharing an
`utterance_group_id` are an explicit exception: they join at zero gap without
crossfade, remain separate voices, and retain join diagnostics for listening
review.

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
## Verification tiers

Use the cheapest tier that can prove the changed contract:

1. `scripts/verify_pipeline.py --tier static` runs unit, compile, JavaScript,
   and documentation checks without models.
2. `gpu-smoke`, `chapter`, and `full` require explicit `--allow-models`; these
   are workstation gates, not ordinary CI tests.
3. `artifact` hashes and inspects an existing export without generating it.

Accepted synthesis and validation are checkpointed independently. Regression
coverage must prove that a crash after synthesis revalidates the matching WAV,
that changed hashes cannot be reused, and that an older selected retry remains
the reported winner while all attempts still count toward retry metrics.

Automated acceptance is not the final listening gate. The Quality tab preserves
join dispositions and exposes both adjacent segments; accepted warnings and
every retry attempt remain separately reviewable.
