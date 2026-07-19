#!/usr/bin/env bash
# Preview wrapper for the fzf picker and SessionPicker.app.
# Called with: $1=sid $2=home_key (unused, kept for contract) $3=query
# All preview logic lives in session_indexer.py --preview (single source of
# truth for what counts as signal vs noise — see the Preview section there).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/session_indexer.py" --preview "$1" --search "${3:-}"
