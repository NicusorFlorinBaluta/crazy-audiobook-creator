# Quality Assurance

**Status:** Reference — Describes current behaviour. Keep it accurate when the code changes.

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

### Reproducible synthesis

Synthesis is deterministic, so a cache hit and a regeneration produce the same
audio rather than merely the same *acceptability*.

The engine samples with `do_sample: true` at `temperature: 0.9`. Each line is
therefore seeded from its project, line ID, synthesis text, voice, and attempt
number. Consequences:

- Purging the cache no longer changes the audiobook. Previously "same
  fingerprint implies same audio" held only because the WAV was cached, not
  because generation was repeatable.
- A repaired line regenerated among untouched neighbours reproduces exactly,
  instead of landing as an independently sampled take with different prosody —
  a mismatch that WER and speaker similarity both pass.
- Listening A/B comparisons are repeatable in production, not only in the
  benchmark harness.

The attempt number is part of the seed deliberately. A validation retry exists
because the previous take failed; reusing its seed would reproduce that exact
failure and the retry could never succeed.

## Speaker consistency

Before metadata is accepted, deterministic evidence rejects contradictions
that do not require literary inference: a known male speaker cannot own a quote
whose attached tag says `she`, a known female speaker cannot own one tagged
`he`, gendered noun tags such as `the boy said` and `the woman asked` must
match binary speaker metadata, and an explicitly named tag cannot name a
different registered character. Explicit unnamed boy/girl/man/woman speakers
missed by model analysis are added deterministically before scripting.
Unknown/nonbinary metadata is not guessed from pronouns. These
checks request a corrected metadata pass; they do not choose among multiple
plausible characters.

Before loading Whisper, the voice service uses Qwen's speaker encoder to
compare generated segments to their reference voices. TTS is unloaded before
Whisper unless `keep_tts_and_whisper_resident` is explicitly enabled from a
hardware-specific benchmark. Whisper is always released at the chapter request
boundary. VoiceDesign, Qwen Base, and Whisper are loaded lazily so reference
bootstrap never requires all models to coexist in VRAM.

The default similarity threshold is a starting point, not a universal calibration. Calibrate it against known-good reference/generated pairs from the actual model and hardware before raising it.

### Within-chapter delivery consistency

Every check above is either per-segment (WER, clipping, duration, pitch CV,
speaker similarity) or cross-chapter (`cross_chapter_voice_drift`). Neither
measures whether *adjacent lines in the same chapter* match each other, which
is the artifact a listener notices first.

`brain/orchestrator/quality_trends.py` therefore also reports, per voice per
chapter:

| Metric | Meaning |
|---|---|
| `pitch_relative_spread` | stdev / median of `pitch_median` across the chapter's accepted lines |
| `speaking_rate_relative_spread` | same, for characters per audio second |
| `largest_adjacent_pitch_jump_ratio` | biggest line-to-line pitch change, relative to the chapter median |

These are **warning-only** and never block a release. They are computed
entirely from measurements already paid for during validation, so they add no
inference cost. Thresholds are deliberately quiet on the current corpus: the
useful signal is a *change* in spread between runs, not an absolute level. They
exist so a future sampling or seeding change has something to be evaluated
against — see [Scripting quality and performance policy](scripting-quality-performance-policy.md)
for the promotion protocol.

A chapter with fewer than six measured segments is reported but never warned:
below that, expressive variation and inconsistency cannot be distinguished.

## Reference-voice validation

Qwen VoiceDesign creates each reusable reference by speaking a known gender-appropriate sentence from a compiled, metadata-checked direction. After VoiceDesign releases GPU memory, Whisper transcribes the file. References over the bootstrap WER limit are removed and the bootstrap fails rather than registering a mismatched transcript.

The exact spoken test sentence is stored as `ref_text`; it is not inferred from the voice description.

### Cast distinctness convergence

After the references exist, the Base speaker encoder embeds each one and
compares every pair. A pair at or above `validation.voice_profile_similarity_warning`
(default `0.985`) is a collision.

This measurement used to be terminal: VoiceDesign had already been unloaded to
free VRAM for the encoder, so a collision could only ever be reported. A
52-character cast produced 22 flagged pairs for manual resolution, one of them
a character at 0.992 against the narrator.

Because VoiceDesign runs as a subprocess, the two models take turns instead.
Up to `validation.voice_distinctness_rounds` extra rounds (default `2`) re-boot
it, redesign **only** the colliding voices with a brief naming the specific
voices they collided with, re-embed just those, and re-measure the whole cast.
Whatever still collides when the rounds run out is surfaced for manual redesign
exactly as before, so the loop can improve the outcome but never blocks it.

Redesigned references are transcript-checked in a single Whisper load after the
rounds finish — a contrast brief moves pitch and speaking rate, which is the
kind of change that can hurt intelligibility, and the initial WER pass ran
before any redesign existed.

`BootstrapVoicesResponse.distinctness_rounds` reports per round which voices
were redesigned and whether the collision count and worst similarity actually
improved. Read it before assuming the loop is helping. Full rationale in
[decisions/2026-09-04-voice-distinctness-convergence.md](decisions/2026-09-04-voice-distinctness-convergence.md).

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

## Frontend behaviour tests

`tests/test_dashboard_frontend_ux.py` asserts that specific substrings appear
in the frontend source. That style breaks when an attribute is reordered and
passes when the surrounding logic is broken, so it cannot catch a wrong branch
with intact markup — which is what shipped twice:

- `waiting_for_review` was classified as a terminal status, so it shadowed
  `active_stage` and the `voice_review` branch of `renderWorkStatus` became
  unreachable. A project blocked on voice approval was told to "Choose chapters
  and start the pipeline", which cannot clear a review gate.
- The attention panel rendered "⚠️ Action required … ⚠️ **0** Action Required
  items" — a red alarm asserting that nothing is wrong.

`tests/frontend/` covers this class. The harness (`harness.mjs`) loads the real
`index.html` under jsdom, evaluates the real scripts against it, and calls the
real render functions; only the network and the WebSocket are stubbed. It waits
for the document to finish loading *before* injecting the scripts, so app.js's
`DOMContentLoaded` listener never fires and each test controls exactly what is
rendered.

```powershell
npm ci
npm test
```

`CAC_FRONTEND_DIR` points the harness at a different copy of the sources. That
exists so a regression test can be proven to fail against pre-fix code rather
than passing vacuously — the four regression tests above were verified that way.

## The lint ratchet

`pyproject.toml` ignores a short list of ruff rules that fire in too many
existing places to fix at once. Each entry carries a count and a plan, and the
list only ever shrinks. Adding to it needs a comment saying why.

**Cleared so far.** `B023` (loop-variable closure, 31 sites), `B904`, `TRY400`,
`TRY004`, and `S110`/`S112` -- the last of those being roughly 75 places that
discarded an exception with no trace at all. Nothing in the project now throws
away an exception without recording that it happened. The single deliberate
exception is inside `ProjectLogHandler.emit`, where a logger call would
re-enter the handler that just failed; it uses the stdlib's `handleError`.

**Still open.** `BLE001` (169, from ~208). These all log now, so what remains
is breadth rather than silence -- and breadth matters because `except
Exception` catches the failure you expected *and* the typo you did not. An
`AttributeError` introduced by a refactor gets swallowed and reported as a
degraded read instead of failing loudly. That is not hypothetical: on
2026-09-04 a missing attribute in the cast-adjudication path killed a live run,
and the same shape one frame away would have been silently absorbed.

When narrowing, name what the guarded call can actually raise, generously:

| What is guarded | Catch |
| --- | --- |
| Reading someone else's file | `(OSError, UnicodeDecodeError, ValueError, KeyError, TypeError)` |
| SQLite and pickle | `(sqlite3.Error, pickle.PickleError, OSError, EOFError, ValueError, TypeError)` |
| `wave` / `soundfile` | `(OSError, wave.Error, EOFError, ValueError)` |

`ValueError` earns its place in those: `json.JSONDecodeError` is a `ValueError`,
and so is `int()` or `float()` over a junk field.

Some sites should stay broad, and saying so is part of the job.
`nas_syncer.py` is the standing example: paramiko raises `SSHException`
alongside `OSError` and is imported lazily, so the name is not available in an
`except` clause without making it a hard import. Narrowing to `OSError` alone
would let an `SSHException` escape in the middle of a delivery, which is worse
than a broad catch that logs.

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
