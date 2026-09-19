# Deferred live validation plan — 2026-08-10

**Status:** Historical record — A dated record of what was done and why. Evidence, not a specification -- do not implement from it.

This is the model/GPU/listening work deliberately excluded from the 2026-08-09
implementation session. Run gates in order and stop at the first regression;
do not spend a full-book run to diagnose a lower-tier failure.

Execution results and decisions are recorded in
[live-validation-results-2026-08-10.md](live-validation-results-2026-08-10.md).

## Gate 0 — establish the exact candidate

- Review and split the current broad working tree into coherent commits.
- Record commit, source EPUB hash, config fingerprints, environment preflight,
  `pip check`, and existing artifact hashes in the verification manifest.
- Run the full low-resource CI-equivalent gate on that exact tree.

## Gate 1 — fixed audio/model fixtures

- Confirm health reports Qwen TTS, `sdpa`, OpenAI Whisper `large-v3`, AMD/ROCm,
  and raw audio with VAD off.
- Run cold and warm TTS fixtures at least three times across short emphasis,
  ordinary dialogue, long narration, repeated words, fictional names, and two
  voices. Record median/p95 wall time, audio duration, RTF, WER, similarity,
  retries, allocated/reserved/peak VRAM, and runtime warnings.
- Keep current defaults as control. Test the disabled length-aware token cap
  separately with truncation detection and retry. Do not test unsupported Flash
  Attention 2. Promote only a >=10% median RTF win without a quality regression.
- Exercise raw-audio validator fixtures, including the prior short/high-pitched
  and repeated-speech VAD failures, before any validator setting change.

## Gate 2 — scripting fixture

- Compare the current 350-word/40-fragment bounds with a small one-variable
  matrix. Capture prompt/eval tokens, tokens/second, wall time, retries,
  fallback count, source coverage, unique IDs, and unknown-speaker count.
- Require complete source-span coverage and no attribution regression. Promote
  only a >=20% Pass-2 wall-time improvement.

## Gate 3 — representative chapter

- Run one representative chapter through scripting, generation, validation,
  mastering, and export using a disposable project ID.
- Interrupt after a checkpoint and prove synthesis reuse, validation reuse,
  incomplete-manifest behavior, selected-attempt reporting, and artifact hashes.
- Compare chapter manifest, ordering, pauses, duration, LUFS, peaks, WER,
  similarity, warnings, retries, and RTF with the 2026-08-09 baseline.

## Gate 4 — listening and operational ownership

The old queue is now a triage-only artifact because of the confirmed echo
incident. Follow
[listening-qa-gate-2026-08-10.md](listening-qa-gate-2026-08-10.md) and perform
the authoritative join review on clean regenerated audio.

- Listen to all 36 accepted warnings and all 63 join warnings from
  `sample_book-13`, prioritizing chapters 2, 4, 7, and 8, plus a clean control
  sample. Save dispositions in the dashboard.
- Tune local gain/crossfade only if confirmed defects form a repeatable set;
  compare changes blindly and never crossfade across requested pauses.
- Run the disposable second-project interruption test. Confirm the second job
  cannot acquire synthesis ownership before the first acknowledges interruption
  and releases models, process ownership, port, lock, and GPU allocation.

## Gate 5 — release evidence

- After lower gates pass, run one clean unattended representative multi-chapter
  project. Reserve the full `sample_book-13` rerun for the release candidate.
- Use stage-appropriate monitoring intervals: short during startup/transition,
  progressively longer during stable scripting/TTS, and immediate checks only
  near expected completion or when progress becomes stale.
- Archive the manifest, environment report, metrics summary, final hashes,
  warning/listening dispositions, and cleanup/release evidence.
