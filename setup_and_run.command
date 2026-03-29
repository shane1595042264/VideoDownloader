#!/bin/bash
# VideoDownloader - Setup & Run Script
# Double-click this file to install dependencies and start the server

cd "$(dirname "$0")"

echo ""
echo "============================================"
echo "  VideoDownloader Setup"
echo "============================================"
echo ""

# Check for Homebrew
if ! command -v brew &> /dev/null; then
    echo "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

# Check for ffmpeg
if ! command -v ffmpeg &> /dev/null; then
    echo "Installing ffmpeg (needed for video merging)..."
    brew install ffmpeg
    echo "ffmpeg installed!"
else
    echo "ffmpeg already installed."
fi

# Check for Python 3
if ! command -v python3 &> /dev/null; then
    echo "Python 3 not found. Installing..."
    brew install python3
fi

# Install Python dependencies
echo "Installing Python dependencies..."
pip3 install -r requirements.txt --quiet 2>/dev/null || pip3 install -r requirements.txt --quiet --break-system-packages

# Kill any existing server on port 8000
lsof -ti:8000 | xargs kill 2>/dev/null

echo ""
echo "============================================"
echo "  Starting VideoDownloader..."
echo "  Open http://localhost:8000 in your browser"
echo "============================================"
echo ""

# Start the server
python3 run.py
