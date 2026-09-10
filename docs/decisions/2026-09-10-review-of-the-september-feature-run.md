# Review of the 2026-09-05..09 feature run — 2026-09-10

**Status:** Current

A read-through of the five days between `a4f2f6d` and `d688e25` (~7,800 lines,
73 files), checking each decision against its own record and each
implementation against its own decision. Eight corrections. The reasoning behind
the features was sound; every defect found sits in the gap between what a record
claimed and what the code did.

## 1. A feature that had never once run

`Pipeline._pregenerate_pronunciation_previews` calls `hashlib.sha256(...)` in a
module that never imports `hashlib`. It is the first statement after
`text_to_speak` in the candidate loop, so it raised `NameError` on candidate #1
of every run from 2026-09-07 onward. The whole body sits in a `try` whose
handler logs

```
Pronunciation preview pre-generation encountered an issue: name 'hashlib' is not defined
```

so a stage that had never produced a single preview reported itself as a
transient hiccup for two days. `pyproject.toml` already declares the Pyflakes
rules non-negotiable at zero — the declaration was simply never checked.

**Fixed:** the import, plus `UndefinedNamesTests.test_no_undefined_names_anywhere`,
which walks the tree and asserts F821 is empty. Verified to fail when the import
is removed again.

This is the exact failure the `BLE001` ratchet exists to prevent: *"`except
Exception` catches the failure you expected and the typo you did not."* The
ratchet had also gone backwards, 118 → 139, because three feature commits added
blind excepts faster than the narrowing pass removed them. Now **116**.

## 2. A veto measured, rejected, then shipped anyway

The 2026-09-06 update to
[whole-cast duplicate detection](2026-09-04-whole-cast-duplicate-detection.md)
added a **co-occurrence veto**: refuse a merge if the two names appear in ≥ 3
sentences within 300 characters. The record justifying it asserts that
appositive aliases "appear together at most 0–1 time across a book".

Twenty lines above, in the same record, is the measurement that says otherwise:

| Pair | Relationship | Co-occurrences within 200 chars |
| --- | --- | --- |
| Ilnezhara / Tazmikella | distinct twins | 6 |
| Jarlaxle / Uncle Jax | **same person** | 10 |
| Regis / Rumblebelly | **same person** | 15 |

> Aliases co-occur *more* than distinct characters... Proximity is worse than
> useless here.

The rule fires hardest on aliases, which is the one thing it must never do.

### Why no test caught it

Both "allows" fixtures gave each side every term the other had, so
`distinct_participant_veto` hit its shared-term abstain and returned before
reaching the rule. Nothing in the suite ever executed it.

### Where it was live

That abstain needs the pair to share a token. The whole-cast roster stage exists
precisely for pairs that *do not* — "a character recorded once by proper name and
again by appellative shares no token". Measured over both books in the library,
using the shipped regex:

```
                                                    book 1   book 2
characters                                              60       68
candidate pairs                                      1,770    2,278
vetoed by the retained rules                           765      821
refused ONLY by the removed co-occurrence rule          35       94
```

### What those refusals actually were

An earlier draft of this record named `patji` / `insect_god` as a genuine
name-and-appellative duplicate the rule was blocking. **That was wrong** — the
two are distinct entities (the insect god is a group mind that converses with
Dusk; Patji is a separate god), confirmed both by the text and by the operator.
Inspecting what the shipped regex actually matched:

```
Patji—the god
Patji, Dusk thought, was a god
Patji, he did respect the god
god," he said, "is Patji
insect that wasn't found anywhere but Patji
```

All five are the bare aliases `God` and `Insect` on `insect_god` matching prose
about **Patji himself**, or a literal bug. No sentence in the book contains both
a Patji term and an Insect-God term. The rule was not discriminating; it was
counting noise.

That is a *worse* property than the one originally alleged, not a better one: a
veto whose evidence is junk will fire unpredictably in either direction. But it
also means the honest conclusion is narrower than first written:

> **No genuine merge was blocked on either book.** The refusals were co-starring
> distinct characters (harmless, the roster would never propose them) and
> generic-alias noise. The case against the rule rests on the measurement in the
> 2026-09-04 record — aliases co-occur *more* than distinct characters — not on
> a demonstrated victim in this library.

### Not realised at all

`cast_identity_audit.json` for book 1 records `"applied": []` with an empty
trace: the roster stage proposed no merges on the last run. Nothing was blocked
and nothing was wrongly merged. Removing the rule restores the deterministic
behaviour that shipped before 2026-09-06 — `conjunction_count` also returns 0
for these pairs — so this is a reversion to the measured design, not a new hole.

### Fixed

Rule 3 removed; rules 1 (syntactic conjunction) and 2 (interaction beats)
retained. Re-checked against real book text:

```
MUST STILL BE REFUSED
  Ilnezhara / Tazmikella   conjunction veto (2)
  Bruenor / Drizzt         conjunction veto (2)
  Entreri / Jarlaxle       conjunction veto (5)
  Catti-brie / Wulfgar     explicit genders disagree
  Catti-brie / Breezy      interaction veto     <- the case the update was written for
```

All three Catti-brie / Breezy forms the record cites (*"said to"*, *"looked
to"*, *"…, but …"*) are interaction beats, so rule 2 carries that case alone.

## 3. Block adjudication shipped enabled, against its own gate

[The plan](../plans/targeted-block-adjudication-2026-09-06.md) states *"defaulted
off"*, shows `enabled: false`, and gates enabling on a dry-run diff of both paths
with tagged-line contradictions held at 0 of 490. `brain/config.yaml` shipped
`enabled: true` in the same commit, keeping the comment `# opt-in until measured
on a second book` from the `false` version. `git log -L` confirms it was never
`false`.

Its own Risk 1 is blast radius: 66 blocks carry 6+ suspicious turns, and one bad
response mislabels an entire exchange.

**Fixed:** set to `false`, with the gate named in the comment. The plan's Risk 2
mitigation — an independent post-hoc consistency check outside the adjudicator —
is still unbuilt, and is now listed as a gate item rather than assumed present.

### The dry-run the plan asked for, run after the fact

616 lines of `the-finest-edge-of-twilight-book` were decided by the block path
while the flag was wrongly on. Rollout step 3 —

> "Dry-run both paths over the same book and diff the assignments. Inspect every
> line where they disagree — that set is small enough to read."

— was never run, so it was run now. The per-line path cannot simply be re-run
over the current scripts, because block adjudication left those lines at high
confidence and the detector no longer flags them; each was rebuilt into the
`SuspiciousTurn` the detector would have produced and handed to
`_adjudicate_turn_tier1` alone.

```
re-examined  616 of 616
  agree      596        (96.8%)
  DISAGREE     4
  escalated   16        (the per-line path would have sent these to Gemini)
  failed       0
```

Reading all four, three favour the per-line answer and one favours the block
answer — and every one sits in a stretch where the *neighbouring* labels are
already wrong, so both paths are reasoning from corrupted premises:

| line | block said | per-line said | who is right |
| --- | --- | --- | --- |
| `ch13_0362` | breezy | savahn | **per-line** — the next narrator line is *"Savahn flatly stated."* |
| `ch09_0307` | gregory_antoine | jarlaxle | **per-line** — the beat before is *"Jarlaxle admitted… but he grew more serious"*, and Gregory stutters *"I—"* after |
| `ch17_0168` | jarlaxle | zaknafein | **per-line** — *"You would claim that in any case"* rebuts Jarlaxle, who then answers *"Not with you, old friend"* |
| `ch01_0250` | effron | dahlia | **block** — *"Can you not even call me mother?"* is Dahlia's, so *"No."* is Effron's |

Reading the 16 "escalated" lines settles it. On every one, the per-line path
gave the **same speaker** as the block path and simply landed below the 0.85
auto-accept threshold. So:

```
same answer          612 / 616  = 99.4%
disagreed              4   -- per-line right 3, block right 1
```

**Measurable effect on quality: minus two correct lines out of 616.** Against
10–20% wall-clock and 16 saved escalations, weighed against blast radius,
coarser resume, noisier re-runs, and Risk 2.

The decisive argument is not the diff but §Risk 2's own mitigation: the ch11
cascade block adjudication was built for, and never selected, is caught by
`detect_possessive_contradictions` in forty deterministic lines with no LLM
call. The cheaper tool solves the motivating case better.

**Removed the same day** (see §*The measurement found a fourth
block-adjudication error* below for the finding that settled it): 544 lines out
of the adjudicator, plus the config block, the three CLI flags, two summary
counters, the `local_qwen_block` resolver and the diff script that measured it.
`_LOCAL_RESOLVER_TIERS` still reads that resolver from scripts written before
today; nothing writes it. Eleven block-only tests went with it; the thirteen in
`test_block_adjudication.py` that were never about blocks — the possessive
check, the refutation guard, the unique-candidate resolver — moved to
`tests/test_deterministic_refutation.py`.

### What the diff actually found: a blind spot in the tag guardrail

`ch13_0362` is labelled `breezy` and the very next narrator line is **"Savahn
flatly stated."** The 2026-09-06 record's whole point is that *the attached tag
outranks the model* — so why did nothing catch it?

Because every tag check in the codebase requires the following narrator line to
begin with a **lower-case** letter, the mark of a line continuing the quoted
sentence (*"he said"*). A tag written as its own sentence starts with a capital
and is skipped by all of them. Counted on this book:

```
lower-case-led tags (the guardrail reads these) : 490
capital-led "<Name> <verb>" tags (it does not)  : 574
```

The 490 in the 2026-09-06 result table is not the number of speech tags in the
book. It is the number the guardrail can **see** — slightly under half.

The parser is not the limitation, the gate is: `_dialogue_tag_evidence("Gregory
replied with a blank stare.")` returns `gregory_antoine` quite happily. One
stored speaker in that ignored set contradicts its tag (`ch09_0108`, stored
`perrywinkle_shin`, tag names Gregory). `"Savahn flatly stated."` defeats the
parser too, for a second reason — the adverb between name and verb.

Not fixed here. Widening the gate touches the most load-bearing guardrail in the
attribution path, and the 2026-09-06 record earned its result by measuring
before changing. It needs its own pass, with the same discipline.

## 4. A rejection erased by a later acceptance

The end-of-stages fallback changed from `len(errors) == len(stages)` ("every
external stage errored, so triage was unavailable") to
`external_validation_decision != "reject"`. That field holds only the **last**
stage that answered. Stages run triage → adjudication → web, so a low-confidence
`reject` from triage followed by a low-confidence `accept` from web left the
field at `accept`, cleared `manual_review_required`, and dropped the rejection.

Only critical-risk segments reach that loop at all, so it was the worst possible
place to lose one.

**Fixed:** scan `external_validation_history`, which already holds every
decision. Test verified to fail against the old condition.

## 5. A one-minute rate limit read as a spent day

Google returns `429 RESOURCE_EXHAUSTED` with the word "quota" for **both** a
per-minute rate limit and a per-day quota. The handler treated any such 429 as
exhaustion, raised, and `_ProviderHealth` matched `"quota exhausted"` in the
message and opened a **one-hour** circuit on that provider.

At `request_interval_seconds: 2.0` (30 RPM) the free tier's per-minute ceiling is
reachable. And because candidates are now risk-sorted, one transient rate limit
blacked out the provider for an hour and every remaining *critical* segment fell
through to local acceptance with no external check at all.

The discriminator was in the response the whole time: `quotaId` carries
`PerMinute` or `PerDay`, and `RetryInfo.retryDelay` gives the wait.

**Fixed:**

- `_is_daily_quota_exhaustion()` reads the `quotaId`; anything not positively
  identified as per-day is retried, honouring `Retry-After` / `retryDelay`.
- Exhaustion is signalled by a new `QuotaExhaustedError` **type**, not by
  grepping a message that embeds the server's own `RESOURCE_EXHAUSTED` text —
  which would have re-opened the hour-long circuit regardless of the fix.
- `budget.reserve()` is charged per HTTP attempt again. Moving it outside the
  retry loop let the local 450/day safety budget undercount real requests by up
  to 4×.

### The cooldown now lasts until the quota returns

The fast exit was already there — `is_exhausted()` short-circuits before any
HTTP request — but the *cooldown* was a flat hour, which is wrong in both
directions. Spend the budget at 10:00 PT and an hourly circuit wakes up to fail
thirteen more times before the quota is back; spend it at 23:30 PT and the
circuit stays shut for half an hour after it returned.

A daily quota does not come back on a timer of our choosing, it comes back at
Pacific midnight — the same boundary `_UsageBudget` already keys its day on, and
the one Google's free tier resets on. `QuotaExhaustedError` now carries
`retry_at_epoch`, defaulting to `next_daily_quota_reset_epoch()`, and the
circuit is held to exactly that. The entry records `open_reason:
daily_quota_exhausted` so `before()` can say *"daily quota is spent; it resets
at 2026-09-10 00:00 PDT (9h12m from now)"* rather than reporting a fault, and a
single success clears it.

## 6. The 275-failure bug class, not just its trigger

The [2026-09-07 record](2026-09-07-pronunciation-lexicon-and-stt-validation.md)
correctly diagnosed the "Silent Hard Gate Failure Cascade": Whisper rejected the
BCP-47 tag `en-US`, `transcribe` caught it and returned `""`, WER scored the
empty string as 1.0, and 275 of 277 pristine segments were blocked with
*"Deterministic audio hard gate failed; external models cannot override it"*.

Normalising the language code fixed the **trigger**. The **mechanism** was
untouched: `transcribe` still returned `""` on any exception, and nothing
downstream could tell that apart from silence. Any future fault — OOM, a corrupt
file, a model-unload race — reproduces the same flood.

**Fixed:** `TranscriptionUnavailableError` and `transcribe_strict()`.
`transcribe()` keeps its lenient contract for benchmarks, voice design and
ad-hoc scripts; `ValidationLoop` uses the strict form and, on an outage, keeps
the acoustic gates (which never needed STT), suppresses the text verdict,
records `acceptance_reason="stt_unavailable"`, and zeroes the WER — a stored 1.0
for audio nobody listened to is a false reading that every downstream average
and threshold would treat as real.

## 7. Triage thresholds that only half applied

`external_validation.audio_triage` documents three knobs. `_risk_priority` in
`pipeline.py` hardcoded copies of all three, so tuning them changed which
segments were admitted but not the order quota was spent in. Both copies also
compared the status against `"failed"`, while `ValidationStatus.FAIL` is
`"fail"` — a clause that could never be true.

**Fixed:** one predicate, `GeminiValidationService.is_critical_risk_segment()`,
used by both, reading the configured values and comparing against the enum.

## 8. Smaller

- `_split_group` recursed forever if `max_suspicious_per_call <= 0`; clamped.
- `_is_block_targeted` rescanned the whole chapter once per block; cached per pass.
- Non-spoken pause detection was `not any(c.isalnum())`, which also silences a
  spoken `"?"` or `"..."`. Narrowed to actual separator glyphs and shared by
  synthesis, validation and the review gate so the three cannot disagree. On both
  books this changes nothing today — all 62 hits are genuine em-dash separators —
  so it removes a latent risk rather than a live one.
- ~~Scene-break silence was 100 ms, below the threshold where a listener hears a
  break at all. Now `PAUSE_MARKER_SILENCE_SECONDS = 0.9`, which does change those
  62 lines in book 1.~~

  **Wrong, reverted the same day.** The placeholder is not the pause.
  `voice.mastering.assembler` owns the timing and inserts
  `max(previous.pause_after_ms, current.pause_before_ms)` between segments,
  under an explicit rule: *"One timing owner: adjacent pause directives are
  combined with max(), never added together."* Checked against the data:
  every separator line in book 1 carries `pause_after_ms = 900`, and so does
  the line before it, so the break already runs ~0.9 s + placeholder + ~0.9 s
  ≈ **1.96 s**. Measured on the seven markers inside the delivered
  chapters 1–5, the existing segments are 0.16–0.48 s at −52 to −180 dBFS —
  silent, and short by design.

  Raising the placeholder to 0.9 s would have pushed those breaks to ~2.7 s and
  introduced exactly the third, uncombined silence source the assembler's rule
  exists to prevent. Back to 0.1 s.

  This also settles the remediation question it raised: the delivered
  chapters 1–5 need **no re-master**. The audio was never wrong.
- `deploy_voice_apk.py` trusted any SSH host key (`AutoAddPolicy`) while sending
  credentials; it now loads `known_hosts`, rejects unknown keys, and prints the
  one-time `ssh-keyscan` enrolment command. URL probes check the scheme first.

## Verification

```
ruff check .        All checks passed        (was 19 errors, 3 of them F821)
pytest              764 passed, 2 skipped    (was 713; +51 tests)
BLE001 ratchet      116                      (was 139; ratchet target was 118)
live cast pass      both books, both tiers, 3 API requests total
```

Each regression test for a shipped defect was verified to fail when the defect is
reinstated, following the convention set by `25e7d8a`.

## Fixing without reading: the layer that replaced the review queue

Most of this record describes turning defects into review items. For this
project that is close to useless, and the 2026-09-04 record already says why,
about cast merges:

> Approving a merge means reading the verbatim excerpts that justify it, which
> is a plot summary of a book the operator has not read yet.

The same is true of an attribution. Nine new blocking items were created here
before that registered — nine obligations to read passages from unread books.
So the rule changed: **anything resolvable without a human reading should be.**

### 1. A refutation plus a scene is often an answer

A deterministic check says who did *not* speak. Three now do:

| refutation | evidence |
| --- | --- |
| `possessive_contradiction` | one speaker owns and disowns the same thing in one turn |
| `gendering_tag` | the attached tag genders the speaker differently |
| `addressed_not_speaking` | the tag says the stored speaker was spoken **to** |

The third comes straight from the 2026-09-06 principle — *"a speech tag names
several people, only its subject speaks"* — read for what it also yields: that
name is the **addressee**, and nobody is spoken to by themselves. Only
`<verb> to <name>` and `<pronoun> <verb> <name>` are read, and a possessive is
excluded: *"his arm around Dusk's shoulders"* is about Dusk without addressing
him.

If the surrounding scene leaves exactly **one** other speaker the refutation
allows, that is the answer, and nobody reads anything.

```
                              refuted  unique  ambiguous  none
the-finest-edge-of-twilight         1       1          0     0
isles-of-the-emberdark              9       3          5     1
```

The one `none` is the known false positive — *"your way home"* against *"our
way back"* — where the rule correctly declines. It acts only where the text
leaves one answer.

### 2. Where the text leaves two, ask a closed question

Block adjudication is the wrong tool for the rest: it decides a whole block
jointly, bringing its blast radius to a six-line problem, and it agrees with
the per-line path 99.4% of the time anyway.

The better move is narrower. *"Who speaks this line?"* is the open question the
per-line adjudicator already answered wrongly and Gemini answers differently on
different runs. By this point the deterministic layer has established what it
did not have then — who did not speak, and the complete candidate set — so the
model can be asked to **choose from a list** instead of attributing freely.

That is a different task, and empirically a far more stable one:

```
                                     open question          closed question
ch11_0148   dahlia 1.00 then effron 0.74     —          (resolved deterministically)
ch07_0188                                    —          vathi      3/3  1.00
ch36_0270                                    —          starling   3/3  0.95
ch47_0205                                    —          chrysalis  3/3  0.98
ch47_0209                                    —          chrysalis  3/3  0.98
ch60_0113                                    —          dajer      3/3  0.98
ch38_0118                                    —          dajer      3/3  0.98
```

Every one agrees with a hand reading. Two (`vathi`, `chrysalis`) had been worked
out by hand hours earlier, independently, and `ch38_0118` is self-evident: the
line is *"My name is Colonel Dajer,"*.

Three guards, because the model is the weakest link: the answer must be in the
candidate list, all runs must agree, and mean confidence must clear 0.85. It is
opt-in behind `--llm`, and provenance is recorded as `constrained_choice`
rather than `deterministic_unique_candidate`, because a later reader must be
able to tell which lines the text settled and which a model chose.

Blocking review items: **1 → 0** and **8 → 2**, with no spoilers spent.

### Would this generalise to every low-confidence line?

Tempting, and not yet supported. The tier changed four things at once — a
closed candidate set, the refutation as a stated fact, a window seven times
wider, and unanimity across runs — so nothing here isolates which of them did
the work. Assuming it was the context would repeat the mistake this record
documents twice already.

What *is* measurable without a model is the cost. Widening the per-line window
from (4, 6) to (20, 30) on the sixteen lines the per-line path left at
0.80–0.83:

```
narrow (w4/s6)     25,978 chars   ~6,500 tokens
wide   (w20/s30)  114,465 chars  ~28,600 tokens
                                   4.4x prefill
```

Prefill is roughly 15% of compute on this hardware (see `benchmarks/`), so
widening *everything* costs something like 1.5x wall-clock across ~1,038 calls.
Widening only the low-confidence tail — sixteen of 616 lines, 2.6% — costs
almost nothing. That asymmetry is the argument for cascading rather than
widening globally: spend the context where the cheap attempt already said it
was unsure.

Whether it *helps* was then measured on those sixteen lines: same prompt, same
code path, only the window changes, three runs each.

```
        agrees_with_stored  stable_3of3  mean_conf  above_0.85  wall
narrow        16/16            16/16       0.917      13/16     5.3s/line
wide          15/16            16/16       0.954      16/16     6.1s/line
```

Three things fall out, and they separate the variables the tier had confounded.

**Context buys confidence, not stability.** Every line was already unanimous
across runs in *both* conditions. So the stability the constrained-choice tier
gained did not come from the wider window — it came from closing the question.
Those are different levers with different effects, and conflating them was the
error in the paragraph above.

**The cost is small, and not what the prefill arithmetic suggested.** 4.4x the
prompt is +15% wall-clock, because prefill runs at ~760 tok/s against ~64 tok/s
decode. Widening everything is therefore affordable; it is just poorly targeted,
since 97% of lines are already confident.

**And it found a real error.** On `ch11_0222` the narrow window said `effron` at
0.80 and the wide window said `dahlia` at 0.98. The wide answer is right, and
the text says so outright — the next narrator line is *"That had Effron's hair
on the back of his neck standing up. Something about the timbre of Dahlia…"*, so
the line unsettled Effron and was not his. Effron owns the tower
(`ch11_0149`, tag-confirmed), so *"visit you at your tower"* addresses him.

That line had been resolved by `local_qwen_block` at 0.95 — one more line block
adjudication got wrong that the per-line path, given room, gets right.

### The decision: cascade, do not widen globally — built

Re-run with a wide window **only where the cheap attempt lands below the
auto-accept bar**, before escalating to Gemini. On this sample that is 16 of 616
lines (2.6%); widening all 1,038 calls buys the same benefit for 40x the extra
compute, on lines that are already confident.

This is now wired, as `TieredAttributionAdjudicator._retry_with_wide_context`,
behind `external_validation.tiered_attribution.wide_context_retry` (on by
default). The design constraint it is built to is that the cascade must be
**strictly additive**: it can turn an escalation into a local resolution and
nothing else.

* Only a result already bound for `gemini_api` reaches it, so the choice is
  never "one local call or none" — it is "one local call, or a paid remote one".
* The retry's answer runs through the *same* guardrails. A wider window is not
  a reason to relax alias resolution or the gender checks.
* If the retry also fails — low confidence, an unresolvable name, an exception —
  the **narrow** escalation is returned untouched. It cannot make a line worse.
* One retry, never two (`allow_wide_retry=False` on the inner call).
* A single run, deliberately. Both widths were 3-of-3 stable in the A/B, so
  repeats measure nothing here; unanimity is worth paying for where the model is
  known to waver, which is the constrained-choice tier, not this.

A line the retry settles is stored as `local_qwen_wide`, and the reason keeps
why the narrow window gave up — otherwise the record shows a confident answer
with no trace of the doubt that produced it. `wide_context_resolved` in the run
summary counts them; watched against `escalated_to_tier2`, it says whether the
cascade is still paying.

`build_turn_window` moved out of `detect_suspicious_turns` to module level in
`attribution_detector.py` so the retry rebuilds a turn shaped exactly like a
detected one. Two builders would drift, and the difference would surface as an
attribution change nobody could account for.

#### What it costs a whole pass

The A/B compared one narrow call against one wide call on the same line. That is
not the number an operator cares about, and taken alone it misleads: it makes
the cost look like the extra context, when the cost is actually the second call.
So the wired path was measured again over an **unbiased 150-line sample** of the
same book's suspicious turns, at production radii:

```
lines                       150
retried (narrow escalated)    4  (2.7%)
  settled by the retry        4
  still escalating to Gemini  0
mean secs, no retry         5.0
mean secs, retried         12.9
pass wall                   +3.4%
```

**+3.4% on the pass, and every escalation in the sample disappeared.** The retry
rate matches the 2.6% predicted from the 616-line diff, which is the reassuring
part: the cascade fires about as often as the analysis said it would.

A separate run over the 16 known sub-threshold lines — the hardest sample there
is — put 5 of 6 retries away locally, leaving `ch21_0004` for Gemini. The
cascade does not claim to settle everything, only to try cheaply first.

One caution on the per-call figures: the A/B's +15% assumed ~64 tok/s decode,
and this run logged closer to 19 tok/s. Decode speed and answer length move
these numbers around far more than window size does. The +3.4% pass figure is
the durable one.

#### The measurement found a fourth block-adjudication error

`ch17_0168` came back as `zaknafein` at 0.95 against a stored `jarlaxle`. Reading
the passage, the model is right:

> ch17_0167 **jarlaxle** (tag-confirmed) — *"…but in this case, it is simply incorrect."*
> ch17_0168 **jarlaxle** ← stored — *"You would claim that in any case."*
> ch17_0169 **jarlaxle** — *"Not with you, old friend. Were I here to cause trouble…"*

Nobody answers their own claim with *"You would claim that in any case."* The
line is Zaknafein's scepticism and `ch17_0169` is Jarlaxle's reply — as the
*"old friend"* address confirms. Both stored lines carry
`attribution_resolver: local_qwen_block`.

This is **not** a cascade finding — the narrow window caught it on a single
call, and it surfaced only because re-running an already-adjudicated book
re-asks settled lines. It is one more entry in the case against block
adjudication, alongside `ch11_0222` and `ch11_0147`/`ch11_0148`. The fix is not
to hand-edit the line; it is the removal already recommended below, after which
a re-run resolves it correctly on the per-line path.

What this does **not** support is using the constrained-choice tier generally.
Its candidate list only exists because a deterministic check refuted a speaker
first. With no refutation there is no list, and the question is open again --
which is the question both models are unreliable at.

### Rejected: widening the possessive check to scene scope

`ch11_0222` is a possessive contradiction the check does not see, because
`ch11_0149` ("my tower") and `ch11_0222` ("your tower") sit in different
unbroken runs. Widening the scope to ±60 spoken lines catches it — and takes
the finding count from 2 to 19 across the two books:

```
run-scoped     book 1: 1   book 2: 1
scene-scoped   book 1: 6   book 2: 13
```

Most of the additions are legitimate. Within one unbroken turn, "your tower"
and "my tower" cannot both be true. Across a scene the addressee changes, so
one speaker saying "my people" and "your people" is ordinary English, and
`people`, `own`, `side`, `time`, `kind` and `camp` are exactly what the wider
scope turns up. Precision falls from about a half to about a seventh.

That is the co-occurrence veto again — a rule widened past the evidence that
justified it — so the scope stays where it is, and cases like `ch11_0222` are
left to the wide-context cascade, which found this one.

## The one defect that cannot be auto-fixed

`ch11_0148` is the only finding the possessive check reports on
`the-finest-edge-of-twilight-book`, and it is real: Effron disowns the tower on
that line and owns it on the next, which its own tag confirms as his. So the
line is not Effron's.

Every automatic resolver disagrees:

```
local qwen3.8:27b            effron   0.98
gemini-3.5-flash-lite        effron   0.95
possessive contradiction     "not effron"
```

Escalating it to Gemini made things **worse**: the model restated `effron` and
the escalation path cleared `attribution_review_required`, turning a proven
defect into a confidently accepted wrong answer.

### A refutation now outranks a model

A deterministic finding is a fact about the text, so it is marked
`[deterministic] ` in the review reason and a model may not restate the speaker
it refutes. Any *other* answer settles the line normally — once the speaker
changes, the contradiction is gone.

The marker is captured before any tier runs, because each tier rewrites the
review reason as it reports; reading it inside the loop loses it after the
first stage.

Run live, twice, on the same line:

```
run 1   triage effron 0.95  -> refused -> adjudication dahlia 1.00  -> accepted
run 2   triage effron 0.95  -> refused -> adjudication effron 0.74  -> below
                                                                       threshold,
                                                                       stays flagged
```

`dahlia` is the answer the 2026-09-06 record argues for and the one block
adjudication was built to produce and never did. But **Gemini is not stable
here** — the same line, the same prompt, two different answers minutes apart.

That is the answer to "can it be auto-fixed?": **no.** Not by the local model,
not by Gemini, not reliably by anything. What can be done is refuse to let a
model overwrite the refutation, and leave the line flagged for a human. It is
flagged now.

## What this review did not check, and one gap it exposed

The cast pass has been run live on both books through **both** tiers (Lever 3).
Still unexercised: the **audio-QA** path, and the 429 handling in §5 — no real
rate limit or per-day 429 was observed, only the local safety budget, so that
code remains covered by fakes alone.

The block-adjudication rollout gate (dry-run both paths, diff every
disagreement) remains unrun — the flag is off, not validated.

Two artefacts of the testing itself, recorded so they are not misread later:
running with `api.enabled: false` to reach the web tier makes `_call_stage`
record two failures per call, which pushed both API circuits open for 15
minutes (since cleared, along with the stale failure counts from the 2026-09-09
exhaustion). And the live runs spent 2 `gemini-3.5-flash-lite` and 1
`gemini-3.5-flash` request against the 450/50 daily budgets.

### The deterministic layer does not separate `patji` from `insect_god`

Chasing the wrong example in §2 surfaced a real one. With rule 3 gone,
`merge_veto("insect_god", "patji", ...)` returns `None`: no conjunction, no
interaction beat, and the genders are `male` vs `other` — and `other` means
*unresolved*, so by design it does not block. Two distinct gods, and nothing
deterministic refuses them.

This is not new and not a regression: `conjunction_count` was 0 for this pair
before 2026-09-06 as well. It is the category the 2026-09-04 record already
documented with `zaknafein` / `drizzt` (father and son, never conjoined), and
labelled honestly:

> **The veto is a safety net for what text can settle deterministically, not a
> complete classifier.**

So the answer to "is cast identification robust?" is **no, and it was never
claimed to be**. For a pair like this the only things standing between a bad
roster proposal and a merged voice are the grounding stage and
`min_confidence: 0.95` — and per that record's own Verification section, the
Gemini stages have *never been run against the live API*. `require_approval` is
`false`, so a merge that clears them applies unattended.

**Aggravating factor: alias hygiene.** The roster prompt is names, aliases,
gender and dialogue counts — no book text. It would see:

```
patji       "Patji"       aliases [Father, The God]
insect_god  "Insect God"  aliases [God of Insects, The Thing, Insect, God]
```

A shared divine appellative across two entries is exactly the cue that invites a
duplicate proposal. Book 2 carries 12 such generic aliases (`god`, `captain`,
`colonel`, `admiral`, `chief`, `first`, `one`, `voice`, `thing`, …) that are not
in `_UNVETOABLE`, and 14 aliases claimed by more than one character. The
cheapest real risk reduction here is upstream of the veto entirely: stop
recording bare generic appellatives as aliases.

**Not fixed by inventing a discriminator.** Building a new unmeasured detector
for this shape is precisely the mistake §2 exists to correct. Three levers were
taken instead, in order of confidence.

### Lever 1 — `require_approval: true` (applied)

Merges are now held for the operator rather than applied unattended. Costs
spoilers (see the 2026-09-04 record); buys certainty. This is what actually
closes the two-gods case.

### Lever 2 — alias hygiene (applied, and mostly a no-op)

Every candidate term was measured individually against both books before any
was added. The blanket list first proposed would have destroyed **7 real
refusals**:

```
term                       vetoes lost
god, goddess, thing                  0   added
captain, admiral, colonel            0   added
general, sergeant, chief             0   added
lieutenant, major, president         0   added
doctor, professor, priest, voice     0   added
officer                              2   EXCLUDED  Police Officer + Captain, ...
one                                  2   EXCLUDED  Chrysalis + One of the Ones Above, ...
first                                3   EXCLUDED  Dusk + First of the Sky, ...
```

The excluded three are the mirror of the whole-cast problem: for a character the
book never named — "Police Officer", "One of the Ones Above", "First of the Sky"
— the generic word is the *only* term it owns, and suppressing it leaves the
entry unable to veto anything. `second` measured free but is excluded as the
same class as `first`.

Honest result: the added subset changes **zero vetoes on either book**. It
completes a list that already held `king`/`queen`/`lord`/`lady`/`master` but not
`god`/`captain`/`general`, and nothing more.

### Lever 3 — the live run (done, via the browser tier)

The API tier's daily safety budget was spent (450/450 and 50/50, keyed to
America/Los_Angeles), so the pass was driven through the persistent web session,
which consumes no API quota. **First live observation of the roster stage on
either book.**

```
the-finest-edge-of-twilight  59 entries   0 proposals
isles-of-the-emberdark       67 entries   1 proposal: white_haired_being + hoid
                                            -> rejected at grounding,
                                               "no source evidence available"
```

Three things follow.

**The §2 worry was overstated.** `patji` + `insect_god` was *not* proposed,
despite the shared divine appellative. The roster prompt already carries
*"characters who merely share a title or species are DIFFERENT people. Do not
propose them"*, and it held.

**But the mechanism is real.** The single proposal it did make is driven by
exactly that: `white_haired_being` and `hoid` both carry the alias
`master`/`Master`, and nothing else links them. So shared generic appellatives
do induce proposals — the guard is the prompt and the grounding stage, not the
veto layer, which returned `None` for this pair.

**Grounding is the load-bearing gate**, and it worked. It is also the one that
would drop a *genuine* proper-name/appellative duplicate, since those by
definition rarely co-occur — the same structural tension as §2, now on the other
side of the ledger.

### The API tier, run live the next day

Repeated once the budget rolled over at Pacific midnight. Both tiers have now
been exercised:

```
                     web tier                    API tier (flash-lite roster)
book 1 (59)          0 proposals                 0 proposals
book 2 (67)          white_haired_being + hoid   starling + starling
                     -> dropped at grounding     -> a self-pair
total cost: 2 x gemini-3.5-flash-lite + 1 x gemini-3.5-flash
```

Neither book produced a merge on either tier, which is the expected answer.

**The API roster stage returned a character merged into itself.** `merge_veto`
refuses that, but it runs in the *caller*, so the pair reached the grounding
stage and spent a second API call to be told nothing. `adjudicate_cast` already
pre-filters ids that are not in the roster; a self-pair now short-circuits the
same way. Test verified to fail with the filter removed.

Worth noting the tiers disagreed in character: the web tier (Pro) made one
plausible proposal, the API tier (flash-lite) made one nonsensical one.

### A finding that did not survive measurement

An earlier draft of this section reported **phantom entries**: 35 of book 2's 68
cast members with zero dialogue, *"24 of which have a designed voice assigned,
each costing a reference clip and TTS design time for a character that never
speaks."*

**That was wrong.** Counting attributed lines in the finished scripts rather
than trusting the stored estimate:

```
                              est. 0    truly silent   speak anyway   silent AND holding a voice
the-finest-edge-of-twilight        8              8              0                            0
isles-of-the-emberdark            35             11             24                            0
```

24 of the 35 speak — `mother_frond` 33 lines, `captain` 25, `frost` 23, `ruen`
21. And **no truly silent character holds a voice in either book**: the 24 with
voices are exactly the 24 that speak. Casting was right; the inference was not.
The 2026-09-04 record's "no measured case" for spurious entries still stands.

### What it actually was: a count that is never reconciled

`dialogue_count` is pass-1's estimate. `sync_dialogue_counts` exists to replace
it with the real figure, and the registry is written to disk right after — but
in the adjudicator that sync is gated on `local_resolved_count > 0`, and in the
audit on `if repaired:`. A book those passes had nothing to fix keeps the
estimate forever.

That is not cosmetic. `cast_identity.choose_primary` decides **which side of a
merge survives** by dialogue count, and the whole-cast roster prompt shows the
count to the model. With `mother_frond` stored at 0 while speaking 33 lines, a
merge involving her would absorb her into the other entry rather than the other
way round.

Fixed by calling `sync_dialogue_counts` unconditionally just before the registry
is saved at the end of the attribution stage — the sync is "make the stored
count match the script", which has nothing to do with whether adjudication
changed anything.

### Fragment aliases (done, 2026-09-10, second pass)

`_derive_character_aliases` splits a multi-word name and records the first and
last word as standalone aliases:

```
"White-Haired Being"  -> [master, White-Haired Being, White-Haired, Being, White, Haired]
"Mother Frond"        -> ... Mother, Frond
"Second of the Soil"  -> ... Second, Soil
"Police Officer"      -> ... police, Officer
```

35 such fragments in book 1, 51 in book 2. `Being`, `White`, `Haired`, `Ones`
are not names, and they are what the roster model reads — the one web-tier
proposal was driven by `master`/`Master` appearing on both sides.

#### The constraint that shapes the rule

Some books never give a character a proper name at all. "The Dark One", "The
Master", "The Elder Ones" can be the only handle a character has, for most of a
book or all of it. Any rule that suppresses generic words would leave those
characters unidentifiable — and Lever 2 already measured the same trap from the
other side: `officer`, `one` and `first` had to be excluded because for "Police
Officer" and "First of the Sky" the generic word is the only term they own.

So the rule never touches a **name**. `prune_ambiguous_fragment_aliases` removes
only *single-word* aliases, never a multi-word form, never an alias that is some
entry's own name, and never the last one standing. Checked directly:

```
the_master    ['The Master']                  -> unchanged
the_dark_one  ['The Dark One', 'Dark', 'One'] -> full name always retained
elder_ones    ['The Elder Ones', 'Ones']      -> full name always retained
```

#### What is removed, and why frequency is not the test

A single-word alias goes when it is ambiguous or is not used as a name:

* more than one entry claims it, or
* the analyser derived it by splitting, and it never appears capitalised
  mid-sentence in the source.

Sentence-initial capitals are excluded — "Being" opening a sentence proves
nothing. **Frequency is deliberately not a criterion.** A first draft required
three mid-sentence hits and dropped `Applecheeks` (one occurrence, a character
with ten lines), `Mallabritches` and `Terdidy`. A surname mentioned once is
still a surname.

A second draft only counted occurrences *outside* the full name, to stop "One"
inside "the Dark One" voting for itself. That over-dropped too: it excludes
sentence-initial use, so `Shin` looked unused when *"Shin nodded."* is a
perfectly good standalone reference. Both refinements were discarded.

#### Removal is per alias string, not per owner

A test caught this before it shipped. Dropping `Brie` from `catti_brie` — where
it is a fragment — but leaving it on `breezy`, where the model supplied it,
turns a correct abstention into a confident **wrong** answer: with one claimant
left, *"Brie said"* starts resolving to the daughter. Ambiguous names are now
removed from every owner or from none.

That also settles the alias behind the live bogus proposal. `master` is a
fragment for neither `hoid` nor `white_haired_being`, so a fragment-only rule
would have left it; two claimants is enough.

#### Measured on both books

```
                              aliases removed   entries newly left with none
the-finest-edge-of-twilight                10                             0
isles-of-the-emberdark                     40                             0
```

Every removal inspected: `Brie` from both owners, `master`/`Master` from both,
`Being`/`White`/`Haired`, `(Male)`/`(Female)`, the `Second`/`First`/`Above`
collisions, and the word salad from descriptive ids
(`First Company Vice President of Supply` shed `Supply`, `First`, `Vice`,
`President`). `Effron` is **kept** on both claimants because it is a real
character's name — attribution already handles that collision, and deleting the
name would punish its owner for someone else's mis-claim.

Runs in `_consolidate_accumulated_characters`, which is the first point that
sees both the whole roster and the source.

#### Triangulated on a third book, which changed the rule

*The Shadow of the Gods* (John Gwynne, 56 chapters, 923k chars) names people in
a different idiom: Norse given names plus hyphenated epithets — `Battle-Grim`,
`Wave-Jarl`, `Sea-Wolf`, `Raven-Feeders` — and shared titles (`Jarl`, 97
mid-sentence occurrences). It exposed a case the first two books did not.

`_derive_character_aliases` splits an id into words, so `the_battle_grim` yields
`Battle` and `Grim`. Both are capitalised in the text, so the first rule kept
them — but almost every capital is *inside the compound*:

```
frag       total   inside compound   outside   any-case outside
Grim         128               127          1                  5
Feeders       34                34          0                  2
Troll         36                30          6                135
Half          32                30          2                 82
Wolf          24                19          5                 56
```

Alias lookup is casefolded (`script_generator` lowercases both sides), so
`Troll` as an alias is 135 chances to mis-attribute against 6 to help. The test
became: ignore occurrences inside the full name, and of what is left require the
capitalised form to hold the majority. Measured across all three books, that
separates cleanly with nothing in between:

```
real names   Entreri 1.00  Helka 1.00  Dajer 1.00  Bloodsworn 1.00
             Tainted 0.91  Frond 0.86
debris       Grim 0.20  Battle 0.13  Wolf 0.09  Troll 0.04  Half 0.02
             Being 0.02  Feeders 0.00  Shin 0.00
```

Two earlier attempts at this test failed and are recorded above: a frequency
threshold, and a standalone count that excluded sentence-initial use.

**The third book also caught a regression before it shipped.** Excluding the
article-less form of the name ("Battle-Grim" for "The Battle-Grim") is necessary
for compounds — but for "The Bloodsworn" the article-less form *is* the
fragment, so excluding it erased all the evidence and condemned `Bloodsworn` and
`Tainted`: precisely the epithet-only characters the rule exists to protect.
Guarded, and pinned by a test that fails without the guard.

Final measurement, all three books:

```
                              aliases removed   entries newly left with none
the-finest-edge-of-twilight                19                             0
isles-of-the-emberdark                     53                             0
the-shadow-of-the-gods                     12                             0
```

Nothing loses its own name. `Soil`, dropped as a common noun, still resolves:
`_detect_new_characters` matches an exact id before it consults aliases.

Thirteen tests; three verified to fail when the rule they pin is removed.

## Related

- [README.md](README.md) — status convention and index
- [2026-09-04 Whole-cast duplicate detection](2026-09-04-whole-cast-duplicate-detection.md)
- [2026-09-07 Pronunciation lexicon & STT](2026-09-07-pronunciation-lexicon-and-stt-validation.md)
- [Targeted block adjudication plan](../plans/targeted-block-adjudication-2026-09-06.md)
