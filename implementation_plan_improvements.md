# Audiobook Creator — Feature Expansion & Windows Consolidation Plan

## Overview

This plan covers two parallel tracks:

1. **Architecture consolidation** — Move the entire pipeline from the 2-machine setup
   (Windows brain + Ubuntu TTS) onto the single Windows machine using the AMD 7900 XTX
   GPU via ROCm. The Ubuntu machine is retired.

2. **Feature expansion** — App branding, scheduling, selective chapter generation,
   detailed progress tracking, book metadata/artwork, deferred deployments,
   multi-speaker voice fix, and model upgrades.

---

## Background: Why Two Machines Existed

The Ubuntu machine (RTX 2080 Super, 8 GB VRAM) was used solely because ROCm on Windows
was not supported when the project was started. As of AMD's 2026 Adrenalin release,
PyTorch with ROCm now works natively on Windows for RDNA3 GPUs. The 7900 XTX has
**25.8 GB VRAM** — more than 3× the Ubuntu GPU — making it significantly more capable.

**Validation done:**
- `PyTorch 2.9.1+rocmsdk20260116` detects the 7900 XTX and reports 25.8 GB VRAM ✅
- `parler-tts-large-v1` loads on the GPU, uses ~5.1 GB VRAM, and generates audio ✅
- `openai-whisper` uses standard PyTorch, compatible with the AMD ROCm venv ✅
- `fastapi`, `uvicorn`, `transformers`, `torchaudio` all pre-installed by AMD Adrenalin ✅

**One known issue fixed:** `audiotools/ml/decorators.py` in the AMD venv has a
`torch.distributed.ReduceOp` reference that crashes on import with PyTorch 2.9.1 ROCm.
The fix is to change the default value of the `op` parameter in the `track()` method
from `dist.ReduceOp.AVG` to `None`. This has already been applied.

---

## Section 1 — Architecture Consolidation (Ubuntu → Windows)

### 1.1 Rename and update the Ubuntu client

**File:** `brain/orchestrator/ubuntu_client.py`

Rename this file to `voice_client.py`. Rename the class `UbuntuClient` to
`VoiceClient`. Update the docstring and all comments to remove Ubuntu references.

Change the default `host` parameter from the Ubuntu IP to `http://127.0.0.1:8100`.

Remove the `reconnect_interval` logic that was designed to handle cross-machine
network drops — that complexity is no longer needed for a localhost connection. Keep
basic retry logic for the brief window while the voice server subprocess is starting up.

Update all imports of `UbuntuClient` throughout the codebase (primarily `pipeline.py`
and `main.py`) to import `VoiceClient` from `voice_client.py` instead.

### 1.2 Auto-launch the voice server from the pipeline

**File:** `brain/orchestrator/pipeline.py`

Currently the pipeline assumes the Ubuntu TTS server is already running on the network.
Instead, the pipeline should manage the voice server as a subprocess.

Add a `_start_voice_server()` method that:
- Launches `voice/tts_server/main.py` using the AMD venv Python interpreter at
  `E:\PyTorch env\my_venv\Scripts\python.exe`
- Waits for the `/health` endpoint to return OK (use the existing `wait_for_server`
  logic from the client)
- Stores the subprocess handle so it can be terminated later

Add a `_stop_voice_server()` method that:
- Sends SIGTERM / `subprocess.terminate()` to the voice server process
- Waits briefly for clean exit, then kills if needed
- Clears the stored handle

Call `_start_voice_server()` at the start of `run()`, before the first stage check.
Call `_stop_voice_server()` in the `finally` block of `run()` so the GPU VRAM is always
released when the pipeline ends, pauses, or errors.

The AMD venv path should come from config (`voice_server.venv`) so it can be overridden
without code changes.

### 1.3 Update brain/config.yaml

Remove the `ubuntu:` block entirely.

Add a new `voice_server:` block:

```yaml
voice_server:
  host: "http://127.0.0.1:8100"
  venv: "E:\\PyTorch env\\my_venv"
  auto_start: true
  startup_timeout_seconds: 120
```

The pipeline reads `voice_server.host` wherever it currently reads `ubuntu.host`.
The pipeline reads `voice_server.venv` to know which Python to use for the subprocess.

### 1.4 Update voice/config.yaml paths

The Ubuntu voice server used Linux-style paths for `workspace_dir` and
`voice_library_dir`. These are currently relative paths (`workspace`, `voice_library`)
which happen to work on both platforms. No changes needed here unless they were
hardcoded to `/home/crazywiz/...` paths — if so, revert them to relative paths so they
resolve relative to the project root on Windows.

Also update the `parler_server.py` path reference inside `voice_designer.py`. Currently
it hardcodes `/home/crazywiz/crazy-audiobook-creator/parler_server.py`. Change this to
use a path relative to the project root so it works on Windows.

### 1.5 Update the M4B export stage

**File:** `brain/orchestrator/pipeline.py` — `_run_export()` method

Currently after the Ubuntu server generates the M4B file, the pipeline downloads it over
HTTP using `self.ubuntu.download_file()`. Since everything is now local, the M4B file
will already be on the same Windows machine — just copy or reference it by path instead
of downloading. Remove the HTTP download step.

The M4B output path on the voice server side (`workspace/{project_id}/output/`) will be
accessible directly as a Windows path. After export, move or copy the file to the project
directory (`brain/projects/{project_id}/{project_id}.m4b`).

### 1.6 Create a Windows setup script

**New file:** `scripts/setup-voice-server.ps1`

This replaces `scripts/install-ubuntu.sh`. It should:
1. Check that the AMD venv exists at `E:\PyTorch env\my_venv` (error with instructions
   if not found — user must install AMD Adrenalin and create the PyTorch venv from it)
2. Install voice server dependencies into the AMD venv:
   ```
   pip install git+https://github.com/huggingface/parler-tts.git soundfile openai-whisper
   ```
   (with `GIT_CLONE_PROTECTION_ACTIVE=false` set for the audiotools dependency)
3. Apply the `audiotools` ROCm patch: in
   `E:\PyTorch env\my_venv\Lib\site-packages\audiotools\ml\decorators.py`,
   change the `op` parameter default in the `track()` method from `dist.ReduceOp.AVG`
   to `None`
4. Verify `ffmpeg` is in PATH (required for M4B export) and print a warning if missing
5. Print a success summary

### 1.7 Delete obsolete Ubuntu files

Delete or archive these files — they are no longer needed:
- `scripts/fix_ubuntu.py`
- `scripts/fix_ubuntu_config.py`
- `scripts/install-ubuntu.sh`
- `ubuntu_voice_designer.py` (root-level script)

---

## Section 2 — Multi-Speaker Voice Fix (Critical)

This is the highest-priority fix. All character voices currently sound identical despite
the Parler bootstrapping step generating unique reference clips. Root cause: two bugs in
`qwen3_engine.py`.

### Root Cause

**Bug 1 — x_vector_only_mode=True is hardcoded.** The engine always calls
`generate_voice_clone()` with `x_vector_only_mode=True`. This extracts only a compact
speaker embedding that captures rough pitch/timbre but discards prosody, speaking style,
and fine-grained voice texture. When multiple Parler voices share broad characteristics
(all English, calm), their x-vectors collapse to nearly identical embeddings.

**Bug 2 — ref_text is always empty.** `ref_text=""` is passed on every call. The
Qwen3-TTS documentation states that providing an accurate transcript of the reference
audio alongside the audio itself (Full ICL mode) significantly improves speaker
similarity. Without a transcript, the model cannot anchor voice features properly.

### 2.1 Upgrade Parler to large-v1

**File:** `voice/tts_server/voice_designer.py` and `parler_server.py`

Change the model from `parler-tts/parler-tts-mini-v1` to
`parler-tts/parler-tts-large-v1`. The 7900 XTX (25.8 GB VRAM) handles it with room
to spare (~5.1 GB used as confirmed by testing). The large model produces substantially
more distinctive voices from text descriptions.

Also update the generation call to use `torch.float16` explicitly and cast the output
to `float32` before passing to `soundfile.write()` (soundfile does not accept float16
arrays — this was the bug found in testing).

### 2.2 Auto-transcribe voice reference clips after bootstrapping

**File:** `voice/tts_server/voice_designer.py`

After Parler generates each character's reference `.wav` file, immediately transcribe it
using the Whisper model that is already loaded for validation. Store the transcript text
in `voices.json` alongside the voice file path, under a `ref_text` field.

This transcription only happens once per character per project (during bootstrapping),
so the performance cost is negligible.

### 2.3 Add ref_text to the voice library schema

**File:** `voice/tts_server/voice_library.py`

Add a `ref_text` field to whatever data structure or JSON schema holds the voice
registry entries. Add a helper method `get_voice_ref_text(project_id, character_id)`
that retrieves the stored transcript for a character's reference clip.

### 2.4 Use Full ICL mode in the TTS engine

**File:** `voice/tts_server/qwen3_engine.py`

In the `_generate()` method, when a `voice_reference` path is provided:
- Retrieve the `ref_text` for that voice from the voice library
- If `ref_text` is available (non-empty), call `generate_voice_clone()` with
  `x_vector_only_mode=False` and `ref_text=<the transcript>`
- If `ref_text` is not available (e.g., bootstrapping failed to transcribe), fall back
  to the current behaviour: `x_vector_only_mode=True`, `ref_text=""`

Log a warning when falling back to x_vector_only mode so the issue is visible in logs.

### 2.5 Pass ref_text through the validation loop

**File:** `voice/validator/validation_loop.py`

When the validation loop resolves a voice reference path for each line, also retrieve the
corresponding `ref_text` from the voice library. Pass both `voice_reference` and
`ref_text` when calling the engine's generate method.

### 2.6 Add group-by-speaker optimization

**File:** `voice/validator/validation_loop.py`

Instead of processing lines in narrative order (which alternates between speakers
rapidly), group all lines for the same speaker together before passing them to the
engine in batches. This avoids the overhead of re-encoding the reference prompt on
every single speaker switch. After generation, reorder the outputs back to narrative
order using the original line indices.

This is the same optimization used in the TTS-Story reference implementation
(`qwen3_voice_clone_engine.py`).

---

## Section 3 — Model Upgrades

The 7900 XTX's 25.8 GB VRAM allows running meaningfully better models than the Ubuntu
2080 Super (8 GB) could support.

### 3.1 Parler: mini → large-v1

Already covered in Section 2.1. Update `voice/config.yaml`:
```yaml
parler:
  model: "parler-tts/parler-tts-large-v1"
```

### 3.2 Whisper: medium → large-v3

**File:** `voice/config.yaml`

Change `whisper_model` from `"medium"` to `"large-v3"`. The large-v3 model has roughly
20% better WER accuracy, which means fewer false validation failures and fewer retries
during generation — saving time on every chapter. The 7900 XTX has enough VRAM to run
Whisper large-v3 alongside Qwen3-TTS without issue.

```yaml
validation:
  whisper_model: "large-v3"
```

### 3.3 VRAM budget summary

For reference, the expected VRAM usage after all upgrades:
- Parler-large during bootstrapping (Qwen unloaded): ~5.5 GB
- Qwen3-TTS during generation: ~3.5 GB
- Whisper large-v3 during validation: ~3.0 GB
- Peak during generation+validation (Qwen + Whisper loaded together): ~6.5 GB
- All well within the 25.8 GB budget.

---

## Section 4 — Character Analyzer Multi-Pass Fix

**File:** `brain/director/character_analyzer.py`

### Problem

The current character analyzer sends the entire book to the LLM in a single prompt, but
truncates the context to ~25 KB. This means it only reads the preface and the very
beginning of the first chapter. Characters who appear later in the book (or even later
in chapter 1) are completely missed or mischaracterized. This is why characters like
Frost were found in the prologue but omitted for the full book.

### Fix

Refactor `CharacterAnalyzer.analyze()` to process the book chapter-by-chapter:

1. For each chapter, send a prompt containing only that chapter's text along with the
   character registry accumulated so far. Ask the LLM to identify any new characters and
   update/refine existing ones.

2. Merge the results into a single global `CharacterRegistry`, deduplicating by
   character ID. When a character is seen in multiple chapters, prefer the more detailed
   description.

3. After all chapters are processed, run a final consolidation pass that asks the LLM to
   look at all discovered characters together and resolve any naming inconsistencies
   (e.g., "Vathi" vs "Vambrakastram" being the same character referred to by different
   names).

4. Characters discovered during Pass 2 (script generation) should be flagged with
   `discovered_in_pass2=True` as currently — but this case should now be much rarer
   since the multi-pass analysis catches almost everyone.

The `analyze()` method signature stays the same (`analyze(book) -> CharacterRegistry`)
so no callers need to change.

---

## Section 5 — App Branding & Launching

### 5.1 Logo and favicon

**File:** `brain/dashboard/frontend/index.html`

Add a `<link rel="icon">` tag pointing to a favicon file. Add the app logo image to the
navbar/header area. The logo and favicon files should be placed in the
`brain/dashboard/frontend/` static assets directory.

### 5.2 Pause button (rename from Stop)

**Files:** `brain/dashboard/frontend/index.html`, `brain/dashboard/frontend/js/app.js`
or `pipeline.js`

The button currently labeled "Stop" should be renamed to "Pause" everywhere — in the
HTML label, the button's `id`, and all JavaScript references. Update the icon to a pause
icon (❚❚) instead of a stop square. Update the tooltip text. The underlying API call and
pipeline behaviour do not change (it already saves state and resumes from where it left
off), only the label is misleading.

### 5.3 Silent launcher script

**New file:** `start_app.pyw`

A Python script (`.pyw` extension runs without a console window on Windows) that:
1. Spawns the brain API server: `uvicorn brain.dashboard.api.main:app --host 0.0.0.0
   --port 8000` in the background
2. Waits ~2 seconds for the server to start
3. Opens the default browser to `http://localhost:8000`

Use `subprocess.Popen` with `creationflags=subprocess.CREATE_NO_WINDOW` so no terminal
appears.

### 5.4 Desktop shortcut creator

**New file:** `create_shortcut.ps1`

A PowerShell script that creates a Windows `.lnk` shortcut on the desktop pointing to
`start_app.pyw` with `pythonw.exe` as the executable, sets the working directory to the
project root, and assigns a custom icon if one is available.

---

## Section 6 — Scheduling Options

### 6.1 Config schema

**File:** `brain/config.yaml`

Add a `schedule:` block:

```yaml
schedule:
  enabled: false
  timezone: "Europe/Bucharest"   # or whatever the user's timezone is
  windows:
    - days: ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
      start: "22:00"
      end: "07:00"
```

Multiple windows can be defined (e.g., different hours on weekdays vs weekends).
The `days` list specifies which days of the week this window applies to.
Start/end times use 24-hour format. A window that crosses midnight (start > end) is
handled correctly by checking `now >= start OR now < end`.

### 6.2 Pipeline enforcement

**File:** `brain/orchestrator/pipeline.py`

Add a `_check_schedule()` method that:
- Reads the `schedule` block from config
- If `enabled: false`, returns immediately (no-op)
- Checks if the current local time falls within any configured working window
- If **outside** all windows, updates the job state to a new `PAUSED_SCHEDULED` status,
  then blocks in a sleep loop (checking every 60 seconds) until the schedule window
  opens again, then returns

Call `_check_schedule(project_id)` at the top of the chapter generation loop (inside
`_run_generation()`) so the pipeline pauses between chapters when outside hours, not
mid-chapter. Also call it at the top of `_run_mastering()` for the same reason.

Add `PAUSED_SCHEDULED` as a new value to `shared/constants.py` `PipelineStage` enum.

### 6.3 Dashboard UI — schedule editor

**Files:** `brain/dashboard/frontend/index.html`, associated JS

Add a "Schedule" section to the settings or sidebar. It should show:
- A toggle to enable/disable scheduling globally
- A list of configured time windows, each showing the days and start/end times
- An "Add window" button that opens a small form (day checkboxes, time pickers)
- A delete button per window
- A "Save" button that posts the updated schedule config to a new API endpoint

**New API endpoint:** `POST /api/schedule` — accepts the schedule config JSON, validates
it, and writes it back to `brain/config.yaml`. The pipeline picks up changes at the next
chapter boundary (no restart needed since it re-reads config in `_check_schedule()`).

Also display `PAUSED_SCHEDULED` status distinctly in the project card (e.g., with a
clock icon and "Paused — waiting for schedule window").

---

## Section 7 — Detailed Progress Tracking

### 7.1 Data model changes

**File:** `shared/models.py`

Add the following fields to `ProjectStatus`:
- `total_chapters: int` — already exists
- `scripted_chapters: list[int]` — chapter numbers that have completed scripting
- `generated_chapters: list[int]` — chapter numbers that have completed TTS generation
- `mastered_chapters: list[int]` — chapter numbers that have completed mastering
- `current_script_chapter: int | None` — chapter currently being scripted
- `current_gen_chapter: int | None` — chapter currently being generated

### 7.2 Pipeline updates

**File:** `brain/orchestrator/pipeline.py`

The pipeline already updates `completed_script_chapters` and `completed_gen_chapters` in
the job queue. Rename these to match the new model field names (`scripted_chapters`,
`generated_chapters`). Also update `completed_master_chapters` → `mastered_chapters`.

Add updates for `current_script_chapter` and `current_gen_chapter` at the start of each
chapter loop so the UI can show "Chapter 5/12 — In Progress".

### 7.3 Dashboard UI — chapter progress grid

**File:** `brain/dashboard/frontend/index.html` and JS

In the project detail view, add a chapter progress grid below the existing progress bar.
Show one cell per chapter with a visual status indicator:

- ⬜ **Pending** — not yet started
- 🟡 **Scripting** — currently being scripted (LLM pass)
- 🟢 **Scripted** — scripting complete, waiting for generation
- 🔵 **Generating** — TTS in progress
- 🟣 **Mastering** — audio mastering in progress
- ✅ **Complete** — fully done

The grid should update live via the existing WebSocket or polling mechanism. Hovering
over a cell can show the chapter title.

---

## Section 8 — Book Metadata & Cover Artwork

### 8.1 Metadata fetcher utility

**New file:** `brain/extractor/metadata_fetcher.py`

A utility class `MetadataFetcher` with a method `fetch(title, author)` that:
1. Queries the Google Books API:
   `https://www.googleapis.com/books/v1/volumes?q=intitle:{title}+inauthor:{author}`
   No API key required.
2. Finds the best-matching result (check `title` and `authors` fields).
3. Downloads the `thumbnail` image URL from `volumeInfo.imageLinks`. Try to get the
   largest available size by replacing `zoom=1` with `zoom=3` or `fife=w800` in the URL.
4. Returns a `BookMetadata` dataclass with: `title`, `author`, `description`, `isbn`,
   `cover_image_bytes`.

### 8.2 API endpoint

**File:** `brain/dashboard/api/main.py`

Add endpoint `POST /api/projects/{project_id}/fetch-metadata` that:
1. Loads the project's `book.json` to get the title and author
2. Calls `MetadataFetcher.fetch(title, author)`
3. Saves the cover image as `brain/projects/{project_id}/cover.jpg`
4. Updates `book.json` with the fetched description and cover path
5. Returns the updated metadata as JSON

Also call this automatically during project creation if no cover was found in the EPUB
itself.

### 8.3 Embed cover in M4B

**File:** `voice/mastering/m4b_exporter.py`

When building the ffmpeg command for M4B export, check if a `cover.jpg` exists in the
project directory. If so, add it to the ffmpeg command:

```
ffmpeg -i audio_input.wav -i cover.jpg
       -map 0:a -map 1:v
       -c:a aac -b:a 128k
       -c:v copy
       -disposition:v:0 attached_pic
       -metadata:s:v title="Album cover"
       output.m4b
```

---

## Section 9 — Deferred Deployments (Safe Points)

### 9.1 Job state flag

**File:** `brain/orchestrator/job_queue.py` (or wherever job state fields are defined)

Add a `deployment_requested: bool` field to the job state, defaulting to `False`.

### 9.2 Pipeline check

**File:** `brain/orchestrator/pipeline.py`

At the end of each chapter loop (after generation and mastering complete for a chapter),
check if `deployment_requested` is `True` in the job state. If so:
1. Update the status to a new `DEPLOY_PAUSED` stage
2. Fire an HTML5 desktop notification (see 9.3)
3. Block in a loop until the flag is cleared (or until the pipeline is manually resumed)

Add `DEPLOY_PAUSED` to `shared/constants.py` `PipelineStage` enum.

### 9.3 Desktop notification

Use the `win10toast` or `plyer` Python library to send a native Windows notification
from the pipeline thread when parking at a deploy safe point. The notification should
say something like "Audiobook Creator — Safe to deploy. Pipeline is parked after chapter
N. Click Resume when ready."

Alternatively, the frontend JS can use `new Notification(...)` (the Web Notifications
API) when it detects the `DEPLOY_PAUSED` status in a WebSocket update. This requires
the user to grant notification permission in the browser once.

### 9.4 Dashboard UI

**File:** `brain/dashboard/frontend/index.html` and JS

Add a "Request Deploy Pause" button to the project detail view (visible only when a
pipeline is running). When clicked, it calls `POST /api/projects/{id}/request-deploy`
which sets `deployment_requested = True` in the job state.

Add a "Resume" button that appears when the project status is `DEPLOY_PAUSED`. When
clicked, it calls `POST /api/projects/{id}/resume-deploy` which clears
`deployment_requested` and resumes the pipeline.

Display `DEPLOY_PAUSED` status distinctly (e.g., amber/orange colour with "Parked —
safe to deploy" text).

---

## Section 10 — Stop Shows Project as Running (Bug Fix)

### Problem

When the user clicks the Stop/Pause button, the API calls `pipeline.stop(project_id)`
which sets an internal `_stop_flags[project_id] = True`. The pipeline thread then checks
this flag at the next `_check_stop()` call (which may be several seconds away if a
chapter is mid-generation). During that gap, the UI still shows the project as
`RUNNING`, and if the user refreshes, they see the wrong state.

### Fix

**File:** `brain/dashboard/api/main.py`

In the stop/pause API endpoint handler, immediately write `PAUSED` status to the job
queue database **before** calling `pipeline.stop()`. This way, any UI poll or refresh
will immediately see the correct state. The pipeline thread will then also write `PAUSED`
when it actually handles the stop, which is a harmless overwrite.

```python
# In the /api/projects/{id}/stop endpoint:
job_queue.update_job(project_id, {"status": PipelineStage.PAUSED})  # immediate UI update
pipeline.stop(project_id)  # signal the background thread
```

---

## Section 11 — Voice Server Setup on Windows (Parler Integration)

### Current architecture (to be changed)

The Ubuntu `voice_designer.py` boots a Parler microservice as a subprocess on
`127.0.0.1:8101` and calls it via HTTP. The microservice expects to write output files
to a local Ubuntu path (`/home/crazywiz/crazy-audiobook-creator/voice_library/...`).

### New architecture

The Parler server now runs on the Windows machine using the AMD venv. The output path
is a local Windows path under the project's `voice_library` directory.

**File:** `parler_server.py` (root level, deployed to Ubuntu originally, now runs on
Windows)

Update the Parler server to:
1. Cast the generated audio array to `float32` before writing (soundfile requires this —
   the model outputs float16 when loaded with `torch.float16`)
2. Accept `output_path` as a Windows-compatible absolute or relative path

**File:** `voice/tts_server/voice_designer.py`

Update the subprocess launch command to use the AMD venv Python:
```python
subprocess.Popen([
    r"E:\PyTorch env\my_venv\Scripts\python.exe",
    str(Path(__file__).parent.parent.parent / "parler_server.py")
], ...)
```

Update the log file path from the hardcoded Ubuntu path to a Windows path under the
project's logs directory.

Change the Parler model reference from `parler-tts-mini-v1` to `parler-tts-large-v1`.

---

## Section 12 — Selective Chapter Generation

### Motivation

Users want to run the pipeline over a subset of chapters at a time rather than the full
book in one go. Use cases:

- **Validation run:** Generate just chapter 1 to confirm voice quality, timing, and
  pipeline correctness before committing to a full run.
- **Early listening:** Export the first few chapters as a partial M4B to start listening
  while the rest is still being generated.
- **Predictable completion windows:** Process the book in manageable batches (e.g.,
  5 chapters per night) to know when each batch will finish.
- **Selective re-generation:** Re-generate only specific chapters if voice quality was
  poor or the script was wrong, without touching chapters that are already fine.

### 12.1 Chapter selection state in the job

**File:** `shared/models.py` / job queue

Add a `generation_chapter_selection: list[int] | None` field to `ProjectStatus` and the
job state. When `None` (the default), the pipeline generates all chapters as today.
When set to a list of chapter numbers, the pipeline's generation and mastering stages
skip any chapter not in the list.

This field is set by the user via the dashboard before starting (or resuming) a run, and
cleared once all selected chapters are complete. It is persisted in the SQLite job state
so it survives restarts.

Scripting (Pass 1 + Pass 2) always runs for all chapters regardless of this selection —
the full script is needed for character consistency and summaries. Only the TTS
generation, validation, mastering, and partial export stages are filtered.

### 12.2 Pipeline enforcement

**File:** `brain/orchestrator/pipeline.py`

In `_run_generation()`, at the top of the chapter loop, check:

```python
selection = state.get("generation_chapter_selection")
if selection is not None and chapter_script.chapter_number not in selection:
    logger.info("Skipping chapter %d (not in current selection)", chapter_number)
    continue
```

Apply the same check at the top of the `_run_mastering()` chapter loop.

After all selected chapters finish generation and mastering, do **not** automatically
proceed to the full M4B export stage. Instead, transition to a new
`SELECTION_COMPLETE` status and trigger a partial M4B export covering only the
chapters that have been mastered so far (see 12.4). The user can then kick off another
run with the next batch of chapters.

If `generation_chapter_selection` is `None`, the pipeline behaves exactly as before —
all chapters are generated and a full M4B is exported at the end.

Add `SELECTION_COMPLETE` to `shared/constants.py` `PipelineStage` enum.

### 12.3 Dashboard UI — chapter selector

**File:** `brain/dashboard/frontend/index.html` and JS

After scripting is complete (status = `GENERATING` or `SCRIPTING` done), show a chapter
selection panel in the project detail view. The panel contains:

- A list of all chapters with their title and word count, each with a checkbox
- "Select All" and "Select None" buttons
- A "Select range" quick-input (e.g., type "1-5" to check chapters 1 through 5)
- The current status of each chapter (Not started / Generated / Mastered) shown next to
  the checkbox so the user can see at a glance what's already done
- A "Start Generation" button (or "Resume with selection" if the pipeline is paused)
  that sends the selection to the API and starts the pipeline

When no chapters are explicitly selected, all are included (same as today). The
selection is visually distinct from the chapter progress grid — the progress grid shows
*what has been done*, the selector controls *what will be done next*.

If a pipeline run is already in progress, the selector is read-only and shows which
chapters are in the current batch.

### 12.4 Partial M4B export

**File:** `brain/orchestrator/pipeline.py` and `voice/mastering/m4b_exporter.py`

When the pipeline completes a selection batch (status → `SELECTION_COMPLETE`), call
a partial export that:
1. Gathers only the mastered chapter WAV files that exist
2. Builds an M4B that contains only those chapters, with correct chapter markers
3. Names it `{project_id}_chapters_{first}-{last}.m4b` (e.g., `my-book_chapters_1-5.m4b`)
4. Places it in the project directory alongside any previous partial exports

When the user later processes more chapters and exports again, the new partial M4B
contains all mastered chapters (including previously mastered ones), not just the latest
batch. This means each partial export is a complete, playable audiobook up to that point.

The final full export (when all chapters are mastered) produces `{project_id}.m4b` as
today.

Add an API endpoint `POST /api/projects/{id}/export-partial` that triggers this on
demand — the user can also manually export at any point without waiting for a batch to
complete.

### 12.5 API endpoints

Add the following endpoints:

- `POST /api/projects/{id}/set-selection` — accepts `{"chapters": [1, 2, 3]}` (or
  `{"chapters": null}` to reset to all chapters). Writes to job state. Can be called
  when the pipeline is stopped/paused.

- `POST /api/projects/{id}/export-partial` — triggers an immediate partial M4B export
  using all currently mastered chapters. Returns the output file path.

---

## Verification Plan


After implementation, run the following checks before closing out this plan:

1. **Single-machine smoke test:** Start a new pipeline run from scratch on a small EPUB.
   Confirm the voice server subprocess starts automatically, the GPU is detected (check
   logs for "CUDA available" and "AMD Radeon RX 7900 XTX"), and the pipeline completes
   end-to-end without any network errors.

2. **Multi-speaker voice check:** Listen to the generated voice samples for each
   character in the voice library. Confirm they are audibly distinct from each other.
   Check the logs for "Full ICL mode" messages (not "x_vector_only fallback").

3. **Whisper large-v3 confirmation:** Check the validation log lines — they should
   reference `large-v3`, not `medium`.

4. **Character coverage:** Run the multi-pass character analyzer on a full-length book
   and confirm all named characters across all chapters appear in `characters.json`.

5. **Scheduling:** Set a working-hours window that excludes the current time. Start a
   pipeline run. Confirm it pauses with status `PAUSED_SCHEDULED`. Advance the clock
   (or change the window) and confirm it resumes automatically.

6. **Chapter progress grid:** Start a run and watch the dashboard. Confirm chapter cells
   update from Pending → Scripting → Scripted → Generating → Complete in real time.

7. **Deploy safe point:** Click "Request Deploy Pause" during a run. Confirm the
   pipeline parks at the next chapter boundary and a notification fires.

8. **Stop button immediate update:** Click Pause. Confirm the UI shows PAUSED immediately
   (within 1–2 seconds), not after the current generation chunk finishes.

9. **M4B cover art:** Confirm the final `.m4b` file has embedded cover art visible in
   a media player.

10. **Desktop shortcut:** Run `create_shortcut.ps1`. Confirm the shortcut launches the
    app without a terminal window appearing.

11. **Chapter selection:** After scripting a book, select only chapter 1 and start the
    pipeline. Confirm only chapter 1 is generated and mastered. Export a partial M4B
    containing chapter 1 only. Then select chapters 2–4 and run again. Confirm the
    pipeline picks up where it left off, only processes the newly selected chapters,
    and the partial M4B now contains chapters 1–4.
