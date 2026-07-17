# Ubuntu Setup Guide — "The Voice"

## Overview

The Ubuntu machine runs:
- **Qwen3-TTS 1.7B** — The speech synthesis engine
- **faster-whisper** — AI quality validation (speech-to-text)
- **TTS API Server** — FastAPI server that the Windows machine calls
- **Audio Mastering** — FFmpeg-based post-processing pipeline

## Prerequisites

| Requirement | Minimum | Recommended |
|-------------|---------|-------------|
| OS | Ubuntu 22.04+ | Ubuntu 24.04 LTS |
| GPU | NVIDIA with 8GB+ VRAM | RTX 2080 Super (8GB) |
| CUDA | 11.8+ | 12.x |
| RAM | 16 GB | 32 GB |
| Python | 3.11+ | 3.12 |
| Storage | 20 GB free | 40 GB free |
| Network | Connected to Windows machine | Same LAN |

---

## Step 1: NVIDIA Driver & CUDA

### Check Current Driver
```bash
nvidia-smi
```

You should see your RTX 2080 Super with driver version 535+ and CUDA 12.x.

### Install/Update Driver (if needed)
```bash
# Add NVIDIA PPA
sudo add-apt-repository ppa:graphics-drivers/ppa
sudo apt update

# Install recommended driver
sudo ubuntu-drivers autoinstall

# Reboot
sudo reboot
```

### Install CUDA Toolkit (if needed)
```bash
# Check if CUDA is installed
nvcc --version

# If not installed, install CUDA toolkit
sudo apt install nvidia-cuda-toolkit

# Verify
nvcc --version
nvidia-smi
```

---

## Step 2: Install System Dependencies

```bash
# FFmpeg (for audio processing and M4B export)
sudo apt install ffmpeg

# Python build dependencies
sudo apt install python3.12 python3.12-venv python3.12-dev python3-pip

# Audio processing libraries
sudo apt install libsndfile1 libsox-dev

# Verify
ffmpeg -version
python3.12 --version
```

---

## Step 3: Install the Voice Server

```bash
# Navigate to the project
cd /path/to/crazy-audiobook-creator/voice

# Create virtual environment
python3.12 -m venv .venv
source .venv/bin/activate

# Upgrade pip
pip install --upgrade pip

# Install PyTorch with CUDA support
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124

# Install project dependencies
pip install -r requirements.txt
```

### Dependencies (voice/requirements.txt)
```
# Web framework
fastapi>=0.115.0
uvicorn>=0.30.0
websockets>=13.0

# TTS Engine
transformers>=4.46.0
accelerate>=1.0.0
soundfile>=0.12.0
librosa>=0.10.0

# Quality Validation
faster-whisper>=1.1.0
jiwer>=3.0.0

# Audio Processing
pydub>=0.25.0
pyloudnorm>=0.1.1
numpy>=1.26.0
scipy>=1.14.0

# Data & Config
pydantic>=2.9.0
pyyaml>=6.0
aiosqlite>=0.20.0

# Utilities
httpx>=0.27.0
rich>=13.0
```

### Download Models (First Run)

The models will download automatically on first use, but you can pre-download them:

```bash
# Activate the virtual environment
source .venv/bin/activate

# Pre-download Qwen3-TTS 1.7B (~7GB)
python -c "
from transformers import AutoModelForCausalLM, AutoTokenizer
model_name = 'Qwen/Qwen3-TTS-1.7B'
print('Downloading tokenizer...')
AutoTokenizer.from_pretrained(model_name)
print('Downloading model...')
AutoModelForCausalLM.from_pretrained(model_name, torch_dtype='float16')
print('Done!')
"

# Pre-download faster-whisper medium model (~1.5GB)
python -c "
from faster_whisper import WhisperModel
print('Downloading Whisper medium model...')
model = WhisperModel('medium', device='cpu')
print('Done!')
"
```

> **Note**: The exact Qwen3-TTS API may differ from the generic transformers approach above.
> During implementation, we'll use the official Qwen3-TTS inference code from their repository.
> The download command above is illustrative — actual model loading will follow Qwen's recommended approach.

---

## Step 4: Configure

Edit `voice/config.yaml`:

```yaml
tts:
  model: "Qwen/Qwen3-TTS-1.7B"
  device: "cuda"
  dtype: "float16"
  sample_rate: 24000

validation:
  whisper_model: "medium"
  wer_threshold: 0.05
  max_retries: 3
  artifact_noise_threshold: -50
  duration_tolerance: 0.3

mastering:
  target_lufs: -19
  peak_limit_dbfs: -1.0
  crossfade_ms: 30
  noise_gate_threshold: -50
  output_sample_rate: 44100

export:
  codec: "aac"
  bitrate: "128k"
  channels: 1

server:
  port: 8100
  host: "0.0.0.0"
```

---

## Step 5: Firewall & Network

### Open the TTS Server Port
```bash
# Allow incoming connections on port 8100
sudo ufw allow 8100/tcp

# Verify
sudo ufw status
```

### Find Your IP Address
```bash
ip addr show | grep "inet " | grep -v 127.0.0.1
```

Give this IP to the Windows machine for `brain/config.yaml`.

### Test Connectivity from Windows
```powershell
# On Windows, test the connection
curl http://UBUNTU_IP:8100/health
```

---

## Step 6: Run

```bash
cd /path/to/crazy-audiobook-creator/voice
source .venv/bin/activate

# Start the TTS server
python -m tts_server.main
```

You should see:
```
INFO:     Loading Qwen3-TTS 1.7B to CUDA...
INFO:     Model loaded in 12.3s (VRAM: 6.8GB)
INFO:     TTS Server running on http://0.0.0.0:8100
INFO:     Endpoints:
INFO:       POST /voices/bootstrap  — Generate character voice references
INFO:       POST /generate/line     — Generate audio for a single line
INFO:       POST /generate/chapter  — Generate audio for an entire chapter
INFO:       POST /validate          — Validate an audio segment
INFO:       POST /master/chapter    — Master a chapter's audio
INFO:       POST /export/m4b        — Export final M4B audiobook
INFO:       GET  /health            — Health check
```

---

## Step 7: GPU Memory Management

The RTX 2080 Super has 8GB VRAM. Here's how it's used:

| Component | VRAM Usage | Notes |
|-----------|-----------|-------|
| Qwen3-TTS 1.7B (float16) | ~6-7 GB | Loaded at server start |
| faster-whisper medium | ~1.5 GB | Loaded on-demand for validation |
| PyTorch overhead | ~0.5 GB | CUDA context |

### Strategy: Sequential Processing
Since TTS + Whisper together exceed 8GB, they run sequentially:

1. **Generate phase**: Qwen3-TTS loaded → generate all segments for a chapter
2. **Validate phase**: Unload TTS → load Whisper → validate all segments
3. **Retry phase**: If segments failed → unload Whisper → reload TTS → regenerate → re-validate

This adds ~10-15 seconds per model swap but ensures everything fits in 8GB.

### Alternative: CPU Whisper
If you want to avoid model swapping:
```yaml
# In voice/config.yaml
validation:
  whisper_model: "medium"
  whisper_device: "cpu"  # Run Whisper on CPU, keep TTS on GPU
```
Whisper on CPU is slower (~1-2x real-time instead of ~10-30x) but avoids any VRAM contention.

---

## Storage Management

| Item | Size | Location |
|------|------|----------|
| Qwen3-TTS model | ~7 GB | `~/.cache/huggingface/` |
| Whisper model | ~1.5 GB | `~/.cache/huggingface/` |
| Voice library | ~50 MB/project | `voice/voice_library/{project}/` |
| Intermediate WAVs | ~5-10 GB/novel | `voice/workspace/{project}/` |
| Final M4B | ~500 MB-1 GB/novel | `voice/workspace/{project}/output/` |

### Auto-Cleanup
After M4B export, intermediate WAV files can be automatically deleted:
```yaml
# In voice/config.yaml
storage:
  auto_cleanup_intermediates: true  # Delete WAVs after M4B export
  keep_voice_library: true          # Always keep voice references
```

With 75GB available and auto-cleanup enabled, you can generate dozens of audiobooks without running out of space.

---

## Troubleshooting

### CUDA Out of Memory
```bash
# Check current VRAM usage
nvidia-smi

# Kill any stale Python processes using GPU
nvidia-smi | grep python
kill -9 <PID>

# Restart the server
python -m tts_server.main
```

### Model download fails
```bash
# Check internet connection
ping huggingface.co

# Clear cache and retry
rm -rf ~/.cache/huggingface/hub/models--Qwen--Qwen3-TTS-1.7B
python -m tts_server.main  # Will re-download
```

### FFmpeg not found
```bash
sudo apt install ffmpeg
which ffmpeg  # Should output /usr/bin/ffmpeg
```

### Port already in use
```bash
# Find what's using port 8100
sudo lsof -i :8100

# Kill it
sudo kill -9 <PID>
```

### Audio sounds garbled
- Ensure `sample_rate` in config matches Qwen3-TTS output (24000 Hz)
- Check that PyTorch CUDA is working: `python -c "import torch; print(torch.cuda.is_available())"`
- Verify audio files aren't corrupted: `ffprobe output.wav`

---

## Running as a Service (Optional)

To keep the TTS server running in the background:

### systemd Service
```bash
sudo tee /etc/systemd/system/audiobook-tts.service << 'EOF'
[Unit]
Description=Audiobook TTS Server
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/path/to/crazy-audiobook-creator/voice
ExecStart=/path/to/crazy-audiobook-creator/voice/.venv/bin/python -m tts_server.main
Restart=on-failure
RestartSec=10
Environment="CUDA_VISIBLE_DEVICES=0"

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable audiobook-tts
sudo systemctl start audiobook-tts

# Check status
sudo systemctl status audiobook-tts

# View logs
journalctl -u audiobook-tts -f
```

---

## Directory Structure After Setup

```
voice/
├── .venv/                  # Python virtual environment
├── config.yaml             # Configuration
├── requirements.txt        # Python dependencies
├── tts_server/             # TTS API server
│   ├── main.py             # FastAPI app
│   ├── qwen3_engine.py     # Qwen3-TTS wrapper
│   ├── voice_designer.py   # Voice bootstrapping
│   └── voice_library.py    # Voice clip management
├── validator/              # Quality validation
│   ├── whisper_validator.py
│   ├── audio_analyzer.py
│   └── validation_loop.py
├── mastering/              # Audio post-processing
│   ├── assembler.py
│   ├── normalizer.py
│   └── m4b_exporter.py
├── voice_library/          # Saved voice references
│   └── {project_name}/
│       ├── narrator.wav
│       ├── kvothe.wav
│       └── denna.wav
└── workspace/              # Working files (auto-cleaned)
    └── {project_name}/
        ├── segments/       # Individual line audio
        ├── chapters/       # Mastered chapter audio
        └── output/         # Final M4B file
```
