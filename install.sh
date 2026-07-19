#!/bin/bash
# claude-sessions installer — symlinks the CLI picker into PATH and builds the
# initial index. Run from a clone; safe to re-run (idempotent).
#
#   git clone https://github.com/kkauf/claude-sessions.git
#   cd claude-sessions && ./install.sh
#   ./install.sh --app     # also build + install SessionPicker.app (macOS)
set -euo pipefail
cd "$(dirname "$0")"

missing=()
for cmd in fzf jq python3; do
  command -v "$cmd" &>/dev/null || missing+=("$cmd")
done
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "Missing dependencies: ${missing[*]}" >&2
  echo "  macOS:  brew install ${missing[*]}" >&2
  echo "  Linux:  apt install ${missing[*]}" >&2
  exit 1
fi

# First writable bin dir on PATH wins.
BIN_DIR=""
for d in /opt/homebrew/bin /usr/local/bin "$HOME/.local/bin"; do
  if [[ -d "$d" && -w "$d" ]]; then BIN_DIR="$d"; break; fi
done
if [[ -z "$BIN_DIR" ]]; then
  BIN_DIR="$HOME/.local/bin"
  mkdir -p "$BIN_DIR"
  echo "Note: $BIN_DIR created — make sure it's on your PATH."
fi

ln -sf "$PWD/claude-sessions" "$BIN_DIR/claude-sessions"
echo "Linked $BIN_DIR/claude-sessions -> $PWD/claude-sessions"

echo "Building initial index (one-time)…"
python3 session_indexer.py --rebuild --timing

echo
echo "Done. Run: claude-sessions"
if [[ "${1:-}" == "--app" ]]; then
  ./build-app.sh --install
elif [[ "$(uname)" == "Darwin" ]] && command -v swiftc &>/dev/null; then
  echo "Optional native app (Dock-icon picker, no terminal): ./build-app.sh --install"
fi
