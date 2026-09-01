#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv-voter"
TESSDATA_DIR="$SCRIPT_DIR/tessdata"

cd "$SCRIPT_DIR"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv was not found. Install it and run this setup again."
  echo "macOS: brew install uv"
  echo "Other platforms: https://docs.astral.sh/uv/getting-started/installation/"
  exit 1
fi

echo "Creating the private Python environment..."
if [ ! -x "$VENV_DIR/bin/python" ]; then
  uv venv "$VENV_DIR"
fi
uv pip install --python "$VENV_DIR/bin/python" \
  -r "$SCRIPT_DIR/requirements-voter-converter.txt"

if command -v tesseract >/dev/null 2>&1; then
  mkdir -p "$TESSDATA_DIR"
fi
if command -v tesseract >/dev/null 2>&1 && [ ! -f "$TESSDATA_DIR/hin.traineddata" ]; then
  if command -v curl >/dev/null 2>&1; then
    TESSDATA_DOWNLOAD="$TESSDATA_DIR/hin.traineddata.download"
    echo "Downloading the official Tesseract Hindi OCR model..."
    if curl -L --fail --show-error \
      -o "$TESSDATA_DOWNLOAD" \
      "https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/main/hin.traineddata"; then
      mv "$TESSDATA_DOWNLOAD" "$TESSDATA_DIR/hin.traineddata"
    else
      echo "Warning: Hindi OCR model download failed. CSV transliteration is ready,"
      echo "but PDF conversion will need the Hindi Tesseract model."
    fi
  else
    echo "Note: curl was not found, so the Hindi OCR model was not downloaded."
    echo "CSV transliteration is ready; PDF conversion still requires that model."
  fi
fi

if ! command -v gs >/dev/null 2>&1; then
  echo
  echo "Note: Ghostscript was not found. CSV transliteration is ready, but PDF"
  echo "conversion requires Ghostscript (macOS: brew install ghostscript)."
fi
if ! command -v tesseract >/dev/null 2>&1; then
  echo
  echo "Note: Tesseract was not found. CSV transliteration is ready, but PDF"
  echo "conversion requires Tesseract OCR (macOS: brew install tesseract)."
fi

echo
echo "Setup complete."
echo "You do not need to activate the virtual environment."
echo "Start the converter with:"
echo "  $SCRIPT_DIR/run_voter_converter.command"
