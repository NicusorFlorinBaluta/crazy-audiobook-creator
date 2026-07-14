#!/bin/bash
# Crazy Audiobook Creator - Ubuntu Voice Server Setup
# Prepares the Ubuntu machine (The Voice) for running the TTS and Validation pipeline.

set -e

echo "============================================================"
echo " Crazy Audiobook Creator — Ubuntu Voice Server Setup"
echo "============================================================"

# 1. System Dependencies
echo "[1/4] Installing system dependencies..."
sudo apt-update
sudo apt-get install -y python3-pip python3-venv ffmpeg

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

# 4. Check hardware
echo "[4/4] Checking NVIDIA hardware..."
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
