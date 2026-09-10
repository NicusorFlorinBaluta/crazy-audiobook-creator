# Targeted block adjudication — plan, 2026-09-06

**Status:** Implemented, flag OFF, rollout gate NOT cleared.

> **Correction, 2026-09-10.** This line said "defaulted off" and the config
> sample below shows `enabled: false`, but `brain/config.yaml` shipped
> `enabled: true` in the same commit (`8dcf3f6`), carrying the `# opt-in until
> measured on a second book` comment from the false version. `git log -L`
> confirms it was never false. Steps 3 and 4 of Rollout were therefore skipped
> and the feature ran in production for four days with Risk 1 (blast radius: 66
> blocks carry 6+ suspicious turns) unmitigated. Set back to `false`.
>
> **Outstanding before this may be enabled:**
>
> 1. Risk 2's mitigation is unbuilt — there is still no independent post-hoc
>    consistency check outside the adjudicator. Guardrail 4 (reciprocal turns)
>    is pre-existing and checks adjacent pairs, not block self-consistency.
> 2. Rollout steps 3-4 (dry-run both paths over one book, diff every
>    disagreement) have not been run.
> 3. The acceptance criteria below are unmeasured.

Addresses the gap left by
[../decisions/2026-09-06-speech-tags-outrank-adjudication.md](../decisions/2026-09-06-speech-tags-outrank-adjudication.md):
speech tags settle 490 of 3,125 spoken lines (16%), and nothing in that
machinery can see the other 84%.

## The problem, precisely

Attribution is decided **one line at a time**: 1,038 independent LLM calls, each
free to be locally plausible and collectively incoherent. A conversation is a
joint assignment, and deciding one line without simultaneously deciding its
neighbours guarantees results like this, from the shipped script:

```
ch11_0147 [effron] "And I have even done you small favors, as you mention."
ch11_0148 [effron] "…You never come to the house I have built. You have never
                    invited me to be a guest in your tower."
ch11_0149 [effron] "You will never be invited into my tower, mother,"
```

0148 places the tower with the listener; 0149 places it with the speaker. They
cannot share a speaker. `ch11_0149` is now correct because a tag settles it
(*"he replied"*); 0147 and 0148 have no tags and remain wrong.

Critically, the adjudicator **may only change the line it was asked about**. When
the real error is upstream, the only way to reconcile local evidence with the
given labels is to corrupt the target. That is error propagation by design.

## Do not replace the per-line path

The per-line path is now clean on everything checkable — **0 of 490** tagged
lines contradict the book. A wholesale switch to block adjudication risks that
for a speed win which, measured, is not there:

| | per-line today | block |
| --- | --- | --- |
| Calls | 1,038 | 176 (**−83%**) |
| Prefill | 1,038 × 2.8 KB ≈ 2.9 MB | 176 × ~4 KB ≈ 0.7 MB |
| **Decode** | 1,038 × ~1 assignment | 176 × ~5.9 assignments |

You still emit one assignment per suspicious line either way, so decode work is
essentially unchanged, and prefill is ~15% of compute on this hardware (see
`benchmarks/`). An 83% cut in calls buys roughly **10–20% wall-clock**:
1 h 36 m → about 1 h 20 m. Block adjudication is a *quality* change. If speed is
the goal, this is the wrong lever.

## Targeting

Apply it only where the per-line approach is structurally blind — a speaker
holding the floor across several turns with nothing in the book confirming any
of them. Measured on the shipped script:

```
same-speaker runs of >=3 turns         : 196
  3 turns: 123   4: 43   5: 16   6: 5   7: 3   8: 3   9: 1   11: 1   12: 1
…with ZERO tag confirmation anywhere   : 124   <- includes the ch11 cascade
```

Those 124 runs are the candidate set. This is **not** a review queue — 124 items
would drown the 8 currently in the inbox, and long same-speaker runs are usually
legitimate (a character delivering a speech split across lines). It is a
*targeting* signal for which blocks deserve a joint decision.

## Design

### Grouping

A block is a maximal run of spoken lines where consecutive spoken lines are
separated by ≤2 narrator lines. `SuspiciousTurn` already carries `chapter_number`
and `surrounding_lines`, so grouping happens in `adjudicate()` before the loop —
no change to the detector.

Measured block shape:

```
block size (dialogue turns) : mean 17.2   median 11   max 94
suspicious per block        : mean  5.9   max 38
block text (chars)          : mean 1,727  p90 4,054  max 11,201
```

**Cap blocks at 8 suspicious turns per call**, splitting at the widest narration
gap. Uncapped, the worst block decides 38 lines in one shot, and the largest
block is 94 turns / 11.2 KB — beyond what should be trusted to a single
response.

### Integration points

- `_adjudicate_block_tier1(turns, chapter) -> list[AdjudicationResult]`, beside
  the existing `_adjudicate_turn_tier1`. Returning the same result type keeps the
  apply path, all four guardrails and the reciprocal-turn pass untouched.
- Response schema becomes `{line_id → speaker_id, confidence, reason,
  evidence_quote}` for every suspicious line in the block. `max_output_tokens`
  must rise from 350 to roughly 8× that; size it from the cap, not the mean.
- **Tags are hard constraints in the prompt, not evidence to weigh.** Every
  confirmed tag inside the block is stated up front as fixed. An assignment that
  violates one invalidates the whole block response.
- **Per-line fallback** whenever a response is malformed, misses a line, or
  invents a `line_id`. Today's behaviour is the floor; the feature can only add.
- `progress_callback` keeps counting **turns**, not blocks, so the dashboard
  reads identically and the reporting added on 2026-09-06 is preserved.

### Configuration

```yaml
external_validation:
  tiered_attribution:
    block_adjudication:
      enabled: false            # opt-in until measured on a second book
      max_suspicious_per_call: 8
      only_unconfirmed_runs: true   # the 124, not all 176 blocks
```

## Risks

**1. Blast radius.** 66 blocks carry 6+ suspicious turns. One bad response
mislabels an entire exchange, and a listener notices a conversation in swapped
voices far more than one stray line. The cap bounds this; the fallback contains it.

**2. Consistency is not correctness — and it erases the signal that found this
bug.** The ch11 error was *detectable* because the block was self-contradictory
about who owns the tower. A block-level model is explicitly instructed to
produce a self-consistent assignment, so it would resolve that tension by
picking one story and the contradiction would vanish. This trades loud,
detectable errors for smooth, plausible, invisible ones. **Mitigation:** keep an
independent post-hoc consistency check outside the adjudicator, and never let
the block prompt see it.

**3. The tag veto stops being clean.** Per-line it refuses one line. In a block,
refusing one assignment breaks the consistency the call was made for, and
refusing the whole block discards good assignments. Handing tags in as hard
constraints and rejecting-and-retrying the block is the principled route, and it
gives some of the saved calls back.

**4. Coarser progress and resume.** An interrupted run loses a whole block
rather than one turn.

**5. Noisier re-runs.** Whole exchanges can flip between runs, making run-to-run
diffs harder to compare than today's per-line churn.

## Rollout

1. Implement behind the flag, defaulted **off**, with per-line fallback.
2. Unit tests: grouping and capping, hard-constraint violation → retry → fallback,
   malformed/partial/hallucinated-`line_id` responses, and a fixture reproducing
   `ch11_0145..0152`.
3. Dry-run both paths over the same book and diff the assignments. Inspect every
   line where they disagree — that set is small enough to read.
4. Enable only if the acceptance criteria hold.

## Acceptance criteria

- Tagged-line contradictions stay at **0 of 490**. Any regression here fails the
  change outright; it is the only figure checkable against the source rather
  than against another model.
- `ch11_0147/0148` resolve to `dahlia`.
- The unconfirmed-run count falls materially below 124 without the review queue
  growing past roughly 20.
- No block response ever silently reassigns a line whose tag confirms its
  current speaker.
- Wall-clock within ~20% of the per-line path. Speed is not the goal, but a
  large regression is a reason to stop.
