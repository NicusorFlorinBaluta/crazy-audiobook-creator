# CrazyVoice Mobile Companion

**CrazyVoice** is the dedicated Android companion app for **Crazy Audiobook Creator**. Built on the modern AndroidX Media3 and Jetpack Compose architecture, it connects your phone directly to your audiobook creator workstation or server over LAN, Tailscale, or domain reverse proxy.

---

## Core Capabilities

### 1. Transparent Byte-Range AAC Streaming
- **Zero-Latency Playback**: Streams chapters on-demand using HTTP 206 Partial Content range requests.
- **Fast-Start AAC Transcoding**: High-resolution mastered WAV files on the host are automatically transcoded on the fly into 128 kbps AAC streams with ADTS fast-start headers. This eliminates multi-megabyte buffer stalls over cellular or Wi-Fi while maintaining studio acoustic fidelity.
- **Live Incremental Delivery**: Listen to completed chapters while subsequent chapters are actively synthesizing on the GPU pipeline.

### 2. Dual Library Management (Cloud & Local)
- **Visual Separation**: Clearly separates `Crazy Audiobooks` (API/Cloud streaming) from `Local Audiobooks` (on-device files).
- **Live Status Indicators**: Displays `⚡ IN PRODUCTION`, `☁️ STREAM`, and `📥 OFFLINE` status badges.
- **Unclipped Cover Display**: Book covers scale using centered aspect-ratio fitting with smooth rounded borders across both list and player screens.

### 3. Stable Chapter Numbering
- The pipeline sequence number is the chapter identity used by the server,
  mobile player, dashboard, partial deliveries, and M4B chapter marks.
- User-facing titles are always `Chapter {number}`. EPUB headings are preserved
  separately as `source_heading`/`raw_title`; they never create contradictory
  labels such as `Chapter 8: Chapter Seven`.

### 4. Two-Way Progress Synchronization
- Persists chapter number, millisecond offset, playback speed, and completion status to `/api/mobile/v1/books/{id}/progress` in real time.
- Allows seamless switching between the web dashboard audio player and mobile playback without losing your place.

### 5. Offline Background Downloader
- Download mastered chapters or delivery batches directly to device storage (`crazy_downloads/`).
- **Wi-Fi Only Protection**: User-configurable toggle in Settings prevents accidental cellular data consumption.

### 6. Android Auto & In-Car Integration
- Full `MediaLibraryService` integration provides browsable menus, chapter skip controls, and rich metadata on vehicle head units.
- Chapter titles and book artwork pass through to Android Auto without vertical clipping.

---

## Deletion & Sync Lifecycle

### Deleting a Project from the Creator Dashboard
1. When a project is deleted in the web dashboard via `DELETE /api/projects/{id}`:
   - The server unloads any active GPU tasks or locks.
   - Database records in `pipeline_state.db` / `jobs.db` are removed.
   - Directory trees in `brain/projects/{id}` and `workspace/{id}` are recursively removed using `_safe_delete_tree` (which clears Windows file permission locks).
2. The `/api/mobile/v1/catalog` feed automatically omits the deleted project and excludes any orphaned directories or empty queued jobs.

### Synchronizing Deletions to the Mobile App
1. When the user taps **Sync Audiobooks** in CrazyVoice:
   - `CrazyBookSyncService.syncCatalog()` fetches the active catalog from the server.
   - It queries all local Room database entries where `id` starts with `crazy://`.
   - Any local remote book whose `remoteProjectId` is no longer present in the server's catalog is automatically marked inactive (`isActive = false`) and removed from the library view.

### Deleting a Book on the Android Device
1. When a user deletes a `crazy://` book from the app's library menu:
   - `DeleteBookViewModel` deactivates the book entry in the local Room database.
   - Any cached offline audio files in `crazy_downloads/{project_id}` and cached cover artwork in `crazy_covers/{project_id}.jpg` are deleted from disk.

---

## Connection Configuration

1. Open **CrazyVoice** on your Android device.
2. Navigate to **Settings** -> **Crazy Audiobook Creator Server**.
3. Enter your server connection address:
   - **Local LAN**: `http://192.168.x.x:8000`
   - **Tailscale VPN**: `http://100.x.x.x:8000`
   - **Domain / Reverse Proxy**: use HTTPS and configure the dashboard API token
     as `X-API-Token` in the companion client or trusted reverse proxy.
   - Only `/api/mobile/v1/server-info` is public. Catalogs, covers, streams,
     downloads, logs, and progress synchronization follow the dashboard's
     LAN/token authorization boundary.
4. Tap **"Test Connection"** to verify server reachability and API version compatibility.
5. Tap **"Sync Audiobooks"** to synchronize all available books, chapters, and progress.
