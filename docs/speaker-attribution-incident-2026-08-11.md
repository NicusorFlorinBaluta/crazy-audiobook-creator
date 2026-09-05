# Speaker attribution incident and selective repair (2026-08-11)

**Status:** Historical record — A dated record of what was done and why. Evidence, not a specification -- do not implement from it.

## Impact

The completed `sample_book-14` release contained multiple spoken quotations
assigned to Narrator. The visible Starling/Frost exchange in chapter 1 was not
a dashboard rendering problem: those assignments were present in the saved
chapter script and therefore in the synthesized audio.

The pre-repair source audit found 34 blocking issues across chapters 1, 2, 3,
4, 5, and 8. Thirty-three were Narrator-owned quotations without an explicit
non-spoken classification. One additional quote, `"We should explain it."`,
was assigned to Dusk although its leading source tag attributed it to an
anonymous collective (`They said,`).

## Root causes

1. After a semantic attribution response failed, the script generator used a
   conservative fallback that silently assigned unresolved dialogue to
   Narrator. The old regression test explicitly accepted that behavior.
2. Action-only beats such as `smiled` and `nodded` were treated as speech tags,
   producing false contradictions and triggering avoidable full-chunk retries.
3. Attribution validation considered trailing tags but did not safely
   distinguish a leading tag from the preceding turn's trailing tag.
4. There was no source-to-final-script release gate before generation/export.
5. Grouping could merge a classified quoted term into surrounding narration
   and lose the quote classification.

## Fix

- Unresolved spoken dialogue now fails closed. Structural JSON failures retain
  bounded retries, but a semantic failure cannot degrade to Narrator.
- Focused corrections are batched by conversation context and can refine only
  residual invalid turns instead of repeating a full LLM chunk.
- Every Narrator-owned quotation must be explicitly classified as either:
  - `non_spoken_quote`, with source-grounded evidence; or
  - `reported_collective_speech`, with an adjacent anonymous plural speech tag.
- Exact named/generic tags, anonymous collective tags, and short lexical/scare
  quotes embedded in narration receive deterministic source-grounded repairs.
- Leading tags are recognized only when their punctuation introduces the next
  quote; a completed trailing tag is not reused for the following turn.
- Action-only beats are no longer speech tags.
- `attribution_audit.json` is a release-blocking gate before generation and
  export.
- Grouping cannot cross dialogue-classification boundaries.
- Null audit metadata remains compatible with legacy audio manifests, so
  unchanged chapters and segments can be reused.

## Repair and evidence

The repair was built in memory and installed only after a complete audit
passed. Original scripts were preserved under:

`brain/projects/sample_book-14/recovery/attribution-20260811T175718Z`

The focused repair used 8 model requests and 425.8 seconds of model wall time.
A final deterministic correction reclassified chapter 5's inline quoted term
`"trappers"` as narration without another model request.

Final audit:

- chapters: 8
- quoted source fragments: 208
- intentional Narrator quotations: 4
- blocking issues: 0
- changed chapters: 1, 2, 3, 4, 5, 8
- reusable unchanged chapters: 6, 7

The four intentional Narrator quotations are two lexical/scare-quoted terms
(`"child"`, `"trappers"`) and two short anonymous collective reports
(`"Why?"`, `"We should explain it."`).

## Regression coverage

Tests cover fail-closed unresolved speech, batched alternating-turn repair,
leading versus trailing tag directionality, action-only beats, named and
collective contradictions, embedded lexical quotes, grouping boundaries, the
release audit, and managed Voice Server startup readiness. The final mocked
suite before audio regeneration passed 218 tests with 2 skips.

## Audio recovery

Selective regeneration was started after verifying reconciliation retained
chapters 6 and 7. Within changed chapters, unchanged segment fingerprints are
also reused.

The recovery completed successfully on 2026-08-11:

- total resumed-pipeline wall time: 11,668.9 seconds (194.5 minutes)
- final segments: 636
- validation: 627 passed, 9 accepted with soft audio warnings, 0 failed,
  0 flagged, and 2 retries
- average WER: 0.0209
- silence/clipping failures: 0 / 0
- final chapters: 8 generated and 8 mastered
- book duration: 1:16:34
- export time: 74.2 seconds
- output size: 72,554,039 bytes (69.2 MiB)
- SHA-256:
  `2F64634E39398E779D9701B1F5A4E5228A1BBB5920BC251C3D53DEDF0CAB5B4C`
- chapter loudness: median -19.006 LUFS, spread 0.008 LU, no outliers
- highest chapter peak: -1.152 dBFS

Artifact reconciliation reported no missing or stale chapter/master manifests.
The managed Voice Server and Ollama processes and their model ports were
released after export; the dashboard remained online.

The nine accepted warnings are non-blocking soft audio checks. Eight have an
exact transcription match. `ch07_0031` was the only warning with a non-zero
WER (0.2): automated transcription heard `"Fight others"` instead of the
expected `"Bite others"`. Human listening on 2026-08-11 confirmed that the
rendered audio correctly says `"Bite others, Dusk instructed again,"` so the
warning is a transcription false positive and does not block release.
