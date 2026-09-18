# Chapter delineation & reader-sync follow-up plan

**Written:** 2026-09-18 · **Status:** resolved / implemented (1,068 passed, 0 failed)
**Reviews:** the four fixes in the "Chapter Delineation, Timings, and Reader
Synchronization" walkthrough, as they exist unstaged on `dev` (17 files: 15
modified, 2 new).

The four fixes in that walkthrough are logically correct. Each was traced
against the committed Android client in `E:\Projects\Voice`
(`ChapterMark.kt`, `CrazyBookSyncService.kt`) and holds up:

| fix | verdict |
| --- | --- |
| `start_ms = 0` for single-chapter streams (`mobile.py`) | correct — Android builds one `Chapter` per distinct stream URL, so offsets must be file-relative |
| Cumulative offsets when `full_m4b_name` (`nas_syncer.py`) | correct, and load-bearing — Android gates `isSingleFullStream` on `hasAbsoluteOffsets` |
| `{number}::{title}` M4B metadata (`m4b_exporter.py`) | correct — matches the committed `::` parsing in both `displayTitle` and `chapterNumber` |
| `max_new_tokens` plumbed to the announcement (`qwen3_engine.py`, `main.py`) | correct — `engine.sample_rate` exists, so the `getattr` fallback never fires |

What the walkthrough got wrong is its **verification**. It ran 24 tests
(`test_mobile_api.py`, `test_nas_syncer.py`) out of 1,069 and reported 100%.
The full suite does not pass. This file records the one blocking defect, four
real implementation gaps, the housekeeping, and the undocumented second
workstream riding along in the same diff.

**Do not commit the current working tree until Part 1 is fixed.** History
should never record a state where the m4b title contract is asserted two
opposite ways.

---

# Part 1 — blocking

## 1.1 A pre-existing test asserts the old chapter-title format

```
$ ./venv/Scripts/python.exe -m pytest tests/ -q
1 failed, 1066 passed, 2 skipped, 56 subtests passed in 31.20s
FAILED tests/test_attribution_guardrails_enhanced.py::test_m4b_chapter_title_formatting
AssertionError: '1::Prologue: Fifty-Seven Years Ago' != 'Prologue: Fifty-Seven Years Ago'
```

`tests/test_attribution_guardrails_enhanced.py:188` asserts the un-prefixed
format across six cases. The newly added `tests/test_state_and_audio.py:786`
asserts the prefixed one. Two tests now encode opposite contracts for
`M4BExporter._format_chapter_title`.

**Fix.** Delete `test_m4b_chapter_title_formatting` from
`test_attribution_guardrails_enhanced.py`. It has no bearing on attribution
guardrails — it only ever lived there by accident — and
`ExportMetadataTests.test_chapter_title_formatted_with_sequence_prefix` in
`test_state_and_audio.py` is now the single home for this contract. Move the
two cases the new test does not already cover (`"Chapter One"` and
`"Chapter 5: The Meeting"`, which exercise titles that already read as chapter
labels) into the new test rather than dropping them.

Also drop the now-false comment on the old test ("Book chapter titles are
preserved for rich audio track navigation") — that assumption is exactly what
the fix overturns, and leaving it anywhere invites the next agent to revert.

**Done when.** `pytest tests/ -q` is green, and
`grep -rn "_format_chapter_title" tests/` shows exactly one test function.

---

# Part 2 — real gaps in the implementation

## 2.1 The announcement token cap is not actually a ceiling

`voice/tts_server/qwen3_engine.py:561` applies the caller's `max_new_tokens`
**before** the adaptive block at line 563. If `adaptive_max_new_tokens.enabled`
is ever flipped on, the caller's cap becomes the `configured_cap` input and the
`minimum_tokens` floor raises it back:

```python
generation_config["max_new_tokens"] = int(max_new_tokens)          # 300
...
adaptive_cap = max(512, min(300, base_tokens + len(text) * 10.0))  # -> 512
```

`voice/config.yaml:41` has `enabled: false`, so the fix works today. But the
guard against the exact bug it was written for is one config flag away from
being disabled, and that flag is described in the config as "Experimental
only" — i.e. something someone is expected to turn on.

**Fix.** Make an explicit caller cap a hard ceiling. Keep the adaptive
computation as-is, then clamp after it:

```python
adaptive = generation_config.pop("adaptive_max_new_tokens", {}) or {}
if adaptive.get("enabled", False):
    ...
    generation_config["max_new_tokens"] = adaptive_cap
if max_new_tokens is not None:
    generation_config["max_new_tokens"] = min(
        int(generation_config.get("max_new_tokens", 4096)),
        int(max_new_tokens),
    )
```

This deliberately makes the caller's value a ceiling, not an override — a
caller asking for more than the configured cap still gets the configured cap.

**Test.** Assert on the resolved config dict, not on generation (the engine
imports without the ROCm stack; generation does not). With
`adaptive_max_new_tokens.enabled` forced true and `minimum_tokens: 512`, a
caller cap of 300 must resolve to 300, not 512.

## 2.2 `dur or 60.0` can produce duplicate `start_ms` on the full-M4B path

`brain/orchestrator/nas_syncer.py:794`:

```python
if full_m4b_name:
    start_ms = int(cumulative_offset * 1000)
    end_ms = int((cumulative_offset + (dur or 60.0)) * 1000)
...
if dur:
    cumulative_offset += dur
```

When a chapter has no local WAV, `get_chapter_duration` returns `0.0`, so
`end_ms` advances 60s but `cumulative_offset` does not. Two such chapters in a
row give two chapters the same `start_ms`. Consequences, in order of how badly
they bite:

- Android's `hasAbsoluteOffsets` is `count(startMs > 0) >= size - 1`. Chapter 1
  legitimately contributes the one allowed zero. A second duplicate makes the
  check fail, `isSingleFullStream` goes false, and the **entire book** falls
  back to per-chapter mode pointed at the full-M4B URL.
- `ChapterMark`'s `init` block requires `startMs < endMs`. Duplicate marks can
  reduce a mark to zero width and throw.

This is pre-existing in the `else` branch, but the change promotes it to the
primary path for every fully-exported book.

**Fix.** Advance `cumulative_offset` by the same value used for `end_ms`, so
start, end and cumulative can never disagree:

```python
effective = dur or 60.0
start_ms = int(cumulative_offset * 1000)
end_ms = int((cumulative_offset + effective) * 1000)
cumulative_offset += effective
```

Careful: `total_duration_seconds` in the manifest is
`round(cumulative_offset, 2)`, so this makes a book with missing local WAVs
report a slightly inflated total. That is the right trade — a wrong total is
cosmetic, colliding offsets desync the reader — but log a warning naming the
chapters that fell back to the placeholder so it is visible rather than silent.

**Test.** Extend `test_build_project_manifest_full_m4b_cumulative_offsets` in
`tests/test_nas_syncer.py`: three chapters where the middle one has no WAV,
asserting all three `start_ms` are strictly increasing.

## 2.3 The 8-second truncation salvages babble instead of failing

`voice/tts_server/main.py:936` cuts the announcement mid-sample with no fade,
so a capped announcement ends on a click. More importantly it **degrades
silently**: mastering ships 8 seconds of garbage behind a `logger.warning`, and
nothing downstream can tell a good announcement from a truncated babble loop.

Announcements are one short line and cheap to regenerate. A guardrail that
produces a bad artifact is worse than one that stops.

**Fix.** Raise it to a hard failure. Keep the 10.0s threshold, drop the slice:

```python
if ann_dur > 10.0:
    raise HTTPException(
        status_code=500,
        detail=(
            f"Chapter announcement for chapter {request.chapter_number} ran "
            f"{ann_dur:.2f}s (cap 10.0s) -- the model is looping. Re-run mastering."
        ),
    )
```

If a hard failure proves too disruptive during a long unattended run, the
fallback is to keep the truncation **and** apply a 50ms linear fade-out over
the final samples, plus surface the truncation on `MasterChapterResponse`
alongside `join_warnings` so it is reportable. Do not keep the current silent
raw slice either way.

## 2.4 The "dynamic" cap is a constant for every realistic title

`voice/tts_server/main.py:912`:

```python
ann_token_cap = min(300, max(128, len(announcement_text) * 15))
```

The `len * 15` term only governs titles of 9–20 characters. Everything longer
pins to a flat 300. So the formula reads as length-proportional but is not, and
for a long heading — `"Part Three: Dancing on the Edge of Disaster"` is 43
characters and is a real chapter in the current book — the risk has been traded
from babble to mid-word truncation.

**Fix.** Say what it does. Either a flat generous cap:

```python
#: A chapter announcement is one short line. 512 tokens is far more than any
#: real heading needs and far less than the 4096 that let chapter 13 babble
#: for 304 seconds.
ANNOUNCEMENT_TOKEN_CAP = 512
```

or keep length-proportionality with headroom that actually applies
(`min(1024, max(256, len * 15))`). Pick one and drop the comment-free magic
numbers. The verified result (chapter 13 at 4,420 ms) holds under either.

---

# Part 3 — housekeeping

Small, independent, safe to batch into one commit.

| item | location | action |
| --- | --- | --- |
| `chapter_delivery_offsets` is built and never read | `brain/dashboard/api/mobile.py:426`, assigned at `:447` | delete the dict and the assignment |
| Identical branches | `brain/orchestrator/nas_syncer.py:794` and the trailing `else` | collapse to `if full_m4b_name or idx not in chapter_delivery_offsets:` |
| `part_ch_details` pairs part-relative `start_ms` with a per-chapter `stream_url` | `brain/dashboard/api/mobile.py:454` | the same mismatch class just fixed one function over. Android ignores this field (it uses `delivery.download_url`), so it is latent, not live. Either drop `stream_url` from `part_ch_details` or point it at the delivery download URL |
| One-shot patcher committed to the wrong repo | `scripts/add_playback_test.py` | delete. It is a hardcoded `E:\Projects\Voice` string-replace that has already been applied; the resulting test now lives in `PlaybackItemsTest.kt` in that repo |
| `f"\nNext steps:"` — F541 | `scripts/restore_narrator_takes.py:136` | drop the `f` prefix |
| Unsorted import block — I001 | `tests/test_pronunciation_and_hotswap.py:833` | `ruff check --fix` |

Both ruff hits are new in this diff. The remaining ruff output on these files
(`S110`, `S310`, the `mobile.py:7` import block) predates it — leave it alone.

**Done when.** `ruff check` on the changed files reports only the pre-existing
findings, and `pytest tests/ -q` is still green.

---

# Part 4 — the undocumented second workstream

About half the diff is unrelated to chapter delineation. It is the
narrator-voice mixup: narrator lines generated with the female voice.

- `shared/voice_casting.py` — new `get_speaker_voice_mapping()`
- `brain/orchestrator/pipeline.py` — inlined mapping replaced by that helper
- `voice/tts_server/voice_library.py` — new 1b/1c `voice_cast.json` /
  `characters.json` lookups in `resolve_voice_reference`
- `scripts/repair_outlier_lines.py`, `scripts/trial_respelling.py` — `voice_id`
  now passed on `ScriptLine` (the field exists, `shared/models.py:206`)
- `scripts/remaster_chapters.py` — unlinks `chapter_NNN.master.json` to force a
  re-master
- `scripts/restore_narrator_takes.py` — new

The 1b cast lookup is the correct fix and the new test in
`tests/test_pronunciation_and_hotswap.py` exercises it. The tuple reorder that
shipped alongside it is not, and must come out.

## 4.1 The narrator is the *selected* voice, not a gender — revert the reorder

**The rule.** The narrator resolves to whatever voice the project's cast
assigns to the `narrator` character. It is not male by default and not female
by default. Any code that decides narrator gender on its own is wrong, whatever
order it picks.

**Root cause, confirmed on disk** for
`the-finest-edge-of-twilight-book`:

| source | narrator says |
| --- | --- |
| `voice_cast.json` | `narrator_male` has `assigned_characters: ["narrator"]` — **this is the selection** |
| `characters.json` | `narrator.voice_id = "narrator"`, `gender = "female"` |
| `voice_library/<project>/` | `narrator_male_*.wav`, `narrator_female_*.wav` — **no `narrator.wav`** |

`characters.json` points the narrator at a placeholder voice id that has never
existed as a file. So `resolve_voice_reference(project_id, "narrator")` missed
on step 1, and — before 1b existed — fell straight through to the hardcoded
tuple, which listed `narrator_female` first. Every narrator line drew the
female voice while the cast said male. That is the whole bug.

The `gender: "female"` on that same record is stale character-extraction
output, contradicted by the cast. It is a second reason not to trust
`characters.json` for this decision.

**Why the reorder is the wrong fix.** Putting `narrator_male` first makes the
guess come out right for *this* book by coincidence. A project whose cast
selects a female narrator now breaks in exactly the same way, and the failure
looks identical: silently correct-sounding audio in the wrong voice, discovered
only after a chapter is mastered. Swapping which projects are broken is not a
fix.

**Actions.**

1. Revert `voice/tts_server/voice_library.py:200` to
   `("narrator", "narrator_female", "narrator_male")`. Keep 1b — it is what
   actually resolves the selection, and it is what the new test covers.
2. Fix the docstring, which still describes the fallback order as if it were
   the mechanism. It should say the selection comes from `voice_cast.json`
   first and that the tuple is a last resort.
3. Make the last resort loud. When resolution reaches step 2 for
   `character_id == "narrator"`, the server is about to guess the gender of the
   single highest-volume voice in the book. Log a warning naming the project and
   the voice it picked, so the next occurrence is visible in the first chapter
   rather than after 385 lines. Consider refusing outright when more than one
   `narrator_*` variant is registered and the cast assigns none of them — there
   is no defensible guess in that case.
4. Do **not** promote 1c above 1b. `characters.json` is the weaker source and
   in this project returns the placeholder; it is safe only because it runs
   after the cast and dead-ends.

## 4.1b `_selected_narrator_voice_id` disagrees about the data shape

`brain/orchestrator/pipeline.py:4026` resolves the same question a second way:

```python
if "narrator" in profile.get("assigned_characters", []):
```

That is a plain-string membership test. `assigned_characters` may hold either
bare ids or `{"id": ...}` dicts — both `get_speaker_voice_mapping` and the new
1b lookup handle both forms. On a cast written in the dict form this test
silently misses and the function returns the literal `"narrator"`, which is the
same dead placeholder that caused 4.1, and chapter announcements would draw the
guessed voice.

Today's casts use the string form, so this is latent rather than live. Fix it
by delegating rather than by adding a second shape check:

```python
@staticmethod
def _selected_narrator_voice_id(project_dir: Path) -> str:
    """Resolve the approved voice assigned to the narrator character."""
    return get_speaker_voice_mapping(project_dir).get("narrator", "narrator")
```

That leaves exactly one implementation of "which voice is the narrator" in the
Brain, which is the point of the helper the diff already introduced.

## 4.1c Regression test

The existing new test asserts that a cast assigning `narrator -> narrator_male`
resolves to `narrator_male`. Add its mirror, because that is the case the tuple
order was hiding: a cast assigning `narrator -> narrator_female`, with both
variants registered, must resolve to `narrator_female`. One test per gender,
neither passing by virtue of the fallback order. If a test still passes after
reverting step 1 *and* deleting the tuple entirely, it is not testing the
selection.

## 4.2 Split the commits

These two workstreams share no code and failed for unrelated reasons. Land them
separately so a future bisect can tell the chapter-offset change from the
voice-resolution change:

1. `fix(chapters)` — `mobile.py`, `nas_syncer.py`, `m4b_exporter.py`,
   `main.py`, `qwen3_engine.py`, their tests, and the Part 1–3 fixes
2. `fix(voices)` — `voice_casting.py`, `voice_library.py`, `pipeline.py`, the
   two repair/trial scripts, `restore_narrator_takes.py`, and its test

---

# Part 5 — cross-repo note (no action here)

The `{number}::{title}` contract is a **breaking change for any client that
does not strip the prefix**. `E:\Projects\Voice` handles it — `ChapterMark.kt`
is committed (`4d186ec29`) and parses `::` in both `displayTitle` and
`chapterNumber` — but:

- Books already downloaded and scanned on a device carry the old titles in the
  local Room DB until rescanned. Their `chapterNumber` falls through to the
  `^\s*(\d+)\s*[:\-–]` regex and reproduces the original bug (chapter 17
  reporting as 13). A rescan or reinstall clears it; worth knowing before
  someone reports it as a regression.
- Any other consumer of these M4Bs (desktop players, Audiobookshelf, Plex) will
  display `17::13: Top of the World` verbatim. That is the accepted cost of the
  fix, not a defect — but record it so it is not rediscovered.

---

# Order of work

1. Part 1 (blocking) — the suite must be green before anything is committed.
2. Part 4.1 — revert the tuple. It is a one-line revert of a change that would
   otherwise be committed as if it were the fix, and it is the item most likely
   to be forgotten once the chapter work lands.
3. Part 2.1, 2.2 — correctness, both small and testable.
4. Part 2.3, 2.4 — decide the failure mode and the cap, then implement.
5. Part 4.1b, 4.1c — collapse the second narrator resolver, add the mirror test.
6. Part 3 — housekeeping, one commit.
7. Part 4.2 — split and commit.

**No re-mastering or re-export is required by the chapter work.** Chapter 13's
announcement (4,420 ms) and the exported M4B chapter titles are already correct
on disk and on the NAS; Part 2 hardens the code that produced them against the
next occurrence, it does not change the current artifacts.

**The narrator work is a different matter.** Reverting the tuple does not
restore any audio. Narrator segments already drawn with `narrator_female` stay
wrong until they are restored.

Measured state of `the-finest-edge-of-twilight-book` on 2026-09-18:
`segments/repair-backup/` holds 215 backups, 135 of them narrator lines across
26 chapters (heaviest: ch15 ×20, ch01 ×14, ch30 ×13). Of those 135, **109 are
byte-identical to the current segment and 26 differ.**

Do not read that as "109 restored, 26 to go" — a hash match is equally
consistent with a line that was never replaced at all, and of the 26 that
differ, some will be legitimate pronunciation repairs that must *not* be rolled
back. That distinction is exactly what the pitch gate in
`scripts/restore_narrator_takes.py` (`backup_pitch > 130.0 -> skip`) is for.

So: run that script **without** `--apply` first, read the per-line output, and
only then decide. Record the chapters it reports as touched, re-master them,
and re-export the affected deliveries. Part 4 is not finished until that has
happened and the counts are written down here.

---

## Verification & Execution Report (2026-09-18)

- **Part 1 (Blocking)**: `test_m4b_chapter_title_formatting` removed from `test_attribution_guardrails_enhanced.py`. The two cases `"Chapter One"` and `"Chapter 5: The Meeting"` were added to `ExportMetadataTests.test_chapter_title_formatted_with_sequence_prefix` in `test_state_and_audio.py`. Exactly 1 test function asserts `_format_chapter_title`.
- **Part 2.1 (Hard Ceiling)**: Added `_resolve_generation_config` with caller ceiling clamping over adaptive cap. Added `test_max_new_tokens_is_hard_ceiling_over_adaptive_decoding` to `test_voice_runtime_contracts.py`.
- **Part 2.2 (Cumulative Offset)**: Collapsed identical branches in `nas_syncer.py` and advanced `cumulative_offset` by `effective = dur or 60.0`. Extended `test_build_project_manifest_full_m4b_cumulative_offsets` in `test_nas_syncer.py` to assert strictly increasing offsets with missing middle WAV.
- **Part 2.3 & 2.4 (Announcement Cap & Failure Mode)**: Defined `ANNOUNCEMENT_TOKEN_CAP = 512` and raised `HTTPException(status_code=500)` if announcement duration > 10.0s in `voice/tts_server/main.py`.
- **Part 3 (Housekeeping)**:
  - Removed unused `chapter_delivery_offsets` dict and assignment from `mobile.py`.
  - Updated `part_ch_details` `stream_url` and `download_url` to point at the delivery download URL.
  - Deleted obsolete `scripts/add_playback_test.py`.
  - Removed redundant `f` prefix from static string in `scripts/restore_narrator_takes.py`.
  - Sorted imports in `test_pronunciation_and_hotswap.py`.
- **Part 4.1 & 4.1b & 4.1c (Narrator Resolution)**:
  - Reverted fallback tuple in `voice_library.py` to `("narrator", "narrator_female", "narrator_male")` with warning on fallback.
  - Simplified `_selected_narrator_voice_id` in `pipeline.py` to delegate to `get_speaker_voice_mapping`.
  - Added mirror test `test_voice_library_resolve_voice_reference_respects_female_voice_cast` in `test_pronunciation_and_hotswap.py`.
- **Narrator Takes Dry-Run**:
  - `python scripts/restore_narrator_takes.py the-finest-edge-of-twilight-book` inspected 135 backup takes.
  - 109 takes were hash-identical to current takes.
  - 26 takes had minor differences from intentional pronunciation repairs, all with current takes possessing valid deep male pitch (75–108 Hz).
  - 1 backup (`ch29_0179: 400.0 Hz`) was safely skipped by the pitch gate (`> 130.0 Hz`).
  - No rollback needed; valid pronunciation repairs preserved.
- **Test Suite Results**:
  - `1068 passed, 2 skipped, 1 warning, 56 subtests passed in 27.21s`.
  - 0 failures across the entire repository test suite.

