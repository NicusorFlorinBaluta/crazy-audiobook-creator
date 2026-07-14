# Windows Setup Guide — "The Brain"

## Overview

The Windows machine runs:
- **Ollama** with a Qwen3 32B model for LLM script analysis
- **The Orchestrator** — Python pipeline that coordinates everything
- **The Web Dashboard** — FastAPI + HTML frontend for monitoring

## Prerequisites

| Requirement | Minimum | Recommended |
|-------------|---------|-------------|
| OS | Windows 10/11 | Windows 11 |
| GPU | 16GB+ VRAM | AMD 7900 XTX (24GB) |
| RAM | 16 GB | 32 GB |
| Python | 3.11+ | 3.12 |
| Storage | 25 GB free | 50 GB free |
| Network | Connected to Ubuntu machine | Same LAN, gigabit ethernet |

---

## Step 1: Install Ollama

Ollama is the LLM runtime. It handles model management and provides an API.

### Install
1. Download Ollama from [ollama.com](https://ollama.com)
2. Run the installer
3. Verify installation:
   ```powershell
   ollama --version
   ```

### Configure for AMD GPU (Vulkan)

The 7900 XTX works best with Ollama's Vulkan backend on Windows:

```powershell
# Set environment variable for Vulkan backend (if ROCm gives issues)
[System.Environment]::SetEnvironmentVariable("OLLAMA_VULKAN", "1", "User")
```

If using ROCm instead:
```powershell
# For ROCm (if Vulkan isn't performing well)
[System.Environment]::SetEnvironmentVariable("HSA_OVERRIDE_GFX_VERSION", "11.0.0", "User")
```

Restart your terminal after setting environment variables.

### Download the LLM Model

```powershell
# Qwen3 32B — recommended for best script quality
# Q4_K_M quantization: ~20GB download, fits in 24GB VRAM
ollama pull qwen3:32b

# Verify
ollama list
```

**Alternative models** (if Qwen3 32B is too slow):
```powershell
# Qwen3 14B — faster, slightly lower quality
ollama pull qwen3:14b

# Llama 3.3 70B Q2 — fits in 24GB but very slow
ollama pull llama3.3:70b-q2_K
```

### Test the LLM

```powershell
ollama run qwen3:32b "Describe the speaking voice of a gruff dwarven blacksmith in a fantasy novel. Be specific about tone, pitch, pace, and accent."
```

You should get a detailed voice description. If it works, the LLM is ready.

---

## Step 2: Install Python

### Option A: Official Python
1. Download Python 3.12 from [python.org](https://python.org)
2. During install, **check "Add Python to PATH"**
3. Verify:
   ```powershell
   python --version
   ```

### Option B: via Conda/Miniconda
```powershell
# Create a dedicated environment
conda create -n audiobook python=3.12
conda activate audiobook
```

---

## Step 3: Install the Brain

```powershell
# Navigate to the project
cd e:\Projects\crazy-audiobook-creator\brain

# Create virtual environment (if not using conda)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

### Dependencies (brain/requirements.txt)
```
# Web framework
fastapi>=0.115.0
uvicorn>=0.30.0
websockets>=13.0

# EPUB parsing
ebooklib>=0.18
beautifulsoup4>=4.12.0
lxml>=5.0

# LLM client
httpx>=0.27.0

# Data validation
pydantic>=2.9.0

# Database
aiosqlite>=0.20.0

# Utilities
pyyaml>=6.0
rich>=13.0          # Pretty console output
click>=8.1          # CLI interface
```

---

## Step 4: Configure

Edit `brain/config.yaml`:

```yaml
ollama:
  host: "http://localhost:11434"
  model: "qwen3:32b"
  context_window: 10

ubuntu:
  host: "http://UBUNTU_IP_ADDRESS:8100"  # ← Replace with your Ubuntu machine's IP
  timeout: 30
  retries: 3

extraction:
  skip_toc: true
  skip_appendices: true
  min_chapter_words: 100

script:
  max_segment_sentences: 4
  default_speed: 1.0
  narrator_pause_ms: 500
  dialogue_pause_ms: 300
  chapter_start_pause_ms: 1000
  chapter_end_pause_ms: 2000

dashboard:
  port: 8000
  host: "0.0.0.0"
```

### Find Your Ubuntu Machine's IP

On Ubuntu, run:
```bash
ip addr show | grep "inet " | grep -v 127.0.0.1
```

---

## Step 5: Network Setup

Both machines need to communicate over the LAN.

### Windows Firewall
Allow inbound connections on port 8000 (dashboard):
```powershell
New-NetFirewallRule -DisplayName "Audiobook Dashboard" -Direction Inbound -Port 8000 -Protocol TCP -Action Allow
```

### Test Connectivity
```powershell
# Test that you can reach the Ubuntu machine
# (run this AFTER setting up the Ubuntu machine)
curl http://UBUNTU_IP_ADDRESS:8100/health
```

---

## Step 6: Run

### Start Ollama (if not auto-started)
```powershell
ollama serve
```

### Start the Pipeline + Dashboard
```powershell
cd e:\Projects\crazy-audiobook-creator\brain
python -m dashboard.api.main
```

Open `http://localhost:8000` in your browser.

---

## Troubleshooting

### Ollama doesn't detect GPU
```powershell
# Check GPU visibility
ollama ps

# Force Vulkan
$env:OLLAMA_VULKAN = "1"
ollama serve
```

### Model is too slow
- Try `qwen3:14b` instead of `qwen3:32b`
- Check GPU utilization: Task Manager → Performance → GPU
- Ensure no other GPU-intensive apps are running

### Can't reach Ubuntu machine
```powershell
# Ping test
ping UBUNTU_IP_ADDRESS

# Check if TTS server is running on Ubuntu
curl http://UBUNTU_IP_ADDRESS:8100/health
```

### Python version issues
```powershell
# Verify pip installs to the right environment
pip --version
which python  # Should point to your venv/conda env
```

---

## Directory Structure After Setup

```
brain/
├── .venv/              # Python virtual environment
├── config.yaml         # Configuration
├── requirements.txt    # Python dependencies
├── extractor/          # EPUB parsing module
├── director/           # LLM script generation
├── orchestrator/       # Pipeline coordination
├── dashboard/          # Web UI
│   ├── api/            # FastAPI backend
│   └── frontend/       # HTML/CSS/JS
└── projects/           # Generated audiobook data
    └── {project_name}/
        ├── book.json           # Extracted text
        ├── characters.json     # Character registry
        ├── script/             # Generated scripts per chapter
        │   ├── chapter_001.json
        │   └── ...
        ├── quality_report.json # Validation results
        └── audiobook.m4b       # Final output (downloaded from Ubuntu)
```
