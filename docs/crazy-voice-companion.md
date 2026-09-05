# CrazyVoice Mobile Companion

**Status:** Reference — Describes current behaviour. Keep it accurate when the code changes.

**CrazyVoice** is the dedicated Android companion app for **Crazy Audiobook Creator**. Built on the modern AndroidX Media3 and Jetpack Compose architecture, it connects your phone directly to your 24/7 NAS streaming endpoint or Creator workstation.

---

## Core Capabilities

### 1. Transparent 24/7 Byte-Range M4B/AAC Streaming
- **Zero-Latency Playback**: Streams chapters and delivery parts on-demand using HTTP 206 Partial Content range requests.
- **24/7 NAS Streamer Endpoint**: Streams from the isolated container `crazy-bookplayer-streamer` at `https://crazyha.mywire.org/bookplayer/` mounting `/mnt/nas/media/crazybooks` directly. Your phone can stream and seek 24/7 even when the Creator PC is asleep.
- **Dynamic HTTP Authentication (`PlaybackModule.kt`)**: The streaming media source wraps `DefaultHttpDataSource` in a dynamic factory that strips embedded credentials from URIs (preventing Android `HttpURLConnection` crashes) and injects active `Authorization: Basic <base64>` headers dynamically into every HTTP request, including byte-range scrubbing requests.
- **Live Incremental Delivery**: Listen to completed delivery batches (*Part 01, Part 02, Part 03*) while subsequent chapters are actively synthesizing on the GPU pipeline.

### 2. Synchronized Karaoke Lyrics / Script Viewer
- **Real-Time Spoken Line Highlight**: Displays script lines in real time, highlighting the current spoken line with primary container styling while gracefully fading past and future lines.
- **Character Speaker Badges & Emotion Tags**: Shows the speaker's name badge with personalized palette tints alongside emotional intent tags (*"whispering"*, *"urgent"*, *"reflective"*).
- **Smart Auto-Centering & Drag Detection**: Automatically scrolls to center the currently spoken dialogue. Touching or dragging the list gracefully pauses auto-scroll, surfacing a floating **"Sync to Audio"** button to jump back into lockstep at any moment.
- **Tap-to-Seek**: Tapping any dialogue or narration line immediately seeks ExoPlayer to that exact line's millisecond boundary (`start_ms`).

### 3. Synchronized Ebook Reader
- **Original Book Manuscript Typography**: Renders the authentic book text partitioned cleanly into original prose paragraphs extracted from the source EPUB.
- **Paragraph-Level Audio Synchronization**: Highlights the actively narrated paragraph with a soft accent tint and a prominent margin indicator bar.
- **Auto-Following Narration**: The reader scrolls smoothly to follow audio narration down the chapter. Dragging the text pauses following, surfacing a floating **"Follow Audio"** pill.
- **Tap-to-Listen ("Play from here")**: Tapping any paragraph seeks audio playback directly to that paragraph's start timestamp.
- **Customizable Typography & Theming**:
  - Font scaling: quick `A-` / `A+` controls to dynamically adjust font size from `12sp` to `32sp`.
  - 4 Reading Themes: **Light**, **Sepia** (warm paper), **Dark**, and **OLED** (pure black for battery saving).

### 4. Player View Mode Switcher
- In the player view for Crazy Audiobooks, a Material 3 segmented pill switcher (`[ Cover | Script | Reader ]`) appears right above the chapter title and docked player controls.
- Seamlessly toggle between visual cover art, line-by-line script, and full ebook reading view without interrupting audio playback.

### 5. Pure Book Manuscript Chapter Titles & Accurate Timestamps
- Displays genuine book chapter headings (*"Prologue"*, *"Chapter Thirteen"*, *"Chapter Fourteen"*, *"Chapter Twenty-Three"*) directly matching the author's manuscript without artificial duplicate numbering.
- **NAS Syncer Batch Timing (`nas_syncer.py`)**: Computes exact chapter audio durations from workspace WAVs and calculates clean 0-based relative millisecond offsets (`start_ms`, `end_ms`) for each chapter in a delivery part.
- **Superseded Part Pruning**: The syncer automatically prunes obsolete revisions (e.g. `Part 01...-r1.m4b` replaced by `-r2.m4b`) from the NAS storage to prevent ghost duplicate entries.

### 6. Incremental Offline Downloads with Pre-Cached Reading
- **Download for Offline Playback**: Long-press any book in the library and tap **"Download for Offline Playback"** (icon `VoiceIcons.Download`).
- **Complete Offline Experience**: When downloading an audiobook offline, the app fetches the audio parts AND automatically pre-caches **all chapter scripts and reader texts** into local app storage (`crazy_scripts/` and `crazy_reader/`).
- **Resilient Offline Fallback**: If an offline user attempts to load or refresh, the app gracefully falls back to the local disk cache with zero errors.
- **Connection Restoration & Resync**:
  - If a chapter was opened offline without prior cache, the screen displays an informative state with a **"Retry Sync"** button.
  - Switching tabs (`Cover` → `Script` / `Reader`) automatically checks for connection restoration and syncs missing data from the server.
- **Update Workflow for New Chapters**: When new parts are generated by the pipeline, tapping **"Download for Offline Playback"** automatically fetches **only the newly generated parts** without re-downloading earlier parts.
- **Delete Offline Audio**: Long-press a downloaded book and tap **"Delete Downloaded Offline Audio"** to free device space and switch back to 24/7 streaming.
- **Wi-Fi Only Protection**: User-configurable toggle in Settings prevents accidental cellular data consumption.

### 7. Android Auto & In-Car Integration
- **Full Media3 MediaLibraryService**: Browsable library trees, chapter mark skip controls, and scrubbable timeline on vehicle head units.
- **Inline Artwork Delivery (`artworkData`)**: Attached directly via `MediaMetadata.Builder.setArtworkData()`, delivering crisp cover art across the MediaSession IPC binder directly to Android Auto cards without encountering cross-process `FileProvider` permission blocks.
- **Extended Projection Permissions**: `ImageFileProvider` pre-authorizes all standard Android Auto and Automotive projection hosts (`com.google.android.projection.gearhead`, `com.google.android.gms`, `com.android.bluetooth`, `com.google.android.apps.automotive.templates.host`).
- **Robust Path Mapping**: `cover_paths.xml` declares `path="."` and `path="crazy_covers"` for full FileProvider compatibility.

### 8. 24/7 Two-Way Progress Synchronization
- Persists chapter number, millisecond offset, playback speed, and completion status to `/api/mobile/v1/books/{id}/progress` in real time.
- Backed by `/mnt/nas/media/crazybooks/{projectId}/progress.json` on the NAS for continuous 24/7 availability across devices.
- Works seamlessly across both streaming and offline playback modes.

---

## APK Distribution Endpoints & Locations

The compiled Android application (`Voice-CrazyAudiobook-debug.apk`) is continuously published to the following endpoints:

| Location | Description | Access / URL |
| :--- | :--- | :--- |
| **24/7 NAS Remote Streamer** | Primary 24/7 remote download (always online) | `https://<username>:<password>@crazyha.mywire.org/bookplayer/Voice-CrazyAudiobook-debug.apk` <br>*(or browse to `https://crazyha.mywire.org/bookplayer/Voice-CrazyAudiobook-debug.apk` and enter basic auth credentials)* |
| **Creator PC Direct** | Workstation direct download endpoint | `http://192.168.50.44:8000/api/mobile/v1/app` |
| **Local Project Root** | Copied to Creator repository root | `e:\Projects\crazy-audiobook-creator\Voice-CrazyAudiobook-debug.apk` |
| **Android Build Output** | Compiled from Voice repository | `E:\Projects\Voice\app\build\outputs\apk\free\debug\app-free-debug.apk` |

---

## Connection Configuration

1. Open **CrazyVoice** on your Android device.
2. Navigate to **Settings** -> **Crazy Audiobook Creator Server**.
3. Set your server endpoint:
   - **24/7 NAS BookPlayer Streaming**: `https://<username>:<password>@crazyha.mywire.org/bookplayer`
   - **Creator PC Direct**: `https://crazyha.mywire.org/audiobook` or local `http://192.168.50.44:8000`
4. Tap **"Test Connection"** to verify server reachability.
5. Tap **"Sync Audiobooks"** (or pull down to refresh on Library) to synchronize all available books, chapters, covers, and progress.

## Source Repository

- **GitHub**: [https://github.com/NicusorFlorinBaluta/crazy-audiobook-player.git](https://github.com/NicusorFlorinBaluta/crazy-audiobook-player.git)

