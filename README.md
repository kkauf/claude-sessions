# claude-sessions

Fast session picker for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Search and resume any session across all projects with fuzzy finding.

<!-- Add a screenshot or GIF here -->

## Features

- **Cross-project search** — find sessions from any project, not just the current one
- **Full-text search** — matches against titles, first messages, and all user message keywords
- **Incremental caching** — instant startup after first run (~0.3s warm, ~6-10s cold)
- **Preview panel** — see conversation history before resuming
- **Smart ranking** — title/preview matches rank above keyword-only matches
- **Relative dates** — "today", "2d", "1w", "3mo"
- **Cross-project tags** — sessions from other projects show a dim project label

## Requirements

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (the CLI)
- [fzf](https://github.com/junegunn/fzf) >= 0.40
- [jq](https://github.com/jqlang/jq) >= 1.6
- bash >= 4.0
- macOS or Linux

## Install

```bash
# Clone and symlink to PATH
git clone https://github.com/kkauf/claude-sessions.git
ln -s "$(pwd)/claude-sessions/claude-sessions" /usr/local/bin/claude-sessions

# Or just copy the script
curl -o /usr/local/bin/claude-sessions https://raw.githubusercontent.com/kkauf/claude-sessions/main/claude-sessions
chmod +x /usr/local/bin/claude-sessions
```

## Usage

```bash
# Browse all sessions
claude-sessions

# Pre-filter search
claude-sessions booking
claude-sessions "auth bug"
```

**Controls:**
- Type to search (exact substring match)
- Arrow keys to navigate
- `Enter` to resume the selected session
- `ctrl-/` to toggle the preview panel
- `Esc` to quit

## Raycast integration

You can launch the session picker from [Raycast](https://raycast.com) with an optional search query.

Create a Raycast script command (e.g., `~/.raycast-scripts/claude-sessions.sh`):

```bash
#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Claude Sessions
# @raycast.mode silent

# Optional parameters:
# @raycast.icon 🤖
# @raycast.packageName Claude Code
# @raycast.argument1 { "type": "text", "placeholder": "Search (optional)", "optional": true }

# Documentation:
# @raycast.description Open Claude Code session picker in terminal

SEARCH_QUERY="${1:-}"

# For iTerm2:
osascript <<EOF
tell application "iTerm"
    activate
    set newWindow to (create window with default profile)
    tell newWindow
        tell current session
            write text "claude-sessions '$SEARCH_QUERY'"
        end tell
    end tell
end tell
EOF

# For Terminal.app, replace the above with:
# osascript -e "tell application \"Terminal\" to do script \"claude-sessions '$SEARCH_QUERY'\""
```

Then add `~/.raycast-scripts/` as a script directory in Raycast preferences. You'll get a global hotkey to search and resume Claude sessions from anywhere.

## How it works

Claude Code stores session data as `.jsonl` files in `~/.claude/projects/`. This script:

1. **Scans** all project directories for session files
2. **Extracts** titles (custom or auto-summary), first user message, and keywords from all user messages
3. **Caches** results in `~/.claude/.sessions-unified-cache.tsv` (incremental — only reprocesses changed files)
4. **Displays** via fzf with ANSI formatting, hidden search keywords, and a preview panel

### Cache

The cache lives at `~/.claude/.sessions-unified-cache.tsv`. Delete it to force a full rebuild:

```bash
rm ~/.claude/.sessions-unified-cache.tsv
```

## Session data format

Claude Code stores sessions as `.jsonl` files with these record types:

| Type | Content |
|------|---------|
| `user` | User messages (`.message.content` — string or array of `{type, text}`) |
| `assistant` | Claude's responses |
| `summary` | Auto-generated session title |
| `custom-title` | User-set title (via `/rename`) |

The script reads `user`, `summary`, and `custom-title` records. It never modifies session files.

## Contributing

Issues and PRs welcome. This started as a personal tool — there's plenty of room for improvement:

- [ ] Configurable keybindings
- [ ] Delete/archive sessions from the picker
- [ ] Session statistics (message count, duration)
- [ ] Export sessions
- [ ] Better Linux testing
- [ ] Terminal.app / Alacritty / Warp Raycast script variants

## License

MIT
