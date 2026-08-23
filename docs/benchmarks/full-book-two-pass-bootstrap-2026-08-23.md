# Full-book two-pass scripting and voice bootstrap — 2026-08-23

## Scope and caveat

This report records spoiler-free operational and quality measurements for the
63-chapter production validation run. A game shared the GPU during scripting,
so scripting wall time is useful as an observed workload measurement, not as an
isolated model benchmark. Artifact integrity, deterministic audit results,
confidence trails, validation outcomes, and retry/failure behavior remain valid
quality evidence.

## Scripting

- Director mode: separate book-wide character analysis followed by scripting.
- Chapters: 63.
- Script lines: 9,028.
- Dialogue fragments audited: 3,937.
- Narrator quotations audited: 37.
- Observed scripting wall time: 15,139.6 seconds (about 4 hours 12 minutes).
- Mean observed wall time: 240.3 seconds per chapter.
- Deterministic named-tag repairs: 57.
- Final Gemini-resolved assignments in the script artifact: 11.
- Final attribution audit: passed, zero blocking issues.
- Lines left requiring manual attribution review: zero.
- Speaker-confidence distribution: 3,922 at or above 0.90; 14 from 0.80
  through 0.899; none below 0.80.

The run was 28% slower than the immediately preceding 11,795.9-second joint
run and 42% faster than the earlier 26,104.5-second joint run. Configuration,
model, director mode, and GPU contention differed, so these comparisons do not
isolate any one cause.

## Voice bootstrapping

- Durable stage interval: approximately 50 minutes 14 seconds.
- Final cast entries: 147.
- Canonical assigned entries: 62.
- Optional alternatives: 85.
- Ready entries: 147.
- References using configured seed text because script dialogue was
  insufficient: 2.
- Transcript checks recorded: 147.
- Mean reference WER: 0.0276.
- Maximum retained reference WER: 0.1875, below the configured 0.20 gate.
- WER distribution: 108 exact-zero; 12 above 0 through 0.05; 15 above 0.05
  through 0.10; 12 above 0.10 through 0.20.
- Retained clipping, excessive-silence, excessive-quiet, DC-offset, or
  full-scale-peak findings: zero.
- Cast pair diagnostics: 1,953 total; 1,933 distinct and 20 marked similar.
- Acoustic similar-pair warning occurrences: 40, one on each side of the 20
  pairs.
- Design-time contrast warnings retained from character analysis: 54. These
  record that the original profile needed a dedicated contrast direction; they
  are not audio-generation failures.

Bootstrapping completed without a canonical WER failure, pipeline error, OOM,
or service restart. The managed voice service shut down normally after the
pipeline parked at the required voice-review gate. The similar pairs remain an
intentional human-review concern and were not automatically approved.

## Run-specific defects found and corrected

The synchronous bootstrap request left the job database holding the prior
voice-cast payload even after the new artifact and revision were committed. A
direct status poll could therefore display 107 stale entries and 16 stale
similar pairs while `voice_cast.json` correctly contained 147 entries and 20
pairs. The pipeline now writes the completed cast into job state together with
its revision. The status endpoint also reconciles a cached cast when its
fingerprint does not match the durable revision. Regression coverage verifies
that a direct status request returns the post-bootstrap artifact.

The bootstrap progress snapshot remained at a generic 0/1 while VoiceDesign,
automatic redesign, WER validation, acoustic diagnostics, and cast comparison
were active. This did not affect output, but it made healthy work look stalled
and allowed the stale previous cast to be mistaken for current progress. A
stage-aware progress transport is a priority finding for the subsequent
application-wide audit.
