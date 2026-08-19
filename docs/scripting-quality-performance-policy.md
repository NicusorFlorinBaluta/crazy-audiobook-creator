# Scripting quality and performance policy

## Priority order

Scripting optimizations are evaluated in this order:

1. Preserve or improve final audiobook quality.
2. Preserve source fidelity, attribution auditability, confidence, escalation,
   and manual review.
3. Improve completion time and resource use.

A faster result is not accepted when it weakens a higher-priority requirement.
Model changes and schema changes require separate validation so a speed gain
cannot conceal an attribution or delivery-quality regression.

## Compact metadata contract

The source fragments and creative decisions are not compacted. The model still
receives the complete local source context and must return:

- `id`, `emotion`, and `speed` for every fragment;
- `speaker`, `speaker_confidence`, and source-grounded `speaker_evidence` for
  every dialogue fragment;
- an explicit `dialogue_kind` and evidence for non-spoken quotations and
  reported collective speech;
- scene changes, deliberate pause exceptions, chapter continuity summary, and
  source-evidenced character updates when applicable.

The model may omit only values that the application can restore without an
interpretive decision:

- narrator ownership for structurally non-dialogue fragments;
- the normal `spoken` classification for dialogue assigned to a character;
- a repeated scene index after the scene's first fragment;
- routine pause values implied by the required emotion and speed.

Missing emotion or speed is a structural error. Missing or uncertain dialogue
attribution enters focused correction and, if still unresolved, becomes a
low-confidence review item. Compaction never converts uncertainty into a
release-grade guess.

## Promotion gates

The compact contract is promoted only when all of these checks pass:

- Exact source coverage and fragment order are unchanged.
- Expanded compact rows are equivalent to canonical rows for speaker,
  confidence, evidence, dialogue classification, emotion, speed, scene state,
  and effective pauses.
- Focused attribution, external escalation, and manual review continue to see
  the canonical metadata fields.
- The complete automated test suite passes.
- Live requests show a material reduction in received metadata size without an
  increase in structural failures, fallbacks, attribution issues, or missing
  delivery fields.

The runtime records received and expanded canonical character counts for every
full scripting request. This is a serialization-size proxy, not a claim about
raw model speed. Chapter time, output tokens, safeguard terminations, focused
repairs, and review fallbacks remain the outcome metrics.

## 2026-08-16 baseline

Before compact output was enabled, representative 40-fragment requests from the
current full-book run produced 3,815-4,088 output tokens, 11,776-12,782 response
characters, and took 581-650 seconds at approximately 6-7 tokens/second. One
short chapter reached the 900-second generation safeguard and used conservative
review metadata. Chapter 5 took 2,082.8 seconds for 1,643 source words.

The first live compact requests must be compared with this baseline. A smaller
payload alone is insufficient for promotion if quality or review metrics worsen.

## Initial live compact results

The first two 40-fragment batches after deployment produced:

| Batch | Full output | Full request | Canonical expansion | Focused check | Total |
|---|---:|---:|---:|---:|---:|
| 1 | 982 tokens / 3,213 chars | 210.0 s | 8,621 chars (62.7% avoided) | 232 tokens / 40.4 s | 250.5 s |
| 2 | 1,630 tokens / 5,906 chars | 245.5 s | 8,810 chars (33.0% avoided) | 73 tokens / 18.7 s | 264.2 s |

Both batches passed structural validation. Neither triggered a full retry,
unresolved-attribution fallback, generation safeguard, or missing-delivery-field
error. The second batch also retained and admitted a source-evidenced character
update, confirming that compact line metadata did not remove the joint-discovery
channel.

Relative to the 581-650 second legacy full-request baseline, the initial compact
batches reduced total full-plus-focused batch time to about 4.2-4.4 minutes.
This is a provisional scripting promotion based on canonical metadata and review
quality gates. It is not a claim of final acoustic equivalence: synthesis,
automated audio validation, and listening review remain authoritative for the
finished audiobook.

## Targeted repair paths (2026-08-16)

Two focused repair paths were added after the initial compact baseline to
address the defect classes observed in the first live batches:

### Focused delivery repair

After each full-chunk response, every fragment is checked for missing or
missing-value `emotion` and `speed` fields. If any are absent, a small,
context-aware repair call covering only the affected row IDs is issued
(maximum two rounds). The repair request is cheaper than a full-chunk
retry because it carries only the affected source fragments plus their
preceding context.

Metrics recorded per chunk: `delivery_focused_retries` (total fragments
repaired), `delivery_focused_rounds` (number of rounds used), and
`delivery_issue_counts` (per-field breakdown). A chunk that exhausts both
repair rounds without resolving all delivery fields escalates to a
full-chunk retry rather than accepting bad metadata.

### Strict narrator correction for spoken dialogue

After the standard focused attribution retry, any remaining fragments that
combine a confirmed dialogue tag with narrator assignment receive one
additional stricter correction pass. The correction prompt explicitly
forbids narrator for those fragment IDs and lists only valid character
IDs. If the stricter pass still returns narrator, deterministic repairs
are applied (gender and dialogue-tag matching) before the fragment is
marked low-confidence for review.

This separates two failure modes that require different treatment:

| Mode | Root cause | Recovery |
|---|---|---|
| Compact-schema omission | Model omitted required field | Focused delivery repair (max 2 rounds) |
| Genuine attribution ambiguity | Context insufficient | Low-confidence review item, Gemini escalation |
| Narrator on spoken dialogue | Model applied wrong role | Strict correction pass + deterministic repair |

### Metrics distinguishing failure modes

Call metrics now record:

- `delivery_focused_retries` / `delivery_focused_rounds` / `delivery_issue_counts` — compact-schema field omissions
- `focused_retries` — first-pass attribution issues
- `strict_attribution_retries` — narrator-on-spoken-dialogue corrections
- `local_repairs` — deterministic (no LLM call) fixes
- `fragment_fallbacks` — genuinely unresolved, sent to review
- `structural_failures` — full-chunk parse/structure failures

The distinction is important for calibration: high `delivery_focused_retries`
indicates the compact schema prompt needs tightening, while high
`fragment_fallbacks` indicates genuine literary ambiguity that Gemini
escalation or human review should handle.
