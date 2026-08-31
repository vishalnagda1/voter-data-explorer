#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv-voter"
TESSDATA_DIR="$SCRIPT_DIR/tessdata"

cd "$SCRIPT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 was not found. Install Python 3 and run this setup again."
  exit 1
fi

if ! command -v gs >/dev/null 2>&1; then
  echo "Ghostscript was not found."
  echo "macOS: brew install ghostscript"
  echo "Ubuntu/Debian: sudo apt-get install ghostscript"
  exit 1
fi

if ! command -v tesseract >/dev/null 2>&1; then
  echo "Tesseract OCR was not found."
  echo "macOS: brew install tesseract"
  echo "Ubuntu/Debian: sudo apt-get install tesseract-ocr"
  exit 1
fi

echo "Creating the private Python environment..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$SCRIPT_DIR/requirements-voter-converter.txt"

mkdir -p "$TESSDATA_DIR"
if [ ! -f "$TESSDATA_DIR/hin.traineddata" ]; then
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required to download the official Hindi OCR model."
    exit 1
  fi
  echo "Downloading the official Tesseract Hindi OCR model..."
  curl -L --fail --show-error \
    -o "$TESSDATA_DIR/hin.traineddata" \
    "https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/main/hin.traineddata"
fi

echo
echo "Setup complete."
echo "You do not need to activate the virtual environment."
echo "Start the converter with:"
echo "  $SCRIPT_DIR/run_voter_converter.command"
