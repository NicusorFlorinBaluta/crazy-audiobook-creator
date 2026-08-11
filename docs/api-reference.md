# API Reference

The application has two local FastAPI services:

- Dashboard/Brain: `http://127.0.0.1:8000`
- Voice: `http://127.0.0.1:8100`

Loopback and `dashboard.trusted_lan_cidrs` peers require no application token.
Other peers must send the configured `X-API-Token`; WebSockets may use
`?token=<value>`. Forwarded headers never grant LAN trust. The Voice `/health`
route remains unauthenticated for readiness checks.

FastAPI’s generated OpenAPI schema at `/docs` is the definitive field-level reference for a running version.

## Dashboard API

### Projects

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/projects` | List project states |
| `POST` | `/api/projects` | Upload an EPUB as multipart field `file` |
| `GET` | `/api/projects/{project_id}` | Get stored state |
| `DELETE` | `/api/projects/{project_id}` | Delete stopped project state, artifacts, and voice references |
| `GET` | `/api/projects/{project_id}/status` | State plus per-chapter progress |

Uploads must have an `.epub` suffix, remain under the configured compressed/expanded limits, and pass ZIP traversal/compression checks.

### Pipeline control

| Method | Route | Purpose |
|---|---|---|
| `POST` | `/api/projects/{project_id}/start` | Start/resume the project |
| `POST` | `/api/projects/{project_id}/stop` | Immediately interrupt work and release app-owned services |
| `POST` | `/api/projects/{project_id}/reset` | Reset to extracting, scripting, bootstrapping, voice_review, generating, validating, mastering, or exporting |
| `POST` | `/api/projects/{project_id}/voice-review/approve` | Approve speaking voice cast and continue audio generation |
| `POST` | `/api/projects/{project_id}/request-deploy` | Park at the next chapter boundary |
| `POST` | `/api/projects/{project_id}/resume-deploy` | Release deployment parking |
| `POST` | `/api/schedule` | Replace the schedule section |
| `GET` | `/api/schedule` | Get working hours plus whether a window is open now |
| `GET` | `/api/voice/health` | Report actual on-demand Voice state without starting it |

Only one project pipeline may run at once. `stop` closes an active Ollama
stream, requests Voice cancellation, terminates app-owned model services, and
waits briefly for the worker to release the global lease. Starting another
project performs the same interruption first. If lease release cannot be
confirmed, the new run is refused instead of running concurrently.

`POST /api/system/release-gpu` pauses active work, waits briefly for workers to
exit, and unloads app-managed Ollama and Voice models. The Electron wrapper
calls it before terminating the dashboard process.

#### Reset to Stage (`POST /api/projects/{project_id}/reset`)
Accepts `{"stage": "<stage_name>"}` where `<stage_name>` is one of 8 supported stages:
- **`extracting`**: Rebuilds `book.json` from the preserved project `source.epub`, then clears downstream scripts, cast, segments, and masters. Legacy projects without a preserved source return `409` without deleting `book.json`.
- **`scripting`**: Clears script JSONs and character cast, keeping extracted EPUB text. Forces fresh LLM character analysis.
- **`bootstrapping`**: Clears `voice_cast.json`, marks reference generation as forced, and regenerates voice profiles on resume.
- **`voice_review`**: Resets `voice_review_status` to `"pending"` and sets `active_stage = voice_review`, re-opening the Voice Review banner in the UI.
- **`generating`**: Clears generated segment WAV files and resets `mastered_chapters = []`. Keeps script and voice cast intact.
- **`validating`**: Preserves segment WAVs, changes the validation revision, removes segment/master manifests, and re-runs Whisper/speaker/audio checks without re-synthesizing unchanged lines.
- **`mastering`**: Clears `mastered/` WAV files and `.m4b` export. Re-runs ffmpeg/sox chapter mastering.
- **`exporting`**: Removes `.m4b` export file to re-run M4B packaging.

### Chapter selection

```http
POST /api/projects/{project_id}/set-selection
Content-Type: application/json

{"chapters": [1, 2, 5]}
```

- A nonempty array is a partial audio batch.
- `{"chapters": null}` selects all chapters and permits a full export.
- `[]`, zero/negative values, and out-of-range chapters return `422`.
- Input is sorted and deduplicated.

Analysis and scripting remain book-wide. Selection controls generation, mastering, and that run’s export.

### Files and reports

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/projects/{project_id}/download` | Download the full M4B, or latest partial when no full file exists |
| `GET` | `/api/projects/{project_id}/download/chapter/{number}` | Download a mastered chapter WAV |
| `POST` | `/api/projects/{project_id}/export-partial` | Export currently mastered chapters |
| `GET` | `/api/projects/{project_id}/script` | Get the book script |
| `GET` | `/api/projects/{project_id}/characters` | Get the character registry |
| `GET` | `/api/projects/{project_id}/voices` | List assignable voices and preview readiness |
| `GET` | `/api/projects/{project_id}/voices/{voice_id}/preview` | Stream a reference-voice WAV |
| `PATCH` | `/api/projects/{project_id}/characters/{character_id}/voice` | Reassign a character to a voice |
| `POST` | `/api/projects/{project_id}/voices/{voice_id}/regenerate` | Redesign and validate one reference voice |
| `POST` | `/api/projects/{project_id}/voices/{voice_id}/upload` | Import a recorded reference plus exact transcript |
| `POST` | `/api/projects/{project_id}/voice-review/approve` | Approve a new project's cast and optionally continue |
| `GET` | `/api/projects/{project_id}/quality` | Aggregate quality results |
| `GET` | `/api/projects/{project_id}/logs` | Recent project log lines |
| `GET` | `/api/projects/{project_id}/logs/stream` | SSE project log stream |
| `POST` | `/api/projects/{project_id}/fetch-metadata` | Explicit Google Books lookup |
| `GET` | `/api/projects/{project_id}/metadata-candidate/cover` | Preview the cached matched cover |
| `GET` | `/api/projects/{project_id}/cover` | Display the project's current cover |

Project IDs and all resolved files are constrained beneath the project/workspace roots.

Metadata lookup accepts JSON. `{"apply": false}` returns a ranked, validated
candidate without changing the book; `{"apply": true, "replace_cover": false}`
merges the reviewed description, ISBN, genre, year, and provider provenance.
EPUB title/author are not replaced. An existing cover is preserved unless
`replace_cover` is explicitly true. `refresh` bypasses the query-specific
cache. No match returns `404`; provider/network failure returns `502`.

`GET /voices` returns a speaking-only cast. Non-speaking registry entries are
reported only as an excluded count and cannot receive voice assignments. Voice
previews are read-only and available during any stage once the reference
exists. Reassignment, redesign, and upload require the pipeline to be stopped
or parked at a safe boundary. A change invalidates only dependent chapters.

When narration exists, `narrator_choice` contains the ready state, preview URL,
and selected ID for the male and female narrator candidates. Assign a candidate
with the normal character voice endpoint using `character_id = narrator`.

The upload route accepts multipart `file` and `transcript`. The transcript must
exactly match the clean single-speaker recording. WAV, FLAC, MP3, M4A, AAC, and
OGG are accepted; FFmpeg normalizes audio to mono 24 kHz PCM WAV after duration,
silence, and clipping checks. Before the existing reference is replaced, the
managed Voice validator compares Whisper's transcription with the supplied
text and rejects material mismatches. The first upload check may therefore
take a minute while the models start; services started only for the check are
released afterward.

New projects stop at `voice_review` once bootstrap succeeds. Approval accepts
`{"continue_pipeline": true}`. This gate is recorded once, so later partial
chapter batches do not wait for approval again.

### Dashboard WebSocket

`WS /ws/updates` carries pipeline progress and error messages for the browser dashboard.

## Voice API

The Brain normally calls these routes. Direct callers must use project-relative paths accepted by the server; arbitrary filesystem paths are rejected.

### Health and lifecycle

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/health` | Service/model/GPU health |
| `POST` | `/cancel/{project_id}` | Mark active project generation cancelled |
| `POST` | `/unload` | Unload models after the active GPU operation |
| `WS` | `/ws/progress` | Generation progress stream |

### Voice references

| Method | Route | Purpose |
|---|---|---|
| `POST` | `/voices/bootstrap` | Design/validate/register project voices |
| `POST` | `/voices/regenerate` | Force one reference voice to change |
| `GET` | `/voices/{project_id}` | List registered project voices |

Bootstrap request:

```json
{
  "project_id": "example",
  "characters": {
    "narrator": {
      "id": "narrator",
      "name": "Narrator",
      "gender": "other",
      "voice_description": "A clear, restrained storytelling voice"
    }
  },
  "force_regenerate": false
}
```

### Generation

| Method | Route | Purpose |
|---|---|---|
| `POST` | `/generate/line` | Generate one line |
| `POST` | `/generate/chapter` | Generate, validate, and retry a complete script chapter |
| `POST` | `/validate` | Validate one existing workspace/voice-library audio file |

Chapter generation accepts:

```json
{
  "project_id": "example",
  "chapter_number": 3,
  "lines": [],
  "validate": true,
  "auto_retry": true,
  "max_retries": 3,
  "validation_terms": ["Tuka"],
  "language": "en",
  "validation_revision": ""
}
```

`POST /generate/chapter` returns `application/x-ndjson`. Each line is an object with `type = progress`, `result`, or `error`. Progress data includes `phase` (`synthesis` or `validation`), `line_id`, `completed`, `total`, `percent`, `cache_hit`, and `attempt`. A client disconnect cooperatively cancels the run; a second overlapping request for the same project returns `409`.

The terminal result includes `generated_line_ids`, `failed_line_ids`, and every `quality_results` attempt. `validation_terms` comes from character and pronunciation dictionaries and only scopes fuzzy fantasy-name matching. Success requires an exact one-to-one match with the request’s unique script line IDs. A partial/misaligned response is not a successful chapter.

### Mastering and export

| Method | Route | Purpose |
|---|---|---|
| `POST` | `/master/chapter` | Assemble exact segment inputs, announce the title, and master a chapter WAV |
| `POST` | `/export/m4b` | Package the supplied mastered chapters |
| `GET` | `/download/{project_id}/{path}` | Download a file beneath that project workspace |

Mastering fails on any missing, empty, or unreadable expected segment. Export fails on missing/uninspectable chapter audio. `output_name` is sanitized to a filename and lets partial exports avoid overwriting the full-book output.

## Error semantics

- `400` / `422`: invalid request, unsafe path, invalid selection, or malformed upload
- `401`: token missing or incorrect
- `404`: project or artifact not found
- `409`: already running, cancellation, or conflicting GPU lifecycle action
- `500`: generation, validation, mastering, or export failure
- `503`: service/model unavailable

Clients should treat only an explicit success response with complete artifact IDs as completion; HTTP success plus a partial line set is not sufficient.

## Diagnostics, performance, and review

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/system/preflight` | Read-only effective runtime/backend compatibility report; does not load models |
| `GET` | `/api/projects/{id}/metrics` | Versioned, cache-aware performance summary (`?format=csv` downloads chapter rows) |
| `GET` | `/api/projects/{id}/storage` | Storage categories plus an exact safe-cleanup preview and confirmation token |
| `POST` | `/api/projects/{id}/storage/cleanup` | Remove only the unchanged set of previewed retry/temp files |
| `GET` | `/api/projects/{id}/support-bundle` | Redacted configs, state, metrics, preflight, and bounded log tails; no audio |
| `GET` | `/api/projects/{id}/quality/review` | Join-warning queue enriched with adjacent script/audio and saved disposition |
| `POST` | `/api/projects/{id}/quality/review` | Save `unreviewed`, `acceptable`, `needs_remaster`, or `source_tts_issue` |
| `GET` | `/api/projects/{id}/segments/{line_id}/audio` | Stream one segment for local quality review |
| `GET` | `/api/projects/{id}/voices/{voice_id}/download` | Download a reusable reference WAV named with its book and character |

Quality attempts include `selected`. This identifies the artifact that was
actually retained; it is not necessarily the numerically latest attempt.
Retry counts still describe every attempted retry.
