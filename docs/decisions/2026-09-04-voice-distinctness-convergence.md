# Cast distinctness convergence — 2026-09-04

**Status:** Current

## The problem

Voice bootstrap measured cast distinctness at a point where nothing could be
done about it.

`VoiceDesigner.bootstrap_voices` ran in this order:

1. Boot the VoiceDesign microservice, design every character's reference clip.
2. Shut the microservice down, freeing VRAM.
3. Transcript-check the references with Whisper, then unload Whisper.
4. Load the Base speaker encoder, embed every reference, compare all pairs.
5. Warn about every pair above `voice_profile_similarity_warning`.

Step 5 could only warn. The code said so explicitly:

> The VoiceDesign helper has already been stopped so the Base speaker encoder
> can run without VRAM co-residency. Do not attempt an impossible late fallback
> generation here; the warning is surfaced for an explicit user redesign
> instead.

### What that cost, measured

On the live project `the-finest-edge-of-twilight-book` (52 speaking characters,
1,326 pairs), the dashboard reported:

> 22 acoustically similar pairs require preview and acknowledgement before
> approval.

Worst observed values, from the Voice casting tab:

| Pair | Speaker similarity |
| --- | --- |
| `avelyere` / `narrator_female` | 0.992 |
| `avelyere` / `donnola` | 0.988 |
| `avelyere` / `sylfae` | 0.988 |
| `bedorijay` / `zaknafein` | 0.988 |
| `braelin` / `effron` | 0.987 |

A named character measured at 0.992 against the narrator is not a warning
about the audiobook; it is a defect in it. The 22 pairs were handed to the
operator as manual work, at the exact moment the machine had all the
information needed to fix them and had just thrown away the ability to do so.

A contrast mechanism already existed — many profiles carried *"the initial
profile was too similar to X, Y, Z; its dedicated contrast direction is
required"* — but it fired during initial design, against whatever had been
generated so far, and **nothing verified that it had worked**. The measurement
that would have told us came later, after the model was gone.

## The decision

Reorder the VRAM phases so the two models take turns, and close the loop.

VoiceDesign is a **subprocess** on port 8101, not an in-process model. Its VRAM
is fully released when the process exits, so re-entering that phase costs a
process boot, not a co-residency problem. That was true all along; the previous
structure simply never used it more than once.

The bootstrap now runs:

1. Design all → compare (as before).
2. While voices collide **and** rounds remain:
   a. Re-boot VoiceDesign.
   b. Redesign **only** the colliding voices, each with a contrast brief that
      names the specific voices it collided with.
   c. Shut VoiceDesign down.
   d. Re-embed only what changed; re-compare the whole cast.
3. Attach similarity warnings from the **final** measurement only.
4. Transcript-check whatever was replaced, in one Whisper load.

### Bounds and guarantees

- **Bounded.** `validation.voice_distinctness_rounds` (default 2, clamped to
  0–5) caps the extra rounds. Anything still colliding when they run out is
  surfaced exactly as before. There is no unbounded retry.
- **Costs nothing when clean.** A cast with no collisions boots VoiceDesign
  zero extra times. This is the common case for a small cast.
- **`0` restores the old behaviour** exactly: measure, warn, stop.
- **Never loses a working voice.** A redesign that raises is logged and
  skipped; the previous reference stays.
- **Only the canonical candidate is replaced.** Alternatives exist for a human
  to audition and may already have been chosen; a round does not discard them.
- **Cancellation is honoured** between every voice and every round.
- **Redesigned references are transcript-checked.** A contrast brief moves
  pitch and speaking rate, which is exactly the kind of change that can hurt
  intelligibility, and the initial WER pass ran before any redesign existed.

### Why warnings are applied at the end

Previously each comparison appended warnings as it went. With more than one
comparison that would leave a voice repaired in round 1 still carrying its
round-0 collision text. Warnings are now derived once, from the final
diagnostics.

## Reporting

`BootstrapVoicesResponse.distinctness_rounds` carries one entry per round:
which voices were redesigned, and the collision count and worst similarity
before and after. Empty when the first measurement was already clean.

This is the evidence needed to answer "is the loop actually converging, or just
burning model swaps" without re-running a bootstrap.

## What was rejected

- **Keeping both models resident** so comparison could regenerate inline. This
  is what the original comment ruled out, and it was right to: on a 24 GB card
  the VoiceDesign model plus the Base speaker encoder is the co-residency the
  whole subprocess design exists to avoid.
- **Unbounded retry until every pair separates.** Some casts genuinely cannot
  separate — a book with eight middle-aged men of the same described timbre has
  a real ceiling. An unbounded loop would hang bootstrap rather than surface
  that ceiling to the operator.
- **Lowering `voice_profile_similarity_warning` to flag fewer pairs.** That
  hides the problem rather than fixing it, and the threshold is what the
  operator tunes to their own tolerance.

## Verification

`tests/test_voice_distinctness_convergence.py`, 18 tests against a faked engine
and library — no model is loaded:

- converges in one round and does **not** spend the second;
- stops at the configured bound and degrades to warnings;
- a clean cast triggers zero VoiceDesign boots;
- `0` rounds measures and warns but never redesigns;
- only colliding voices are redesigned;
- the brief names the specific collided voices;
- a failed redesign keeps the previous reference;
- cancellation aborts the round before any generation;
- redesigned references are transcript-checked, and the re-check is skipped
  when nothing was redesigned;
- warnings reflect only the final measurement;
- a round that makes the cast worse is discarded and rolled back;
- the previous take is not deleted before a redesign, so rollback has a
  file to return to.

### Measured against real models — 2026-09-04

Run on the Windows/ROCm workstation against the real VoiceDesign and speaker
encoder. Four male voices, same age band, **byte-identical** voice descriptions
and test sentence — deliberately the hardest case, so the contrast brief is the
only thing that can separate them. Scratch project id; no real project touched.

**Run 1 — production threshold (0.985), 4 voices, 120 s**

Worst pair 0.9784, so nothing collided and zero rounds ran. Confirms the
"clean cast costs no extra model boot" path on real models.

**Run 2 — threshold lowered to 0.970 to force collisions, 255 s**

| Round | Redesigned | Similar pairs | Worst similarity |
| --- | --- | --- | --- |
| start | — | 6 | 0.9881 |
| 1 | all four | 6 | **0.9915 (worse)** |
| 2 | all four | 4 | 0.9789 |

The mechanism works: VoiceDesign re-booted per round, only colliding voices
were regenerated, re-measurement ran, the bound held, unresolved collisions
degraded to warnings, and the per-round report was accurate.

**The substance is weaker than hoped, and round 1 exposed a real defect.**

A redesign is a *resample from a stochastic model*, not a monotonic
improvement. Round 1 made the cast worse, and the loop accepted that state as
the baseline for round 2. That is fixed: the loop now scores each measurement
(collision count first, worst similarity as tie-break), keeps the best, and if
a round fails to improve on it, restores the best state and stops rather than
spending another VoiceDesign boot resampling from a known-worse position.
`_redesign_for_distinctness` no longer deletes the previous take first — it was
destroying the only file a rollback could return to, and `_generate_voice`
re-registers the id anyway. Each round now reports `kept: true|false`.

**A second real-model round, 2026-09-04, after the rollback fix**

Same adversarial setup. This run exercised the loop end to end with the
rollback working:

| Round | Similar pairs | Worst similarity | Kept |
| --- | --- | --- | --- |
| start | 6 | 0.9868 | — |
| 1 | 4 | 0.9915 | yes |
| 2 | 5 | 0.9797 | **no — rolled back** |

Round 1 was kept because collision *count* dominates the score; round 2 raised
the count from 4 to 5 and was discarded, restoring round 1's cast. The final
warnings correctly say "redesigned in distinctness round 1", and critically
**no WER warnings appeared**, which is what confirmed the file bug below was
real and is now fixed.

### A defect the first real run hid behind a false signal

The earlier run reported `WER 1.00` on all four redesigned references, which
read as "the contrast brief destroys intelligibility". It was not. The re-check
took 20 ms per voice against roughly 1 s in the initial pass, and the log said
`Failed to load audio` — the files were **gone**.

`VoiceLibraryManager.register_voice` reclaims the previous artifact when a new
file is registered under the same id. Removing the explicit `delete_voice` call
was therefore not enough: the rollback restored a *path* to a file that
registration had already deleted, leaving the whole cast pointing at missing
audio. Whisper could not open them, returned nothing, and WER came back 1.00.

Fixed by copying the audio aside in `_snapshot_voices` rather than only
recording its path, putting it back and re-registering it on rollback, and
discarding the copies once they can no longer be needed. A rolled-back round is
also no longer transcript-rechecked, because its audio is the previous take
that the initial pass already checked. Three tests cover it.

**On the brief wording itself, the evidence is inconclusive.** The brief was
rewritten so that each colliding voice receives a *different* direction, ranked
by measured pitch — the lowest told to go lower, the highest higher, the rest
separated on timbre and rate — because the original gave every colliding voice
the identical "be different from them" instruction, and identical instructions
applied to identical descriptions resample into the same region. That reasoning
is sound but unproven: both briefs reduced 6 colliding pairs to 4 on this
setup, the old one reaching a better worst-similarity and the new one a worse
one, with n=1 each and run-to-run starting variance (0.9817 to 0.9881) of the
same order as the effect. Treat the new brief as a better-motivated guess, not
a measured improvement.

**Known limitation of the score.** Collision count dominates worst similarity,
so a round that removes two collisions while pushing one remaining pair from
0.9868 to 0.9915 counts as an improvement — which is what round 1 did. Whether
that ordering is right for the audiobook is a judgement call that has not been
tested against listening.

**What is still not established:** whether a contrast brief of any wording can
reliably move a genuine 0.99 pair below 0.985 on a *realistic* cast. Everything
measured so far used four byte-identical descriptions, which is the worst case
and not representative. Read `distinctness_rounds` on the next real bootstrap.
