# Audio echo incident — 2026-08-10

## Summary

Listening to the `sample_book-13` export from the 2026-08-09 full-book run
revealed widespread echo-like, reverberant, or doubled voice output. The book
passed transcript, clipping, silence, speaker-similarity, mastering, and export
checks, but those checks did not measure this perceptual artifact.

The affected export must not be treated as a listening-quality baseline.

## Root cause

The TTS engine derived subtle pitch, tempo, and tone settings from script
emotion and speed. SoX was unavailable on the test machine, so pitch and tempo
were processed by the librosa phase-vocoder fallback. Phase-vocoder processing
can smear transients and create chorus/reverberant artifacts in speech even
when transcription and speaker identity remain correct.

The issue occurred in individual segment WAVs before chapter mastering. Voice
references had signal characteristics similar to neutral generated lines, so
reference design and the 30 ms join crossfade were ruled out as the primary
cause.

## Evidence

- 316 of 563 full-book lines (56.1%) received non-identity post-processing.
- Processed segments had median delayed correlation `0.10449`, versus `0.07016`
  for neutral segments and `0.07126` for voice references.
- Processed median spectral flux was `0.19231`, versus `0.21257` for neutral
  segments, consistent with transient smearing.
- The 2026-08-10 representative chapter was an inadequate perceptual guard:
  only 2 of its 24 lines used the affected path.
- Dry/wet blending was already disabled for Qwen generation. The remaining
  problem was the transformation algorithm itself, not a doubled blend.

These measurements support the reported listening symptom, but the listener's
perception is the decisive quality evidence.

## Fix

- Production post-processing now defaults to disabled. Qwen Base output is
  preserved except for numeric peak protection.
- The phase-vocoder fallback is no longer selected implicitly. If optional
  post-processing is enabled without a quality-approved backend, pitch/tempo
  changes are skipped with a warning.
- Re-enabling the legacy phase vocoder requires the explicit experimental
  `allow_phase_vocoder_fallback: true` setting.
- Synthesis fingerprints include the versioned `clean-output-v1` policy,
  enabled state, and unsafe-fallback state. Old processed cache entries cannot
  satisfy a clean-output fingerprint.
- Validator retry logic no longer labels emotion as adjusted when production
  post-processing is disabled.
- Regression tests cover the clean default, explicit opt-in, fallback refusal,
  and audio-policy fingerprint identity.

## Recovery

The echo cannot be reliably removed from the mastered M4B. Regenerate affected
speech from TTS under the clean policy. Voice references can be reused.

Because the post-processing policy is now part of the generation context, a
normal regeneration run will reject old segment cache entries. For maximum
confidence, regenerate the complete selected chapters rather than attempting
to repair the mastered file. Validate with matched listening samples that
include reflective, soft, intense, and non-default-speed lines before starting
another full-book release run.
