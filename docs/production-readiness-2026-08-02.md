# Production-readiness changes — 2026-08-02

This note records the decisions implemented after reviewing the failed
`sample_book-v32b-prod-e2e` run and the evidence from the replacement
`sample_book-1` production baseline completed on 2026-08-03. The original
`sample_book` and `sample_book-v32b-prod-e2e` projects remain preserved as
evidence artifacts.

## 2026-08-03 baseline result

Status: **supervised release candidate**. Partial chapter production is ready;
unattended full-book production still requires the operational tests listed
below.

- Book-wide analysis and scripting completed for 8 chapters and produced 540
  grouped utterances with `qwen2.5:32b`.
- Source-backed identity adjudication consolidated `Sixth of Dusk` and `Dusk`
  without allowing suffix-only merging.
- The speaking-only cast produced 13 ready profiles, including previewable male
  and female narrator candidates. The user selected `narrator_male`, and the
  assignment is present in the chapter segment manifest.
- Voice approval was required once, persisted across dashboard restart, and no
  longer reopens after approval.
- The Prologue-only batch generated and validated 72/72 segments: 70 clean
  passes, 2 `accepted_with_warning`, 0 flagged, and 0 failed. There were 14
  retries and final average WER was 2.02%.
- Mastering produced 519.2 seconds at -21.8 LUFS. Partial M4B export produced a
  7.3 MB, one-chapter audiobook.
- Chapters 2-8 were explicitly skipped, confirming partial selection behavior.
- Voice, VoiceDesign, and managed Ollama ports were closed after completion;
  scheduling remained disabled.
- The complete regression suite passed: 87 tests after the multi-chapter fixes
  described below.
- Generation required 1,629.87 seconds for 519.2 seconds of mastered content
  (about 0.32x realtime). This is functional but below the desired unattended
  throughput and remains a production performance concern.

## Implemented decisions

- Starting a dashboard project immediately interrupts any active project.
  In-flight work may be lost; completed fingerprints, chapters, and segments
  remain eligible for reuse.
- A global OS lock prevents dashboard and scratch workers from sharing the GPU
  pipeline concurrently.
- Every newly inserted job requires one voice-review approval. Existing SQLite
  rows that predate the feature remain grandfathered.
- Private-LAN peers listed in `dashboard.trusted_lan_cidrs` and loopback may use
  the dashboard without a token. Trust is based only on the TCP peer.
- Soft duration/noise/score anomalies become `accepted_with_warning` when text,
  speaker, clipping, silence, and pacing hard checks pass.
- Character suffixes are never direct merge evidence. Explicit aliases merge;
  possible title/short-name pairs require a verbatim book excerpt from the
  automatic identity adjudicator, or both a positive adjudication and a
  source-verified long-name-to-short-name continuation in one paragraph. This
  handles introductions such as `Sixth of the Dusk` followed by `Dusk` without
  merging unrelated pairs merely because one name contains the other.
- Pass 2 cannot create new cast members. Low-confidence metadata receives three
  model attempts, and deterministic fallback never selects a speaker based on
  gender or proximity.
- Ollama streaming has a finite inactivity watchdog, one unload implementation,
  and a force-close cancellation path.
- Chapter-scoped retry co-residency is enabled after controlled short and
  53-word benchmarks; Whisper is released before the next chapter's initial
  TTS pass.
- Every speaking profile receives a stable, unique casting-palette direction;
  this reduces same-gender VoiceDesign collapse even when the analyzed source
  descriptions are not textually similar. Acoustic similarity diagnostics use
  the actual character gender and measured pitch metadata when suppressing
  cross-register false positives.

## Baseline measurement coverage

The replacement baseline captured the following:

1. Pass 1 time and identity-adjudication decisions/evidence.
2. Every Pass 2 request's fragment count, elapsed time, retries, and token rate.
3. Per-chapter script time and speaker-confidence corrections.
4. Voice-design and preview time per speaking profile.
5. Aggregate generation, mastering, export, load, and unload timing. Separate
   TTS-versus-Whisper timing is not yet exposed by the chapter API.
6. Confirmation that completion releases app-owned GPU services. Peak VRAM was
   not captured as a time series.
7. Counts for `pass`, `accepted_with_warning`, `flagged`, and `fail`.

The earlier VRAM-contention concern has now been tested directly. The corrected
53-word, three-repeat benchmark retained 18.12 GiB free, produced 0.0 WER, and
measured only a 3.5% median TTS real-time-factor slowdown while both models were
loaded. This is not worthwhile during an entire chapter, but it is safe and
useful for avoiding repeated swaps inside the much smaller retry loop. The
implementation therefore unloads Whisper at every chapter boundary.

## Acceptance gates for the next E2E

- All unit tests pass before starting models.
- Voice review is visibly required exactly once.
- A second-project start interrupts the first and never overlaps GPU workers.
- No character is merged without explicit alias or cited source evidence.
- Pass 2 creates no unknown registry entries and performs no arbitrary pronoun
  assignment.
- Soft-only warnings do not block mastering; hard defects still do.
- The selected chapter masters and exports successfully.
- Final Voice/Ollama processes and GPU allocations are released.

## Remaining promotion gates

The following must pass before changing the verdict to unattended production
ready:

1. Generate and export at least three consecutive chapters in one selection.
2. Immediately interrupt active generation, accepting the in-flight loss, and
   verify Voice/Ollama/GPU cleanup plus cached-segment reuse on resume.
3. Restart the dashboard between interruption and resume and verify project
   state and approved casting survive.
4. Exercise a real working-hours close/open transition, then restore the
   user's normal disabled schedule.
5. Start a second disposable project while work is active and prove that the
   first worker is interrupted without GPU overlap.
6. Record separate TTS, Whisper, retry, model-load, mastering, and export timing
   plus peak VRAM during a representative multi-chapter run.
7. Perform a subjective listening pass over joins, pacing, voice distinction,
   narrator consistency, and the two soft-warning lines (`ch01_0063` and
   `ch01_0067`).

The automated operational run below may satisfy gates 1-4 and 6. Gate 5 must
use a disposable new project rather than either preserved evidence project.
Gate 7 remains a user listening decision.

## 2026-08-03 chapters 2-4 operational result

Status remains **supervised release candidate**. The representative
three-chapter run passed the generation, interruption, restart, scheduling,
mastering, export, and cleanup gates, while also exposing and fixing a generic
fictional-name validation gap.

- Immediate cancellation during chapter 2 released the app-managed services in
  19.4 seconds and preserved two completed segments. After dashboard restart,
  project state, selection, and approved casting survived.
- A real closed working-hours window parked the worker with model ports closed;
  changing to an open window resumed it automatically. The original disabled
  `Europe/Bucharest`, Monday-Friday `10:00-05:00` configuration was restored.
- Chapter 1 was reused and excluded from the batch. No book-wide scripting or
  voice bootstrapping recurred.
- Chapters 2-4 completed 202/202 final segments: 199 pass, 3
  `accepted_with_warning`, and 0 fail. Final-attempt retry counts were 5, 0,
  and 10 respectively. Raw average WER was 4.91%; glossary-aware effective
  error correctly discounts close ASR spellings only for approved book terms.
- The three warnings were soft-only: `ch02_0124` (WER 0), `ch03_0044` (WER
  7.69%), and `ch04_0030` (WER 16.67%). None had a hard clipping, silence,
  pacing, speaker-identity, or text failure.
- A false rejection of `ch03_0120` exposed two defects. First, validation used
  whole-line similarity for multiple fictional-name variants. Second,
  non-speaking world entities were absent from the validation glossary. The
  validator now aligns tokens and discounts close substitutions only when the
  expected token is book-approved; insertions, deletions, and ordinary prose
  changes retain full cost. The pipeline now derives repeated proper names and
  world terms from the completed book script without adding non-speakers to
  casting. `Patji` and `Eelakin` then passed live from cached audio in 52.9
  seconds. Regression coverage is 87 passing tests.
- Chapter masters are present for chapters 2-4: 492.9 seconds at -22.1 LUFS,
  567.4 seconds at -22.3 LUFS, and 594.4 seconds at -20.3 LUFS. All three
  manifests use the selected `narrator_male` profile.
- Partial export produced `sample_book-1_chapters_2-4.m4b`, 23.7 MB and 27:34
  across three chapters.
- Peak sampled VRAM was 5.15 GB. The sampler stopped at the deliberately
  terminal validation failure and therefore does not cover the final corrected
  resume; stage logs remain authoritative for that portion. Separate TTS and
  Whisper timing is still not exposed, so the timing/VRAM promotion gate is
  only partially satisfied.
- Terminal state is `selection_complete`; Voice, VoiceDesign, and managed
  Ollama ports are closed. The normal schedule remains disabled.

### Promotion gates still open

1. Start a second disposable project while another disposable project is
   actively generating and prove immediate interruption without GPU overlap.
2. Capture uninterrupted time-series metrics through a terminal multi-chapter
   run, including separate TTS, Whisper, retry, load/unload, mastering, and
   export durations.
3. Complete the user listening pass for joins, pacing, voice distinction,
   narrator consistency, and all accepted soft warnings.
