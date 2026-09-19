# Mobile chapter progress, timeframes and playback sync — plan, 2026-09-16

**Status:** Proposed. Not started.

**Spans two repos.** The evidence is in this one (`crazy-audiobook-creator`, the
backends); the fix is almost entirely in `E:\Projects\Voice` (the companion
app). Paths below are prefixed `Voice/` when they refer to the app.

**Blocked on one measurement.** Step 0 decides whether the whole approach is
viable. Do not write code before it returns.

---

## Symptom

For full-stream audiobooks served from the NAS, the phone scrubber sits pegged
at 100% and Android Auto shows the whole-book duration (13.5 h) instead of the
current chapter (37:37). Scrubbing, remaining-time and next/previous are all
wrong as a consequence.

## Root cause, traced

`brain/orchestrator/nas_syncer.py:808` gives every chapter of a book with a full
M4B export the *same* stream URL, distinguished only by a fragment:

```python
stream_url = f"{project_id}/full/{quote(full_m4b_name)}#chapter={idx}"
```

A URL fragment is never sent to a server, and OkHttp strips it. So 32 chapters
resolve to one 740 MB file. Their `start_ms`/`end_ms`
(`brain/orchestrator/nas_syncer.py:793-798`) are absolute offsets into the whole
book.

The app's `validChapters` branch
(`Voice/core/data/impl/.../CrazyBookSyncService.kt:181-211`) turns each of these
into its own `Chapter` entity carrying that chapter's duration and a single
`MarkData`.

That single mark is then flattened. `Chapter.parseMarkData()`
(`Voice/core/data/api/.../Chapter.kt:60`) unconditionally forces the *first*
mark to `startMs = 0`, so every chapter ends up with exactly one mark spanning
`(0, duration-1)`.

From there the two visible failures follow mechanically:

- **Android Auto.** `MediaItemProvider.kt:268` computes
  `needsClipping = mark.startMs > 0 || (chapterMarks.size > 1 && …)` — both
  false. `ClippingConfiguration.UNSET`. ExoPlayer opens the whole file and
  reports its real 13.5 h duration, which the Auto scrubber uses. Clipping was
  never *broken*; it was never engaged.
- **Phone scrubber.** `BookPlayViewModel.kt:257` does
  `relativePosition.coerceIn(0L, currentMark.durationMs)`. The player's position
  runs into the 13.5 h file while `currentMark.durationMs` is 37 min. The clamp
  pegs it at 100%.

A third, unreported consequence: `CrazyBookSyncService.kt:562-587` downloads
`rawDlUrl` once per chapter into `chapter_001.m4b`…`chapter_032.m4b`. With the
fragment stripped that is 32 copies of the same 740 MB file — **~24 GB** for one
offline book.

## What is already built

The playback layer needs no changes. `MediaItemProvider.kt:264-316` already
emits per-mark `ClippingConfiguration`, mark-relative `durationMs`, and the
Android Auto `elapsed (-remaining)` subtitle. `PlaybackItems.kt` already maps
multi-mark chapters to clipped items and converts positions in both directions.
`BookPlayViewModel` is mark-relative throughout. All of it is committed
(`Voice@4d186ec29`, clean tree).

The entire fix is in the data layer. The playback layer gets a test, nothing
more.

---

## The constraint that shapes everything: there are two backends

They model chapters differently, and only one of them is broken. Any detection
logic has to tell them apart.

| | NAS streamer (`nas_syncer.py`) | Local creator (`mobile.py`) |
|---|---|---|
| chapter `stream_url` | same M4B + `#chapter=N` | `.../stream/chapter/{n}?format=aac` — genuinely distinct files |
| chapter `start_ms` | absolute book offsets | **`0` for every chapter** (`mobile.py:489-493`) |
| book `stream_url` | the full M4B | **always non-blank** (`mobile.py:330`, `:529`) |
| `status` | `ready_full` | can also be `ready_full` (`mobile.py:269-270`) |

The local backend is working correctly today and must keep working. Its chapters
are real separate streams; collapsing them would be a regression, and a severe
one: with all 32 marks at `startMs = 0`, `parseMarkData`'s
`.distinctBy { it.startMs }` **silently drops 31 of them**, leaving one 13.5 h
chapter with no navigation at all.

This rules out `status == "ready_full"` and `book.streamUrl.isNotBlank()` as
detection signals. Both are true on the local backend.

---

## Step 0 — verify Range support before writing code

Clipping to chapter 30 means seeking roughly 700 MB into the M4B. If the NAS
streamer answers `200` instead of `206`, every chapter start pulls the file from
byte zero and the approach is dead on arrival.

The local backend is fine — `brain/dashboard/api/main.py:1962-1968` uses
`FileResponse` with `Accept-Ranges: bytes`. The NAS streamer is **not in this
repo** and is unverified.

```bash
curl -s -o /dev/null -D - -r 700000000-700001000 "http://192.168.50.180/PROJECT_ID/full/BOOK.m4b"
```

Expect `HTTP/1.1 206 Partial Content` and a `Content-Range` header. Anything
else stops this plan; the fallback is to make the streamer serve per-chapter
byte ranges, which is a different change in a different place.

---

## Step 1 — detect the single-stream shape correctly

In `Voice/core/data/impl/.../CrazyBookSyncService.kt`, add the predicate
**above** the existing branch chain, and evaluate it before
`validChapters.isNotEmpty()` in all three places that branch on chapter shape
(catalog build at `:181`, remote progress at `:291`, local remap at `:333`).

```kotlin
val distinctChapterStreamBases = validChapters
  .mapNotNull { it.streamUrl?.substringBefore('#')?.trim() }
  .filter { it.isNotBlank() }
  .distinct()

val hasAbsoluteOffsets =
  validChapters.count { (it.startMs ?: 0L) > 0L } >= validChapters.size - 1

val isSingleFullStream =
  publishedDeliveries.isEmpty() &&
  validChapters.size > 1 &&
  distinctChapterStreamBases.size == 1 &&
  hasAbsoluteOffsets
```

`distinctChapterStreamBases.size == 1` is the discriminator. `hasAbsoluteOffsets`
is not a heuristic — it is a **precondition**: without absolute offsets there is
nothing to build marks from, so the branch must not be taken even if the URLs
collapse. `size - 1` allows chapter 1 to legitimately start at 0.

Deliberately **not** used: `status == "ready_full"`, `detail.status`,
`book.streamUrl.isNotBlank()`. Each is true on the local backend and would
misclassify it.

If `distinctChapterStreamBases.size == 1` but `hasAbsoluteOffsets` is false, log
a warning naming the project — that is a backend bug worth surfacing, not a
silent fallthrough.

## Step 2 — build one chapter with every mark

For `isSingleFullStream`:

- `ChapterId` = the single clean base URL (fragment stripped).
- `markData` = one `MarkData` per entry in `detail.chapters` that has a
  duration, named `"${ch.number}::${cleanTitle}"` to match the existing
  `chapterNumber`/`displayTitle` parsing in `ChapterMark.kt`, with
  `startMs = ch.startMs`.
- Filter out chapters with no duration. On a `ready_partial` book these would
  create marks pointing past the end of the audio.

**Duration is load-bearing.** `parseMarkData` truncates via
`endMs = if (next.startMs <= duration - 2) next.startMs - 1 else maxEndMs`. If
the chapter entity's `duration` underestimates the real media, every mark past
that point collapses into one. `totalDurationSeconds` is a *sum of chapter
durations* (`nas_syncer.py:851`), not the M4B's measured length, so the two can
diverge through encoder padding. Take the larger of the sum and the last
chapter's `end_ms`, and log a warning when they differ by more than a second.

## Step 3 — migrate positions without losing anyone's place

Three separate stores hold positions, and the superseded plan addressed only the
first.

**Progress, remote.** In the `getProgress` handling, for `isSingleFullStream`
map to `targetMark.startMs + remotePosInChapter` rather than assigning
`remotePosInChapter` directly. This restores symmetry with
`syncProgressToServer` (`CrazyBookSyncService.kt:~530`), which already converts
the other way through marks and is correct as written.

**Progress, local.** In the remap branch, resolve the old mark, take
`localChNum`, find that number among the new marks, and set
`targetMark.startMs + localRelPos`. Where the old `positionInChapter` already
falls inside a new mark's interval, preserve it verbatim.

**Guard against the crash.** `Voice/core/data/api/.../Book.kt:11-19`:

```kotlin
check(chapters.map { it.id } == content.chapters)                     // hard failure
val currentChapter: Chapter = chapters[content.currentChapterIndex]   // -1 -> IndexOutOfBounds
```

Any window in which `content.currentChapter` still holds `…#chapter=5` while
`content.chapters` is `[cleanBase]` throws on Book construction. The
chapter-list write and the `currentChapter` remap must land in the same
`BookContent.put`, and the remap needs an explicit fallback to index 0 when the
old ID resolves to nothing.

**Bookmarks.** `Bookmark` stores `(bookId, chapterId, time)` with `time`
chapter-relative. After migration every bookmark on a migrated book points at a
ChapterId that no longer exists, holding a position in the wrong coordinate
space. Apply the same `mark.startMs + time` conversion, keyed by the old
chapter's mark number. Bookmarks that cannot be resolved should be left in place
rather than deleted — a stale bookmark is recoverable, a deleted one is not.

## Step 4 — offline download

For `isSingleFullStream`, fetch the base URL **once** into
`crazy_downloads/PROJECT_ID/PROJECT_ID.EXT` and build a single local `Chapter`
carrying all marks. In `updatedContent`, keep the absolute `positionInChapter`
instead of resetting it to `relPos`.

**Clean up after the old scheme.** Delete any pre-existing
`crazy_downloads/PROJECT_ID/chapter_*.m4b` — up to 24 GB per book — and prune
the orphaned `chapters2` rows for the retired `#chapter=N` ChapterIds. Neither
is cosmetic; without the first, the fix leaves the disk exactly as full as the
bug did.

## Step 5 — measure chapter-switch latency

32 MediaItems on one URL means 32 separate `MediaSource` instances. Each
transition reopens the connection and re-parses the `moov` atom of a 13.5 h M4B;
Media3 does not share that work across items. This is expected to be acceptable
and may not be. Time a cold next-chapter transition and a cold start into
chapter 30.

If it is bad, the fallback is a single un-clipped MediaItem with mark-relative
position reported through the session metadata — which fixes the phone but
returns the 13.5 h scrubber to Android Auto. That is a worse outcome, so measure
before assuming it.

---

## Explicitly out of scope

**The `" - - Book"` title suffix.** It comes from `metadata["title"]` in the
project's `book.json`; `nas_syncer.py:616` is a pass-through, not a formatter.
Stripping it in the app's sync service treats bad stored data at the furthest
point from its cause, and the regex would mangle a book legitimately titled
`"… - Book"`. Fix whatever writes the metadata, in its own change. It has
nothing to do with progress sync.

---

## Verification

### Automated — data layer (new; this is where the bug lives)

`CrazyBookSyncService` currently has no tests. These three matter more than
anything in the manual pass:

1. **NAS shape** — detail with one shared stream base and absolute `start_ms`
   → exactly 1 `Chapter`, 32 `ChapterMark`s, mark *n* starting at chapter *n*'s
   offset.
2. **Local shape** — detail with 32 distinct `stream_url`s and `start_ms = 0`
   → 32 `Chapter`s, **not** collapsed. This is the regression guard for Step 1.
3. **Migration** — a book with existing local progress mid-chapter-17 migrates
   to the same absolute millisecond, and a pre-migration bookmark resolves to
   the same audio instant.

### Automated — playback layer (one test)

Add to `Voice/core/playback/src/test/.../PlaybackItemsTest.kt`: a single-file
multi-mark chapter produces correctly clipped `PlaybackItem`s and round-trips
positions. The existing tests at `:16-40` already cover the two-mark case; this
extends the shape, it does not re-prove the mechanism.

```powershell
cmd /c "gradlew.bat :core:data:impl:testDebugUnitTest"
```

```powershell
cmd /c "gradlew.bat :core:playback:testDebugUnitTest"
```

```powershell
cmd /c "gradlew.bat :features:playbackScreen:testDebugUnitTest"
```

### Manual, on device and in Android Auto

- Chapter scrubber runs `00:00` → chapter length (e.g. `37:37`), with correct
  remaining time.
- Android Auto subtitle and scrubber show the chapter, not 13.5 h.
- Scrubbing lands where it says it does.
- Next/previous move one chapter, cleanly.
- Reader highlights track the audio.
- **Resume from a bookmark created before the migration.**
- **Cold start into chapter 30, with a stopwatch.**
- Offline download of a full-stream book writes one file; the old
  `chapter_*.m4b` set is gone.

### Deploy

```powershell
python scripts/deploy_voice_apk.py --build
```

Confirm all three targets (NAS streamer `192.168.50.180`, creator root, Voice
root) return 200/206 with identical byte sizes.

---

## Risks

| Risk | Handling |
|---|---|
| NAS streamer lacks Range support | Step 0, before any code |
| Local backend misclassified as single-stream | Strict predicate, Step 1; test 2 is the guard |
| `Book.kt` init crash mid-migration | Atomic write + index-0 fallback, Step 3 |
| Bookmarks silently orphaned | Remap, keep unresolvable ones, Step 3 |
| Duration underestimate collapses tail marks | Larger-of-two + divergence warning, Step 2 |
| Chapter-switch latency from `moov` re-parse | Measured in Step 5; fallback documented |
| 24 GB stale downloads persist | Cleanup in Step 4 |

## Open questions

One, and it gates the plan: **does the NAS streamer serve HTTP 206 for a
mid-file range on the full M4B?** Unverifiable from this repo.
