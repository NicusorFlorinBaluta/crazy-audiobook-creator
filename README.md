# 🎧 Crazy Audiobook Creator

A fully local, two-machine audiobook production pipeline that converts fantasy/fiction EPUB books into professional-grade, multi-speaker, emotionally expressive audiobooks — fully automated with AI quality validation.

## ✨ Features

- **Electron Desktop Shell** — Native desktop application (`desktop/`) with automated subprocess management and 100% guaranteed process/VRAM cleanup on quit
- **Fully Automated Pipeline** — Drop an EPUB, get a chaptered M4B audiobook
- **Multi-Speaker & Narrator Chapter Announcements** — LLM identifies all characters and assigns distinct voices; Narrator voice automatically speaks chapter title announcements
- **Standardized Audiobook Formatting** — Professional silence spacing (1.0s intro, 1.5s post-announcement pause, 0.5s inter-segment gap, 2.0s chapter outro)
- **Emotional Narration** — Context-aware emotion tagging for every line
- **Voice Design & SQLite Embedding Cache** — Unique voices generated from text descriptions with SQLite caching (`voice_cache.db`)
- **AI Quality Validation** — Whisper `small` STT transcription + generic text normalization (`num2words` + `EnglishTextNormalizer`) with WER threshold checks
- **Single-Instance Protection & Auto-VRAM Release** — OS-level file locking (`app.lock`) prevents duplicate app runs; GPU memory auto-cleans (`torch.cuda.empty_cache()`) after 5 mins idle
- **Professional Audio** — Vectorized noise gate, LUFS loudness normalization (-19 LUFS target), cross-fading
- **100% Local** — No cloud APIs, no subscriptions, complete privacy

## 🏗️ Architecture

```
┌─────────────────────────────────────┐     ┌─────────────────────────────────────┐
│     🖥️ Windows PC (The Brain)       │     │     🐧 Ubuntu PC (The Voice)         │
│     AMD 7900 XTX · 24GB VRAM       │     │     RTX 2080 Super · 8GB VRAM       │
│                                     │     │                                     │
│  📚 EPUB Input                      │     │  🎤 Qwen3-TTS 1.7B                  │
│  ↓                                  │     │     Voice Design + Emotion Control  │
│  📝 Text Extraction                 │     │                                     │
│  ↓                                  │     │  ✅ Whisper Quality Validator        │
│  🧠 LLM Script Director             │────→│     WER Check + Artifact Detection  │
│     (Ollama · Qwen3 32B)           │     │                                     │
│     • Character detection          │     │  🎚️ Audio Mastering                  │
│     • Voice descriptions           │     │     LUFS Norm + Cross-fade          │
│     • Emotion tagging              │     │                                     │
│  🖥️ Web Dashboard                   │←────│  📦 M4B Export                       │
│     Monitor · Review · Override    │     │     Chapters + Metadata             │
└─────────────────────────────────────┘     └─────────────────────────────────────┘
```

## 🔧 Tech Stack

| Layer | Technology |
|-------|-----------|
| LLM | Ollama + Qwen3 32B Q4 (Windows, Vulkan) |
| TTS | Qwen3-TTS 1.7B (Ubuntu, CUDA) |
| Validation | faster-whisper + jiwer |
| Audio | FFmpeg, pyloudnorm, pydub |
| Backend | Python 3.12+ / FastAPI |
| Frontend | Vanilla HTML/CSS/JS |
| Database | SQLite |
| Communication | REST API + WebSocket |

## 📂 Project Structure

```
crazy-audiobook-creator/
├── brain/          # Windows — LLM orchestrator + web dashboard
├── voice/          # Ubuntu — TTS server + audio mastering
├── shared/         # Shared Pydantic models and constants
├── docs/           # Setup guides and architecture docs
├── scripts/        # Install scripts
└── projects/       # Generated audiobook projects (gitignored)
```

## 🚀 Quick Start

### Prerequisites
- **Windows PC**: AMD/NVIDIA GPU with 16GB+ VRAM, Python 3.12+, Ollama
- **Ubuntu PC**: NVIDIA GPU with 8GB+ VRAM, Python 3.12+, CUDA 12.x
- Both machines on the same local network

### Setup
```bash
# Windows — install brain
cd brain
pip install -r requirements.txt
ollama pull qwen3:32b

# Ubuntu — install voice
cd voice
pip install -r requirements.txt
# Models download automatically on first run
```

### Run
```bash
# Ubuntu — start TTS server
cd voice && python -m tts_server.main

# Windows — start pipeline + dashboard
cd brain && python -m dashboard.api.main
```

Then open `http://localhost:8000` in your browser, upload an EPUB, and let it run.

## 📖 Documentation

- [Architecture Guide](docs/architecture.md) — Detailed pipeline design (stages, data flow, error recovery)
- [Windows Setup](docs/setup-windows.md) — Brain machine setup (Ollama, AMD GPU, Python)
- [Ubuntu Setup](docs/setup-ubuntu.md) — Voice machine setup (CUDA, models, systemd service)
- [LLM Prompts](docs/prompts.md) — Script director prompt engineering (templates, examples, strategies)
- [Voice Design](docs/voice-design.md) — Character voice creation (archetypes, consistency, fantasy tips)
- [Quality Assurance](docs/quality-assurance.md) — AI validation system (Whisper, WER, retry logic)
- [API Reference](docs/api-reference.md) — TTS server and dashboard APIs (endpoints, schemas)
- [Configuration](docs/configuration.md) — All config options explained (YAML reference, profiles)

## 📊 Quality Targets

| Metric | Target |
|--------|--------|
| Word Error Rate | < 5% per segment |
| Loudness | -18 to -20 LUFS |
| Noise Floor | < -50 dB |
| Peak Level | < -1 dBFS |
| Voice Consistency | Same reference clip per character |

## 📄 License

MIT
