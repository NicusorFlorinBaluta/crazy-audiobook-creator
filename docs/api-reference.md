# API Reference

## Overview

The pipeline uses two REST API servers:
1. **Brain API** (Windows, port 8000) — Dashboard backend + pipeline orchestration
2. **Voice API** (Ubuntu, port 8100) — TTS generation, validation, and mastering

Both servers use FastAPI and communicate over the local network.

---

## Voice API (Ubuntu — Port 8100)

The Voice API is the TTS server running on the Ubuntu machine. The Brain (Windows) calls these endpoints to generate audio.

### Health Check

```
GET /health
```

**Response**:
```json
{
  "status": "ok",
  "gpu": "NVIDIA GeForce RTX 2080 Super",
  "vram_total_gb": 8.0,
  "vram_used_gb": 6.8,
  "model_loaded": "Qwen3-TTS-1.7B",
  "uptime_seconds": 3621
}
```

---

### Bootstrap Character Voices

Generate voice reference clips for all characters in a project.

```
POST /voices/bootstrap
```

**Request Body**:
```json
{
  "project_id": "name-of-the-wind",
  "characters": {
    "narrator": {
      "name": "Narrator",
      "gender": "male",
      "voice_description": "Warm mature male baritone, early 40s, measured storytelling cadence..."
    },
    "kvothe": {
      "name": "Kvothe",
      "gender": "male",
      "voice_description": "Young male tenor, late teens. Quick and energetic..."
    }
  }
}
```

**Response**:
```json
{
  "status": "success",
  "project_id": "name-of-the-wind",
  "voices_generated": {
    "narrator": {
      "file": "voice_library/name-of-the-wind/narrator.wav",
      "duration_seconds": 10.2,
      "sample_rate": 24000
    },
    "kvothe": {
      "file": "voice_library/name-of-the-wind/kvothe.wav",
      "duration_seconds": 10.1,
      "sample_rate": 24000
    }
  }
}
```

**Notes**:
- Each voice clip is ~10 seconds of neutral speech
- Clips are saved to the voice library and reused for all subsequent generation
- If a voice already exists for a character, it is NOT regenerated (idempotent)
- To force regeneration, use `"force_regenerate": true`

---

### Generate Single Line

Generate audio for a single script line.

```
POST /generate/line
```

**Request Body**:
```json
{
  "project_id": "name-of-the-wind",
  "line": {
    "line_id": "ch01_005",
    "speaker": "kvothe_old",
    "text": "You should be careful what questions you ask, Chronicler.",
    "emotion": "warning, quiet intensity, measured",
    "speed": 0.9
  }
}
```

**Response**:
```json
{
  "status": "success",
  "line_id": "ch01_005",
  "audio_file": "workspace/name-of-the-wind/segments/ch01_005.wav",
  "duration_seconds": 3.7,
  "sample_rate": 24000
}
```

---

### Generate Full Chapter

Generate audio for an entire chapter. This is the primary endpoint for batch generation.

```
POST /generate/chapter
```

**Request Body**:
```json
{
  "project_id": "name-of-the-wind",
  "chapter_number": 1,
  "lines": [
    {
      "line_id": "ch01_001",
      "speaker": "narrator",
      "text": "It was night again...",
      "emotion": "contemplative, somber",
      "speed": 0.85,
      "pause_before_ms": 1000,
      "pause_after_ms": 1200
    }
  ],
  "validate": true,
  "auto_retry": true,
  "max_retries": 3
}
```

**Response** (returns after ALL lines are generated):
```json
{
  "status": "success",
  "chapter_number": 1,
  "total_lines": 247,
  "generated": 247,
  "failed_validation": 2,
  "retried": 5,
  "total_duration_seconds": 1834.5,
  "quality_report": {
    "average_wer": 0.018,
    "worst_wer": 0.067,
    "flagged_lines": ["ch01_103", "ch01_198"],
    "artifact_detections": 0
  },
  "segment_files": "workspace/name-of-the-wind/segments/"
}
```

**Progress Updates** (via WebSocket):
During generation, progress updates are streamed via WebSocket at `ws://UBUNTU:8100/ws/progress`:
```json
{
  "project_id": "name-of-the-wind",
  "chapter": 1,
  "line_id": "ch01_045",
  "progress": 45,
  "total": 247,
  "percent": 18.2,
  "eta_seconds": 1230,
  "current_speaker": "narrator",
  "current_emotion": "tense, urgent"
}
```

---

### Validate Audio Segment

Manually validate a specific audio segment.

```
POST /validate
```

**Request Body**:
```json
{
  "audio_file": "workspace/name-of-the-wind/segments/ch01_005.wav",
  "expected_text": "You should be careful what questions you ask, Chronicler."
}
```

**Response**:
```json
{
  "status": "pass",
  "wer": 0.0,
  "transcribed_text": "you should be careful what questions you ask chronicler",
  "expected_text_normalized": "you should be careful what questions you ask chronicler",
  "duration_seconds": 3.7,
  "expected_duration_seconds": 3.5,
  "duration_deviation": 0.057,
  "peak_dbfs": -3.2,
  "noise_floor_db": -62.1,
  "clipping_detected": false,
  "quality_score": 0.97
}
```

---

### Master Chapter Audio

Assemble and master all segments for a chapter.

```
POST /master/chapter
```

**Request Body**:
```json
{
  "project_id": "name-of-the-wind",
  "chapter_number": 1,
  "segments": [
    {
      "line_id": "ch01_001",
      "file": "workspace/name-of-the-wind/segments/ch01_001.wav",
      "pause_before_ms": 1000,
      "pause_after_ms": 1200
    }
  ],
  "mastering_config": {
    "target_lufs": -19,
    "peak_limit_dbfs": -1.0,
    "crossfade_ms": 30,
    "output_sample_rate": 44100
  }
}
```

**Response**:
```json
{
  "status": "success",
  "chapter_number": 1,
  "output_file": "workspace/name-of-the-wind/chapters/chapter_001.wav",
  "duration_seconds": 1834.5,
  "lufs": -19.1,
  "peak_dbfs": -1.2,
  "file_size_mb": 309.4
}
```

---

### Export M4B Audiobook

Package all mastered chapters into a final M4B file.

```
POST /export/m4b
```

**Request Body**:
```json
{
  "project_id": "name-of-the-wind",
  "metadata": {
    "title": "The Name of the Wind",
    "author": "Patrick Rothfuss",
    "narrator": "AI Generated",
    "genre": "Fantasy",
    "year": "2007",
    "description": "A multi-voice audiobook generated by Crazy Audiobook Creator"
  },
  "chapters": [
    {"number": 1, "title": "A Place for Demons", "file": "chapters/chapter_001.wav"},
    {"number": 2, "title": "A Beautiful Day", "file": "chapters/chapter_002.wav"}
  ],
  "cover_art": "workspace/name-of-the-wind/cover.jpg",
  "output_config": {
    "codec": "aac",
    "bitrate": "128k",
    "channels": 1
  }
}
```

**Response**:
```json
{
  "status": "success",
  "output_file": "workspace/name-of-the-wind/output/name-of-the-wind.m4b",
  "total_duration": "10:34:21",
  "total_chapters": 92,
  "file_size_mb": 587.3,
  "download_url": "/download/name-of-the-wind/output/name-of-the-wind.m4b"
}
```

---

### Download File

Download any file from the Ubuntu workspace.

```
GET /download/{project_id}/{path}
```

**Example**:
```
GET /download/name-of-the-wind/output/name-of-the-wind.m4b
```

Returns the file as a binary stream with appropriate Content-Type header.

---

### List Voice Library

```
GET /voices/{project_id}
```

**Response**:
```json
{
  "project_id": "name-of-the-wind",
  "voices": [
    {
      "character_id": "narrator",
      "name": "Narrator",
      "file": "voice_library/name-of-the-wind/narrator.wav",
      "duration_seconds": 10.2,
      "created_at": "2026-07-13T20:00:00Z"
    }
  ]
}
```

---

### Regenerate Voice

Force-regenerate a character's voice reference clip.

```
POST /voices/regenerate
```

**Request Body**:
```json
{
  "project_id": "name-of-the-wind",
  "character_id": "kvothe",
  "voice_description": "Updated voice description if you want to change it"
}
```

---

## Brain API (Windows — Port 8000)

The Brain API serves the web dashboard and orchestrates the pipeline.

### Dashboard Endpoints

```
GET /                           → Dashboard home page (HTML)
GET /api/projects               → List all projects
POST /api/projects              → Create new project (upload EPUB)
GET /api/projects/{id}          → Get project details
GET /api/projects/{id}/script   → Get generated script
GET /api/projects/{id}/quality  → Get quality report
POST /api/projects/{id}/start   → Start pipeline execution
POST /api/projects/{id}/stop    → Stop pipeline execution
GET /api/projects/{id}/status   → Get pipeline status
POST /api/projects/{id}/retry/{line_id}  → Retry a specific line
```

### Create Project

```
POST /api/projects
Content-Type: multipart/form-data
```

**Form Fields**:
- `file`: EPUB file upload
- `title`: Optional title override
- `author`: Optional author override

**Response**:
```json
{
  "project_id": "name-of-the-wind",
  "title": "The Name of the Wind",
  "author": "Patrick Rothfuss",
  "chapters_detected": 92,
  "total_words": 187000,
  "estimated_audio_hours": 10.5,
  "estimated_generation_hours": 8.2,
  "status": "created"
}
```

### Pipeline Status

```
GET /api/projects/{id}/status
```

**Response**:
```json
{
  "project_id": "name-of-the-wind",
  "status": "generating",
  "stage": "tts_generation",
  "current_chapter": 15,
  "total_chapters": 92,
  "current_line": 247,
  "total_lines": 3850,
  "chapters_completed": 14,
  "lines_generated": 2156,
  "lines_failed": 3,
  "average_wer": 0.021,
  "elapsed_seconds": 14400,
  "eta_seconds": 28800,
  "started_at": "2026-07-13T20:00:00Z"
}
```

### WebSocket — Real-Time Updates

```
ws://localhost:8000/ws/updates
```

The dashboard connects to this WebSocket for real-time progress. The Brain server proxies updates from the Ubuntu machine.

**Message Types**:
```json
{"type": "progress", "chapter": 15, "line": 247, "total": 3850, "percent": 63.4}
{"type": "quality", "line_id": "ch15_032", "wer": 0.034, "status": "pass"}
{"type": "quality", "line_id": "ch15_045", "wer": 0.089, "status": "fail", "retrying": true}
{"type": "chapter_complete", "chapter": 15, "duration_seconds": 1834}
{"type": "pipeline_complete", "total_duration": "10:34:21", "file_size_mb": 587}
{"type": "error", "message": "Ubuntu TTS server unreachable", "retrying_in": 60}
```

---

## Data Models (Shared)

These Pydantic models are shared between Brain and Voice:

### Character
```python
class Character(BaseModel):
    id: str                          # "narrator", "kvothe", etc.
    name: str                        # Display name
    gender: Literal["male", "female", "other"]
    age_range: str                   # "40s", "late teens"
    personality_traits: list[str]
    voice_description: str           # For TTS Voice Design
    speaking_style: str
    discovered_in_pass2: bool = False
```

### ScriptLine
```python
class ScriptLine(BaseModel):
    line_id: str                     # "ch01_001"
    speaker: str                     # Character ID
    text: str                        # Text to speak
    emotion: str                     # "contemplative, somber"
    speed: float = 1.0               # 0.8 - 1.2
    pause_before_ms: int = 0         # 0 - 2000
    pause_after_ms: int = 500        # 0 - 2000
```

### Chapter
```python
class Chapter(BaseModel):
    number: int
    title: str
    summary: str                     # For continuity with next chapter
    lines: list[ScriptLine]
```

### QualityResult
```python
class QualityResult(BaseModel):
    line_id: str
    status: Literal["pass", "fail", "flagged"]
    wer: float
    transcribed_text: str
    duration_seconds: float
    expected_duration_seconds: float
    peak_dbfs: float
    noise_floor_db: float
    clipping_detected: bool
    quality_score: float             # 0.0 - 1.0
    attempt: int                     # Which retry attempt
```

### ProjectStatus
```python
class ProjectStatus(BaseModel):
    project_id: str
    status: Literal["created", "extracting", "scripting", "bootstrapping",
                     "generating", "mastering", "exporting", "complete", "error"]
    current_chapter: int | None
    total_chapters: int
    current_line: int | None
    total_lines: int
    lines_generated: int
    lines_failed: int
    average_wer: float
    elapsed_seconds: float
    eta_seconds: float | None
```
