#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/.venv-voter/bin/python"

cd "$SCRIPT_DIR"

if [ ! -x "$VENV_PYTHON" ] || \
   ! "$VENV_PYTHON" -c "import pypdf, indic_transliteration" >/dev/null 2>&1; then
  echo "The converter environment is missing or needs an update."
  read -r -p "Run first-time setup now? [Y/n] " answer
  answer="${answer:-y}"
  if [[ "$answer" =~ ^[Yy]$ ]]; then
    "$SCRIPT_DIR/setup_voter_converter.sh"
  else
    echo "Cancelled."
    exit 1
  fi
fi

exec "$VENV_PYTHON" "$SCRIPT_DIR/voter_pdf_to_csv.py" "$@"
