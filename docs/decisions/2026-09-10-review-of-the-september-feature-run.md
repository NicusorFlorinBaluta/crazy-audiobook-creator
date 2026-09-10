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
- Scene-break silence was 100 ms, below the threshold where a listener hears a
  break at all. Now `PAUSE_MARKER_SILENCE_SECONDS = 0.9`, which does change those
  62 lines in book 1.
- `deploy_voice_apk.py` trusted any SSH host key (`AutoAddPolicy`) while sending
  credentials; it now loads `known_hosts`, rejects unknown keys, and prints the
  one-time `ssh-keyscan` enrolment command. URL probes check the scheme first.

## Verification

```
ruff check .        All checks passed        (was 19 errors, 3 of them F821)
pytest              726 passed, 2 skipped    (was 713; +13 tests)
BLE001 ratchet      116                      (was 139; ratchet target was 118)
live cast pass      both books, both tiers, 3 API requests total
```

Each regression test for a shipped defect was verified to fail when the defect is
reinstated, following the convention set by `25e7d8a`.

## What this review did not check, and one gap it exposed

The cast pass has been run live on both books through **both** tiers (Lever 3).
Still unexercised: the **audio-QA** path, and the 429 handling in §5 — no real
rate limit or per-day 429 was observed, only the local safety budget, so that
code remains covered by fakes alone.

The block-adjudication rollout gate (dry-run both paths, diff every
disagreement) remains unrun — the flag is off, not validated.

**Fragment aliases** are measured but unfixed; see Lever 3.

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

### Still open: fragment aliases

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

**Not fixed here.** The measurement in Lever 2 is the warning: for characters
the book never named, the generic fragment can be the only term they own, and
suppressing it cost real vetoes. The same risk applies to removing these
aliases, and attribution resolves lines through them. It needs its own measured
pass, not a change tacked onto this one.

## Related

- [README.md](README.md) — status convention and index
- [2026-09-04 Whole-cast duplicate detection](2026-09-04-whole-cast-duplicate-detection.md)
- [2026-09-07 Pronunciation lexicon & STT](2026-09-07-pronunciation-lexicon-and-stt-validation.md)
- [Targeted block adjudication plan](../plans/targeted-block-adjudication-2026-09-06.md)
