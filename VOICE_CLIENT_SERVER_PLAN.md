# Crazy Audiobook Creator — Mobile Client & Streaming Integration Plan

**Status:** Historical planning record — not a specification. Kept for context; some sections describe modules and deployments that were never built or have since been replaced. Current behavior is defined by [README.md](README.md), [docs/](docs/README.md), the models, and the executable tests.

## 1. Executive Summary

This document specifies the server-side architectural changes, API endpoints, streaming protocols, and discovery mechanisms in **Crazy Audiobook Creator** to support client integration with the **Voice Audiobook Player** (Android).

---

## 2. Server Architecture Overview

Crazy Audiobook Creator's Brain service (`brain/dashboard/api/main.py`) will be extended with:
1. **Dedicated Mobile API Router (`/api/mobile/v1/*`)**: Lightweight JSON endpoints for catalog discovery, chapter manifests, and progress synchronization.
2. **HTTP 206 Byte-Range Streaming (`/api/projects/{id}/stream` & `/api/projects/{id}/download/chapter/{num}`)**: Verified partial content streaming for full `.m4b` files and in-progress chapter `.wav` files.
3. **Local & Remote Connectivity**:
   - **Local Wi-Fi**: Zeroconf / mDNS service advertisement (`_crazy-audiobook._tcp`).
   - **Remote Access**: Seamless support for Tailscale mesh networks and `crazyha` subdomain reverse proxies (with standard `X-Forwarded-Proto: https` and `X-Forwarded-For` compatibility).
4. **Listening Progress Persistence**: Lightweight SQLite / JSON store for syncing playback position.

```mermaid
flowchart TD
    subgraph Clients [Clients & Networks]
        WiFi["Local Wi-Fi Client"]
        Tailscale["Tailscale VPN Client"]
        Internet["Remote Internet (crazyha subdomain / HTTPS)"]
    end

    subgraph Server [Crazy Audiobook Creator (FastAPI :8000)]
        mDNS["Zeroconf Broadcaster<br/>(_crazy-audiobook._tcp)"]
        ReverseProxy["Reverse Proxy / HTTPS Adapter<br/>(crazyha domain)"]
        MobileRouter["Mobile API Router<br/>(/api/mobile/v1/*)"]
        StreamHandler["Audio Stream Handler<br/>(HTTP 206 Byte Ranges)"]
        ProgressStore["Progress Sync Store<br/>(pipeline_state.db)"]
        DeliveryMgr["DeliveryManager & M4B Exporter"]
    end

    WiFi -->|Auto-discover| mDNS
    WiFi -->|Catalog & Stream| MobileRouter
    Tailscale -->|Direct IP / MagicDNS| MobileRouter
    Internet -->|HTTPS Reverse Proxy| ReverseProxy --> MobileRouter

    MobileRouter --> StreamHandler
    MobileRouter --> ProgressStore
    MobileRouter --> DeliveryMgr
```

---

## 3. Dedicated Mobile API Specification (`/api/mobile/v1/*`)

### 3.1 Server Information
**`GET /api/mobile/v1/server-info`**
Returns server status, identity, and capabilities.
- **Response**:
```json
{
  "server_name": "Crazy Audiobook Creator",
  "version": "2.0.0",
  "capabilities": {
    "streaming": true,
    "byte_ranges": true,
    "wav_chapter_streaming": true,
    "incremental_delivery": true,
    "progress_sync": true
  },
  "active_project_id": "stormlight_ch1",
  "active_stage": "generating"
}
```

### 3.2 Mobile Catalog Feed
**`GET /api/mobile/v1/catalog`**
Returns a mobile-optimized list of audiobooks (ready full books and in-progress projects).
- **Response**:
```json
{
  "books": [
    {
      "project_id": "the_way_of_kings",
      "title": "The Way of Kings",
      "author": "Brandon Sanderson",
      "genre": "Epic Fantasy",
      "year": "2010",
      "description": "Roshar is a world of stone and storms...",
      "isbn": "9780765326355",
      "status": "ready_full",
      "total_chapters": 75,
      "generated_chapters": 75,
      "mastered_chapters": 75,
      "total_duration_seconds": 162400.0,
      "cover_url": "api/projects/the_way_of_kings/cover",
      "stream_url": "api/projects/the_way_of_kings/stream",
      "download_url": "api/projects/the_way_of_kings/download",
      "file_size_bytes": 1073741824,
      "updated_at": "2026-08-19T20:00:00Z"
    },
    {
      "project_id": "words_of_radiance",
      "title": "Words of Radiance",
      "author": "Brandon Sanderson",
      "status": "in_progress",
      "total_chapters": 89,
      "generated_chapters": 24,
      "mastered_chapters": 20,
      "total_duration_seconds": 43200.0,
      "cover_url": "api/projects/words_of_radiance/cover",
      "stream_url": "api/projects/words_of_radiance/stream",
      "download_url": "api/projects/words_of_radiance/download",
      "updated_at": "2026-08-19T22:30:00Z"
    }
  ]
}
```

### 3.3 Book Details & Chapter Manifest
**`GET /api/mobile/v1/books/{project_id}`**
Returns chapter list with per-chapter `.wav` stream URLs.
- **Response**:
```json
{
  "project_id": "words_of_radiance",
  "title": "Words of Radiance",
  "author": "Brandon Sanderson",
  "status": "in_progress",
  "total_chapters": 89,
  "mastered_chapters_count": 20,
  "cover_url": "api/projects/words_of_radiance/cover",
  "stream_url": "api/projects/words_of_radiance/stream",
  "download_url": "api/projects/words_of_radiance/download",
  "chapters": [
    {
      "number": 1,
      "title": "Prologue: To Question",
      "status": "mastered",
      "duration_seconds": 2145.5,
      "stream_url": "api/projects/words_of_radiance/download/chapter/1",
      "download_url": "api/projects/words_of_radiance/download/chapter/1"
    },
    {
      "number": 2,
      "title": "Chapter 1: Shadows",
      "status": "mastered",
      "duration_seconds": 1820.0,
      "stream_url": "api/projects/words_of_radiance/download/chapter/2",
      "download_url": "api/projects/words_of_radiance/download/chapter/2"
    },
    {
      "number": 21,
      "title": "Chapter 20: The Silent Gathering",
      "status": "generating",
      "duration_seconds": null,
      "stream_url": null,
      "download_url": null
    }
  ]
}
```

### 3.4 Playback Progress Synchronization
**`POST /api/mobile/v1/books/{project_id}/progress`**
Persists user's listening progress.
- **Request Body**:
```json
{
  "client_id": "voice_android",
  "chapter_number": 4,
  "position_ms": 124500,
  "playback_speed": 1.25,
  "is_completed": false,
  "timestamp": "2026-08-19T22:38:00Z"
}
```

**`GET /api/mobile/v1/books/{project_id}/progress`**
Returns stored progress.

---

## 4. Streaming Performance & Range Requests

### 4.1 Full M4B Stream (`GET /api/projects/{project_id}/stream`)
- Serves full or latest partial `.m4b` with:
  - `Content-Type: audio/mp4`
  - `Content-Disposition: inline`
  - `Accept-Ranges: bytes`
  - Full **HTTP 206 Partial Content** support for byte-range seeking.

### 4.2 Chapter Audio Streaming (`GET /api/projects/{project_id}/download/chapter/{number}`)
- Serves mastered chapter `.wav` files with byte-range support:
  - `Content-Type: audio/wav`
  - `Accept-Ranges: bytes`

---

## 5. Network Connectivity & Remote Access

1. **Local Wi-Fi Discovery (Zeroconf / mDNS)**:
   - Registers service `_crazy-audiobook._tcp.local.` on port 8000.
2. **Tailscale & VPN Access**:
   - The dashboard binds to `0.0.0.0` or configured host to be reachable via Tailscale IP/MagicDNS.
3. **Internet / Subdomain Access (`crazyha`)**:
   - Compatible with HTTPS reverse proxies (Caddy, Nginx, Cloudflare Tunnel, or Home Assistant proxy).
   - Handles `X-Forwarded-Proto`, `X-Forwarded-Host`, and `X-Forwarded-For` gracefully.

---

## 6. Implementation Plan & File Modifications

1. **[NEW] `brain/dashboard/api/mobile.py`**:
   FastAPI router for `/api/mobile/v1/*` endpoints.
2. **[NEW] `brain/dashboard/api/progress_store.py`**:
   Persistent progress store in `pipeline_state.db`.
3. **[NEW] `brain/dashboard/api/discovery.py`**:
   mDNS service registration for zero-config local discovery.
4. **[MODIFY] `brain/dashboard/api/main.py`**:
   Mount mobile router, add `/stream` route, and integrate mDNS lifecycle hooks.
5. **[NEW] `tests/test_mobile_api.py`**:
   Automated unit tests for mobile routes and HTTP range responses.
