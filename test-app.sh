#!/bin/bash
# End-to-end smoke test for SessionPicker.app (macOS).
#
# Builds the app, points it at generated fixtures, and runs its SP_SELFTEST
# mode: panel shows → indexer returns rows → preview renders → the opener
# resolves a resume command. The panel appears briefly on screen (opaque).
set -euo pipefail
cd "$(dirname "$0")"

if [[ "$(uname)" != "Darwin" ]] || ! command -v swiftc &>/dev/null; then
  echo "SKIP: test-app.sh needs macOS with swiftc"
  exit 0
fi

./build-app.sh >/dev/null

rm -rf demo/projects demo/sessions.db demo/cache.tsv
python3 demo/gen-fixtures.py >/dev/null
export SESSION_PROJECTS_DIR="$PWD/demo/projects"
export SESSION_DB_PATH="$PWD/demo/sessions.db"
export SESSION_CACHE_PATH="$PWD/demo/cache.tsv"
python3 session_indexer.py >/dev/null

# Run under a Dock-like minimal environment: LaunchServices launches apps
# WITHOUT Homebrew/user PATH entries, and a terminal-inherited PATH would
# hide dependency-resolution bugs (the 'fzf required' alert regression).
env -i HOME="$HOME" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
  SESSION_PROJECTS_DIR="$PWD/demo/projects" \
  SESSION_DB_PATH="$PWD/demo/sessions.db" \
  SESSION_CACHE_PATH="$PWD/demo/cache.tsv" \
  SP_SELFTEST=1 SP_OPAQUE=1 ./SessionPicker.app/Contents/MacOS/SessionPicker
