# Separate character pass and early attribution gate — 2026-08-23

## Decision

Production uses a dedicated book-wide character extraction pass before
scripting (`script.joint_analysis: false`). Joint discovery remains available
only for experiments.

Audiobook quality is the primary objective. Speed improvements are accepted
only after cast completeness, explicit-tag attribution, confidence/review
behavior, and source coverage remain safe.

## Evidence

The completed joint run produced 138 blocking attribution-audit findings across
29 chapters. The 14 reused Qwen 2.5 chapters contained 45 findings; the 49 Qwen
3.8 chapters contained 93, so changing the scripting model alone did not remove
the coupling failure. A named recurring speaker was absent from the finalized
registry, while explicit source tags were assigned to a generic voice. Gemini
received 109 escalations and could still endorse the generic ID because the
missing identity was not in its allowed candidate set. Other high-confidence
contradictions were discovered only by the final deterministic audit.

## Safeguards

- Complete and fingerprint the cast before scripting.
- Repair only unique, registered names from attached source tags.
- Treat a unique, gender-compatible registered self-identification as a
  deterministic scene identity reveal. Relabel only the contiguous generic
  speaker cluster; conflicting identities or gender remain blocking.
- Never allow external rationale to name one identity while returning another,
  or to collapse an explicit name into a generic voice.
- Persist unresolved attribution for review.
- Enforce the attribution audit before voice bootstrapping, not merely before
  audio generation.
- Retain Qwen 3.8 non-thinking mode, 16K context, 60-fragment batching, and
  adaptive splitting; these performance choices are independent of cast order.

The additional character pass is an intentional performance cost. Avoiding a
full invalid scripting/audio run is more valuable than eliminating that pass.

## Sample-book regression validation

The eight-chapter representative sample was rerun end to end after deployment.
The dedicated registry retained the recurring named speaker that had been lost
by joint discovery. The final `speaker-attribution-v4` audit covered 208 quoted
source fragments and passed with zero blocking findings. The identity-reveal
repair resolved 18 turns in one continuous scene, retained the known recurring
speaker's named assignments, left no generic speaker in the sample, and created
no manual-review items. The run had no Ollama retries, safeguard terminations,
or pipeline errors.

This regression is intentionally stronger than checking the formal audit alone:
the first sample run exposed a generic speaker that later identified itself as
a registered character. That semantic continuity case is now both repaired and
release-gated. A bare proper name is accepted as self-identification only when
it directly answers an identity question; calling another character's name is
not sufficient.

## Full-book production validation

The subsequent 63-chapter two-pass run produced 9,028 lines in 15,139.6 seconds
(about 4 hours 12 minutes, or 240.3 seconds per chapter). The workstation GPU
was also serving a game during this run, so this wall time is a contaminated
throughput measurement and must not be treated as an isolated model benchmark.
It was 28% slower than the immediately preceding 11,795.9-second joint run, but
42% faster than the earlier 26,104.5-second joint run. Quality evidence, retry
behavior, and output integrity remain valid even when raw GPU throughput does
not.

The production attribution audit covered 3,937 dialogue fragments and 37
narrator quotations across all 63 chapters. Deterministic repair corrected 57
named-tag assignments. The external validation trail contains successful
Gemini triage/adjudication decisions, and the final audit passed with zero
blocking issues and zero lines left requiring manual attribution review. The
previously missed recurring character remains registered and has no surviving
generic `unnamed_woman` assignment.

## Recovery incident and follow-up

The first post-scripting resume exposed two independent application bugs, not a
model failure:

- The manual-start path referenced `PAUSED_DEPLOYMENT`, while the enum value is
  `DEPLOY_PAUSED`; this made Resume return HTTP 500 from a scheduled pause.
- The final attribution audit repeatedly compiled dynamic regular expressions
  for every line/name pair. One chapter took 13.44 seconds and made about 65.5
  million calls; tokenized evidence matching plus cached name patterns reduced
  the same chapter to about 0.08 seconds. A complete audit pass now takes about
  4.8 seconds.

Audit contradictions are now queued for external validation even when the
local model originally assigned high confidence. This closes the gap where the
final gate could detect an issue that Gemini never received. Resume behavior,
audit escalation, and the optimized matching path have regression coverage.
