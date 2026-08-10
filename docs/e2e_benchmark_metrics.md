# Performance and quality improvement plan â€” 2026-08-03

## Objective

Improve throughput, resumability, pronunciation, voice distinction, prosody,
continuity, and mastering without weakening fail-closed correctness. Every
optimization is gated by before/after measurements and has an explicit rollback
condition. The preserved `sample_book` and `sample_book-v32b-prod-e2e`
projects remain evidence and must not be modified.

## Current measured baseline

- Prologue: 519.2 seconds mastered from 1,629.9 seconds of chapter generation
  (0.32x realtime output rate).
- Chapter 2: 492.9 seconds mastered from 1,341.3 seconds (0.37x realtime).
- Chapter 4: 594.4 seconds mastered from 1,506.1 seconds (0.39x realtime).
- Chapters 2-4: 202 accepted segments, 199 pass, 3 soft warnings, 0 fail,
  15 final-attempt retries, 4.91% raw average WER.
- Peak sampled VRAM: 5.15 GB with sequential TTS/Whisper residency.
- Resume example: cached chapter 3 revalidation took 52.9 seconds.
- Regression baseline: 87 tests passing; the suite has expanded as phases land.

Raw WER is intentionally not the only quality signal. Approved book-term
spellings can have high raw WER while effective text error is zero. Hard gates
remain ordinary-word fidelity, speaker identity, clipping, silence, pacing, and
artifact checks.

## Non-negotiable constraints

1. Never lower a hard validation threshold merely to improve completion rate.
2. Generation and validation invalidation are separate decisions.
3. Existing accepted audio is reused only when its audio hash, text, voice
   reference, generation controls, and relevant schema match.
4. New quality checks begin in report-only mode before they can block export.
5. Experimental model-residency or inference settings require an isolated A/B
   benchmark and automatic fallback.
6. Partial-book generation, later continuation, immediate interruption, and
   one-time voice review must continue to work.

## Phase 1 â€” cache correctness and observability

### 1.1 Separate generation and validation fingerprints

Generation identity contains only inputs that can change synthesized audio:
text/spoken form, voice reference and transcript, TTS model/configuration,
emotion, speed, and effects. Validation identity separately contains the audio
hash, expected text, glossary, Whisper model, thresholds, analyzer settings,
speaker-reference hash, and validation schema.

Expected effect: prevents validator changes from resynthesizing valid audio.

Validation:

- Unit test: changing only validation schema causes revalidation, not TTS.
- Unit test: changing voice/text/emotion/speed/effects still causes TTS.
- Resume test: one failed line in a cached chapter generates at most that line.
- Reject if any stale audio is accepted after a generation input changes.

### 1.2 Cache accepted validation and speaker-similarity results

Store the complete accepted `QualityResult`, keyed by the separate validation
fingerprint. Do not cache failed results. Reuse cached results without loading
Whisper or recalculating speaker similarity.

Expected effect: cached chapter verification should fall from tens of seconds
to low single-digit seconds, excluding service startup.

Validation:

- First run transcribes and calculates speaker similarity; identical second run
  performs neither operation.
- Audio, text, glossary, reference voice, threshold, model, or schema changes
  invalidate validation only.
- Corrupt/missing audio never hits the cache.
- Cached warning/pass counts exactly match the original run.
- Target: at least 80% reduction in warm cached-chapter validation time.

### 1.3 Detailed stage telemetry

Record per project/chapter/line timings for TTS synthesis, speaker similarity,
Whisper load/transcription, audio analysis, retry synthesis, model load/unload,
mastering, export, cache hits, and cache misses. Record sampled VRAM and active
model state when available.

Validation:

- Timings are monotonic and aggregate within 5% of request wall time.
- Cache counters equal the number of processed lines.
- Metrics survive dashboard restart and do not fail the pipeline if sampling is
  unavailable.
- Telemetry overhead below 1% on a 50-line cached test.

## Phase 2 â€” reduce retries without relaxing quality

### 2.1 Risk-aware first-attempt policy

Apply a narrowly scoped clarity override to very short emphatic or all-caps
lines. Use clean articulation, neutral speed, and no effects on their first
attempt while retaining authored text in the script and manifest. Repetitions,
proper nouns, extreme speed/effects, and punctuation-heavy lines remain
unchanged: the broader policy moved failures between lines in the fixed-corpus
A/B instead of reducing them.

Validation:

- Corpus containing short shouts, repetitions, names, numbers, quotations, and
  normal narration.
- No source text mutation.
- Hard-failure rate must not rise.
- Target: at least 25% fewer retries on the risk corpus with non-inferior speaker
  similarity and effective text error.

### 2.2 Automatic synthesis pronunciation lexicon

Extend the validated book-term glossary into a synthesis lexicon. Derive
pronunciation candidates for repeated fictional terms, retain the original
spelling in metadata, and apply only verified deterministic spoken forms.

Validation:

- Same term is pronounced consistently across at least ten occurrences and
  multiple chapters.
- Ordinary dictionary words are not rewritten.
- Ambiguous candidates remain unchanged and appear in review UI.
- Target: zero pronunciation inconsistencies on the test glossary and fewer
  proper-name retries than baseline.

## Phase 3 â€” casting quality

### 3.1 Acoustic cast-distinctness scoring

Compare voice embeddings, median pitch, pitch range, speaking rate, and timbre.
Show `distinct`, `similar`, or `regeneration recommended`; automatically reject
hard gender/register contradictions while allowing user overrides with an
explicit audit entry.

Validation:

- Same-file and near-duplicate controls are detected.
- Deliberately distinct male/female and same-gender controls are not falsely
  rejected.
- Thresholds are calibrated on at least 30 labelled pairs.
- No non-speaking entity receives a profile.

### 3.2 Selective multiple candidates

Generate two or three candidates for the narrator, major speakers, and profiles
that fail distinctness checks. Minor speakers retain one candidate. Cache every
candidate and allow preview/selection at the one-time voice-review gate.

Validation:

- Candidate selection persists across restart and later chapter batches.
- Selecting a candidate invalidates only affected speakers' audio.
- Voice preparation time and storage increase are reported explicitly.
- User A/B preference improves over the single-candidate baseline.

### 3.3 Reference-sample quality

Prefer clean 10-20 second references with stable level, no music/noise, useful
phonetic coverage, and matching transcripts. Uploaded samples receive the same
checks and are never silently substituted.

Validation:

- Reject clipped, noisy, silent, too-short, transcript-mismatched samples.
- Compare clone similarity and intelligibility against current references.
- Existing generated-design workflow remains available.

## Phase 4 â€” prosody and chunking

### 4.1 Scene-level prosody plan

Generate a constrained scene state: mood, tension, narrator pace, character
state, and transition intent. Line controls derive from the scene state with
bounded speed/emotion changes.

Validation:

- Speaker attribution and source text remain unchanged.
- No controls exceed configured bounds.
- Blind listening comparison across calm, action, intimate, and dialogue scenes.
- Accept only if listeners prefer the plan and retry/WER rates are non-inferior.

### 4.2 Selective adjacent-line chunking

Merge adjacent same-speaker narration or monologue lines when voice, scene,
emotion, and effects are compatible. Preserve line-level timing metadata and
cap duration. Short/emotional/high-risk lines stay independent.

Validation:

- Exact word/order preservation and reversible line mapping.
- Pause boundaries remain within configured ranges.
- A failed chunk can be retried or split without regenerating unrelated chunks.
- Target: fewer TTS calls and improved blind-rated cadence with no increase in
  hard failures; rollback if retry cost offsets at least half the saved calls.

## Phase 5 â€” generated-audio and book-level QA

### 5.1 Character continuity/drift checks

Track embedding, pitch/register, pace, and timbre against both the reference and
the character's accepted history. Begin report-only; block only after threshold
calibration.

Validation:

- Detect deliberately swapped voices and pitch-shifted controls.
- False-positive rate below 2% on accepted baseline chapters.
- Cross-register voices are calibrated by character rather than global gender
  assumptions.

### 5.2 Join, pause, and chapter-continuity checks

Analyze assembled audio for abrupt noise/loudness changes, clipped boundaries,
cut breaths, repeated cadence, and implausible pauses. Surface only noteworthy
joins in the review UI.

Validation:

- Synthetic bad joins are detected; clean controls pass.
- False-positive rate below 2% before checks can block export.
- User reviews all flagged joins in the first three production books.

### 5.3 Book-level mastering consistency

Normalize toward one book target with true-peak protection while preserving
short-term dynamics. Current chapter spread (-22.3 to -20.3 LUFS) should be
tighter without sounding compressed.

Validation:

- Integrated chapter loudness within Â±0.5 LU of the book target.
- True peak within configured ceiling; no new clipping.
- Loudness range and short-term dynamics do not collapse beyond the configured
  tolerance.
- Blind comparison confirms no pumping or flattened dramatic delivery.

## Phase 6 â€” controlled performance experiments

### 6.1 Chapter/selection model-residency strategy

Use Phase 1 telemetry to quantify load/unload cost. Benchmark current
chapter-local switching against selection-level TTS then Whisper batching while
retaining restart-safe checkpoints.

Validation:

- Same accepted audio and retry decisions from fixed seeds where supported.
- Immediate stop releases models within the existing operational limit.
- No loss of partial-generation semantics.
- Adopt only with at least 10% wall-time improvement and no recovery regression.

### 6.2 TTS and Whisper co-residency A/B benchmark

The production policy is chapter-scoped rather than selection-scoped. The
initial TTS pass runs without Whisper resident; both models may coexist while
failed or flagged lines cycle through synthesis and validation; Whisper is
always unloaded at the chapter boundary. Compare wall time, model-load time,
TTS and Whisper throughput, peak VRAM, thermal throttling, failures, and
cleanup before widening that scope.

Validation:

- At least three identical chapter runs per mode.
- No OOM, GPU contention, quality change, or residual GPU allocation.
- Adopt only if median wall time improves at least 10%, p95 line latency does
  not regress more than 5%, and peak VRAM retains a safe configured margin.

### 6.3 Inference-setting benchmark

Evaluate supported attention kernels, compilation, and decoding parameters one
at a time. Smaller models or reduced sampling quality are a separate optional
profile, never the production-quality default.

Validation:

- Fixed voice/text corpus, three repetitions, warm and cold measurements.
- Compare WER/effective error, speaker similarity, retry rate, artifacts, and
  blind preference.
- Adopt only settings that improve speed with statistically non-inferior quality.

## Validation datasets

1. `sample_book-1` remains the working regression corpus; preserved evidence
   projects remain untouched.
2. Add a synthetic edge corpus: short shouts, invented names, numbers,
   repetitions, dialogue switches, long narration, accents, effects, silence,
   clipping, and deliberately swapped voices.
3. Add at least two legally usable books with different prose/dialogue styles
   before declaring thresholds genre-independent.
4. Maintain labelled cast-pair and join-quality datasets for acoustic threshold
   calibration.

## Promotion protocol

For every phase:

1. Run unit and deterministic regression tests.
2. Run a targeted artifact-level test.
3. Run the same fixed chapter before/after with cold and warm caches.
4. Compare wall time, peak VRAM, retries, pass/warning/fail counts, raw and
   effective error, speaker similarity, and produced duration.
5. Perform listening A/B whenever the change can affect audio.
6. Record results in this document or a linked dated report.
7. Keep the feature behind configuration until acceptance criteria pass.

## Execution status

- [x] Baseline and acceptance criteria documented.
- [x] Separate generation and validation fingerprints.
- [x] Accepted validation/speaker-similarity cache.
- [x] Detailed timing and cache telemetry.
- [ ] Risk-aware first-attempt policy (automated gate passed; listening A/B pending).
- [x] Synthesis pronunciation lexicon and explicit candidate-review workflow.
- [ ] Cast distinctness and candidate selection (objective warning evidence implemented; listening calibration and multi-candidate selection pending).
- [ ] Reference-sample QA (audio and transcript checks implemented; real upload E2E pending).
- [ ] Scene prosody and selective chunking (prosody-compatible grouping implemented; scene plan and listening A/B pending).
- [ ] Drift, join, and mastering consistency checks (join and book-loudness reporting implemented; drift and listening acceptance pending).
- [ ] Residency and inference A/B benchmarks (chapter-scoped residency accepted; attention/inference experiments pending).

### Implementation checkpoint â€” cache and timing foundation

- Added a separate SQLite `validation_results` cache keyed by project, line,
  WAV hash, expected text, voice-reference hash, glossary, Whisper model,
  validation thresholds, analyzer configuration, speed, and validation schema.
- Only `pass` and `accepted_with_warning` results are cached. Failed results,
  corrupt files, changed audio, changed text, changed voices, and changed
  validation inputs always miss.
- Cached results include speaker similarity, so a warm accepted line skips both
  Whisper transcription and speaker-similarity inference.
- Generation fingerprints retain a frozen backward-compatible marker that is
  no longer connected to validation schema changes. This preserves existing
  audio while allowing independent revalidation.
- Chapter responses and pipeline logs now expose cache hits/misses plus TTS,
  retry TTS, speaker similarity, Whisper transcription, audio analysis,
  model load/unload, cache I/O, fingerprint I/O, and total wall time.
- Targeted tests prove an identical second run invokes neither TTS, Whisper, nor
  speaker similarity; changing glossary inputs invokes validation but not TTS.
- Regression result at this checkpoint: 89 tests passing.

### Phase 1 acceptance benchmark â€” chapter 3

The same 50 cached chapter-3 WAVs were run twice through the real managed Voice
service. The validation-only reset preserved segments and generation
fingerprints while removing only completion/master artifacts.

| Measurement | Cold validation cache | Warm validation cache |
|---|---:|---:|
| Validation cache hits / misses | 0 / 50 | 50 / 0 |
| Chapter processing time | 39.380 s | 0.929 s |
| Whisper transcription | 28.198 s | 0 s |
| Speaker similarity | 5.110 s | 0 s |
| Audio analysis | 0.408 s | 0 s |
| Peak allocated VRAM | 4.31 GB | 4.20 GB |
| Final quality | 49 pass, 1 warning, 0 fail | identical |

Result: warm chapter validation improved by **97.6%**, exceeding the 80% target.
The composite hash of all 50 segment WAVs remained
`BE4F3A4B4D408FAE25C2E418D932CAE3691695D0F43B6C1D80064B23F6DA7D9B`,
proving no synthesis or audio mutation occurred. The mastered duration remained
567.4 seconds at -22.3 LUFS, the one-chapter M4B exported successfully, and
Voice, VoiceDesign, and managed Ollama ports were closed after both runs.

Phase 1 is accepted. Quality-affecting Phase 2 work may proceed behind feature
flags with its documented corpus and A/B gates.

### Phase 2.1 automated acceptance benchmark â€” risk-line corpus

The fixed six-line real-model corpus covers a short shout, concatenated
repetition, dense fictional names, fast delivery, ordinary narration, and
numbers. The unmodified control required two retries. A broad first-attempt
policy also required two retries and merely moved failures to the repetition and
fast-delivery lines, so that policy was rejected.

The refined policy changes only the short emphatic line. Three fresh-ID
repetitions all completed with 6/6 passes, zero warnings, zero failures, and
zero retries. Only `UNCLE!` was reported as adjusted; it transcribed as
`Uncle.` on the first attempt in all three runs. Its speaker similarity was
0.9624, 0.9580, and 0.9619. Unchanged control lines remained first-attempt
passes; glossary spellings retained zero effective text error.

Automated result: **pass**. Retry count improved from 2 to 0 (100%), hard
failures did not rise, and speaker identity was non-inferior. The matched
Starling listening pair was then approved on 2026-08-03: the authored delivery
was unintelligible and transcribed as `You` (50% WER, 0.864 speaker similarity),
while the clarity-policy delivery sounded good and transcribed exactly as
`Uncle` (0% WER, 0.951 speaker similarity). The production configuration now
enables the narrowly scoped policy. Longer dialogue, repetitions, dense names,
and ordinary narration remain unchanged. Evidence is stored under
`workspace/sample_book-1/quality_ab/risk_policy/`. The isolated models were
unloaded and all model service ports closed successfully.

Post-change regression result: 94 tests passing.

### Phase 2.2 implementation checkpoint â€” pronunciation data integrity

Pronunciation mappings now produce a separate `spoken_text` used only by TTS.
Authored `text` remains unchanged and remains the validation target. Mapping is
case-insensitive, longest-first, and non-recursive; malformed entries fail
closed. A spoken-form hash participates in the segment dependency manifest, so
changing a mapping invalidates dependent segments without invalidating lines
that do not use it. Null spoken forms are omitted from manifest hashing to keep
existing unaffected manifests backward-compatible.

Targeted tests cover source preservation, possessives, longest-phrase priority,
invalid-entry rejection, synthesis routing, and dependency invalidation. The
remaining Phase 2.2 work is automatic *candidate verification*: no inferred
pronunciation will be promoted merely because an LLM or a single ASR sample
suggested it.

Post-foundation regression result: 99 tests passing.

### Phase 2.2 acceptance checkpoint â€” pronunciation review

The pipeline now writes a book-local `pronunciation_candidates.json` after
scripting. Candidate extraction keeps repeated proper names and world terms,
recognizes multi-word character aliases, removes possessives, and suppresses
ordinary sentence starters and fragments of multi-word names. It never invents
or silently promotes a spoken form.

The dashboard Quality tab exposes unresolved candidates with occurrence count,
chapter coverage, and source context. A user-verified spoken form is stored in
the project-local `pronunciation_dict.json`; only chapters containing that term
are marked stale. Subsequent synthesis uses the verified spoken form while ASR
validation and exported metadata continue to target the authored spelling.

Acceptance evidence on `sample_book-1`:

- noisy extraction reduced from 94 unresolved tokens to 22 focused candidates;
- ordinary sentence starters such as `The`, `She`, `You`, and `But` are absent;
- `Ones Above` is represented once instead of separate `Ones` / `Above` rows;
- the live dashboard review panel renders 25 rows (22 unresolved, 3 verified)
  with no browser console errors;
- targeted pronunciation tests: 8 passing;
- complete regression suite: 102 passing.

This completes the deterministic and reviewable lexicon workflow. Listening
verification of individual project mappings remains a normal casting/QA action,
not an automatic inference step.

### Phase 3 implementation checkpoint â€” objective cast/reference evidence

Every generated reference now records duration, peak/RMS, DC offset, clipped
sample fraction, silence ratio, F0 median and 10th/90th percentile range,
spectral centroid, and spectral flatness. Pairwise cast diagnostics persist the
speaker-embedding similarity alongside pitch-range and spectral separation plus
a warning-only composite score.

Requested metadata no longer suppresses measured similarity: specifically, a
voice labelled male and one labelled female still produce a warning when their
actual embeddings and acoustic measurements are too similar. A warning is only
suppressed when both measured pitch and spectral centroid are materially
separated. This fixes the earlier circular logic where an incorrect generated
gender could hide behind the requested gender label.

Uploaded reference audio already required mono 24 kHz PCM, 3â€“30 seconds,
non-silence, and low clipping. It now also starts managed validation for a
one-time Whisper comparison against the user-supplied exact transcript before
replacing the current reference. Transcript mismatch fails closed; the managed
Voice process is released afterward. Orthographic-equivalent speech continues
to use the same normalization that fixed repeated forms such as
`Letsgoletsgoletsgo`.

Automated evidence:

- objective acoustic metric and pair-classification tests added;
- requested gender cannot suppress a measured similarity warning;
- uploaded transcript mismatch and orthographic-equivalence paths tested;
- complete regression suite: 106 passing;
- dashboard restarted on port 8000; Voice, VoiceDesign, and managed Ollama
  ports remained closed after verification.

These thresholds remain advisory until at least 30 labelled same/different
listening pairs are collected. No automatic cast rejection or regeneration has
been enabled yet. The next acceptance test is one real uploaded-reference E2E,
followed by a fresh voice bootstrap that persists the new pair diagnostics.

### Phase 4.2 implementation checkpoint â€” prosody-compatible grouping

Adjacent-line grouping now requires a stable measured control envelope in
addition to the existing speaker, voice, effects, paragraph, character, and
word limits. Calm/reflective, urgent/angry, whispered, bright, and neutral
families do not merge across one another, and a speed span above 0.12 forces a
boundary. Pure dialogue tags retain their existing safe merge exception.

This prevents a long neutral chunk from absorbing a short shout or whispered
transition merely because the same narrator reads both. Exact source offsets,
fragment IDs, word order, and paragraph boundaries remain unchanged. The
script fingerprint now includes `prosody-compatible-v2`, so projects with old
grouping are intentionally refreshed the next time book-wide scripting is run;
voice-only edits and ordinary audio-only partial batches still do not invalidate
scripts.

Automated evidence:

- neutral compatible narration still groups into fewer TTS calls;
- expressive length limits continue to keep high-risk lines short;
- a calm-to-urgent speed/emotion transition remains two independent segments;
- exact source-coverage assertions pass for both grouped and split cases;
- complete regression suite: 107 passing.

Acceptance is not yet complete: a calm/action/intimate/dialogue listening A/B
must show improved cadence with non-inferior validation retries before the
scene-level prosody phase can be marked accepted.

### Phase 5 implementation checkpoint â€” joins and book loudness

Mastering now records a report-only diagnostic for every segment boundary:
actual gap, preceding/following RMS, loudness delta, zero-gap sample jump, and
warning reason. A delta above 8 dB or a large zero-gap discontinuity is surfaced
but does not block mastering. Synthetic controls verify that a 26 dB jump is
flagged while similarly levelled segments remain clean.

Master manifests persist these join diagnostics, chapter LUFS, and true peak.
Export aggregates the available chapter measurements into
`export_quality*.json`, including median/min/max LUFS, spread, peak, and outlier
chapters. A spread above 1.0 LU is advisory; it does not alter or reject the
book.

The existing four mastered `sample_book-1` chapters confirmed the original
problem:

| Chapter | Current LUFS | True peak |
|---|---:|---:|
| 1 | -21.841 | -1.000 dBFS |
| 2 | -22.075 | -1.000 dBFS |
| 3 | -22.295 | -1.000 dBFS |
| 4 | -20.273 | -1.000 dBFS |

Current spread is **2.022 LU**. All chapters are peak-bound because the existing
ceiling attenuates the entire chapter to protect a very small number of peaks.

A `soft_limiter` mode was implemented and benchmarked on the same audio. It
produced -19.035 to -19.003 LUFS (**0.032 LU spread**) with
true peaks between -1.292 and -1.059 dBFS. Only 0.019â€“0.120% of samples entered
the limiter knee. The matched 30-second listening pair was approved as equally
natural on 2026-08-03:

- `workspace/sample_book-1/quality_ab/chapter3_global_ceiling.wav`
- `workspace/sample_book-1/quality_ab/chapter3_soft_limiter.wav`

The production configuration now uses `soft_limiter`. Because mastering
configuration participates in the master dependency fingerprint, previously
mastered chapters are remastered on their next selected run without
regenerating accepted speech segments. Complete regression suite after
activation: 114 passing.

### Phase 6.2 implementation checkpoint â€” retry-scoped model residency

The corrected long-form benchmark used a 53-word passage for three repetitions
per mode on the production GPU. With TTS and Whisper loaded together it retained
**18.12 GiB free**, transcribed at **0.0 WER**, and changed median TTS real-time
factor from **1.1245** to **1.1639**: a **3.50% slowdown**. The raw VRAM result
shows co-residency is safe, but keeping Whisper loaded through an entire chapter
would compound that slowdown and is not justified by a few seconds of saved
model switching.

The accepted implementation therefore enables co-residency only within a
chapter's validation/retry cycle. The long initial synthesis pass remains
TTS-only and Whisper is unconditionally released at every chapter request
boundary. This preserves the useful retry-loop swap reduction without imposing
the measured contention on the next chapter. A regression test verifies the
boundary unload even when co-residency is enabled.

Evidence:

- `brain/projects/sample_book-1/benchmarks/tts-whisper-residency-long-20260803.json`
- complete regression suite: 114 passing;
- production configuration: `keep_tts_and_whisper_resident: true`, with
  chapter-boundary cleanup enforced in code.

Selection-wide residency is rejected unless a future benchmark meets the
original 10% wall-time improvement gate with non-inferior quality. Attention
kernel and inference-setting experiments remain pending and must be evaluated
one variable at a time.

### Optimization Pass Checkpoint (August 2026)

- **FlashAttention:** Enabled natively for Qwen2.5 LLM text scripting (OLLAMA_FLASH_ATTENTION=1). Throughput increased from ~20 tokens/sec to **~28 – 36.8 tokens/sec** (~40% boost) on AMD RX 7900 XTX, effectively cutting character discovery time in half.
- **Qwen VoiceDesign:** Confirmed migration from Parler-TTS to Qwen3-TTS VoiceDesign (1.7B) to provide exact domain-matching initial audio seed references for Qwen Base cloning.
- **VAD Silence Pruning:** Integrated silero-vad into the openai-whisper fallback to cleanly strip silence. Eliminated 100% of the long-silence text hallucinations (which had incorrectly caused valid voice generations to fail with 100% WER). 
- **Tensor Caching:** Qwen3TTSEngine now computes and caches .pt tensors for speaker embeddings. Successive generations skip both audio loading and feature extraction inference entirely.
- **UUID File Management:** Changed VoiceLibraryManager to atomically save uploaded and bootstrapped voices with a hex suffix (e.g., 
arrator_955f3b7b.wav). Fully eliminated the [WinError 5] Access is denied crashes when Uvicorn held locks on actively served dashboard references.

### Phase 7: E2E Pipeline Scale Test & Validation Checkpoint
**Date:** August 2026
**Test Corpus:** Sample Book V4 (8 Chapters, 11,874 words, 67,927 characters)

**1. LLM Scripting (qwen2.5:32b with FlashAttention):**
- **Pass 1 (Character Discovery):** Completed in 630.0s (10.5 minutes), extracting 21 unique character profiles and capping safely at 20.
- **Pass 2 (Chapter Dialogue Generation):** Processed 8 chapters (split into 34 fragment chunks). Sustained peak streaming speed of **20.3 tokens/sec** throughout. Total LLM time was ~95 minutes. Zero malformed JSON or retry hallucinations encountered.

**2. Voice Validation (Qwen VoiceDesign 1.7B + Whisper STT):**
- **VAD Silence Pruning:** Confirmed working locally. When silero-vad is active in the main TTS environment, Whisper gracefully ignores pure silence, preventing 100% false-positive WER rejections. E2E pipeline now clears the voice bootstrap gate perfectly and stops at the oice_review manual casting gate as designed.


## Automated E2E Validation Runs (August 2026)

- **sample_book-e2e (Production 32B Benchmark)**:
  - Lines generated: 72
  - Average WER: 1.06%
  - Elapsed Time: 7.08 hours
- **sample_book-v14b-e2e-val (14B Model Validation)**:
  - Lines generated: 72
  - Average WER: 2.82%
  - Elapsed Time: 1.48 hours
- **sample_book-opt14b (Optimized 14B Benchmark)**:
  - Lines generated: 63
  - Average WER: 2.02%

*Note: 14B models show significantly faster generation times with a modest increase in WER compared to the 32B production models.*

## 2026-08-07 — Phase 3.3 Whisper Upgrade Evaluation

Benchmark of `small` vs `medium` Whisper models for quality validation. Tested on 30 lines (Chapter 1 of `sample_book-2`).

| Metrics | Whisper `small` | Whisper `medium` |
|---------|-----------------|------------------|
| Avg WER | 0.129 | 0.120 |
| Fails (WER > 20%) | 5 / 30 | 5 / 30 |
| Validation Time / Line | ~3.97s (CPU) | ~12.46s (CPU) |

**Historical conclusion (CPU benchmark)**: `small` outperformed `medium` for
this 30-line CPU test. This conclusion is superseded for the Windows AMD/ROCm
production path by the [2026-08-09 full-book E2E](e2e-run-2026-08-09.md): a
controlled failure-set comparison found that custom VAD preprocessing removed
valid short/repeated speech, while raw GPU `large-v3` transcribed the intended
audio. The current configuration therefore uses raw `large-v3`; WER thresholds
remain unchanged.
