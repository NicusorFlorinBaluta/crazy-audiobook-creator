#!/bin/bash
# Crazy Audiobook Creator - Ubuntu Voice Server Setup
# Prepares the Ubuntu machine (The Voice) for running the TTS and Validation pipeline.

set -e

echo "============================================================"
echo " Crazy Audiobook Creator — Ubuntu Voice Server Setup"
echo "============================================================"

# 1. System Dependencies
echo "[1/4] Checking system dependencies..."
if ! command -v ffmpeg &> /dev/null; then
    echo "WARNING: ffmpeg not found. You may need to install it manually."
fi
if ! command -v python3 -m venv &> /dev/null; then
    echo "WARNING: python3-venv not found. You may need to install it manually."
fi

# 2. Python Virtual Environment
echo "[2/4] Setting up Python virtual environment..."
cd "$(dirname "$0")/.."
python3 -m venv venv
source venv/bin/activate

# 3. Python Packages
echo "[3/4] Installing Python dependencies..."
# Install standard dependencies
pip install -r voice/requirements.txt

# Install PyTorch with CUDA support (adjust version if needed for RTX 2080 Super)
echo "Installing PyTorch with CUDA 12.1..."
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121

# 4. Setup Parler-TTS Venv
echo "[4/5] Setting up Parler-TTS virtual environment..."
python3 -m venv venv_parler
source venv_parler/bin/activate
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
pip install git+https://github.com/huggingface/parler-tts.git
pip install fastapi uvicorn requests

# 5. Check hardware
echo "[5/5] Checking NVIDIA hardware..."
if command -v nvidia-smi &> /dev/null; then
    nvidia-smi
else
    echo "WARNING: nvidia-smi not found. Ensure NVIDIA drivers are installed."
fi

echo "============================================================"
echo " Setup complete! "
echo ""
echo " To run the voice server:"
echo "   source venv/bin/activate"
echo "   python -m voice.tts_server.main"
echo "============================================================"
