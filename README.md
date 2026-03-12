# claude-sessions

Fast session picker for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Search and resume any session across all projects with fuzzy finding, TF-IDF ranked keywords, and query-matched previews.

## Features

- **Cross-project search** — find sessions from any project, not just the current one
- **TF-IDF keyword ranking** — distinctive terms (e.g., "outreach", "webhook") rank above generic ones (e.g., "feature", "update")
- **Smart extraction** — indexes all user messages + assistant "gems" (opening remarks and closing recaps), skips tool call noise
- **Query-matched preview** — right panel shows messages containing your search terms, with user (▸) and assistant (▹) distinguished
- **Recency-first** — recently active sessions rank higher; date shows when session was created
- **Incremental caching** — instant startup after first run (~0.3s warm, ~1.3s cold for 370 sessions)
- **Cross-project tags** — sessions from other projects show a dim project label
- **Relative dates** — "today", "1d", "2w", "3mo"

## Requirements

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (the CLI)
- [fzf](https://github.com/junegunn/fzf) >= 0.40
- [jq](https://github.com/jqlang/jq) >= 1.6
- Python 3.8+
- bash >= 4.0
- macOS or Linux

## Install

```bash
# Clone and symlink to PATH
git clone https://github.com/kkauf/claude-sessions.git
ln -s "$(pwd)/claude-sessions/claude-sessions" /usr/local/bin/claude-sessions

# Or just copy both files (script + indexer)
curl -o /usr/local/bin/claude-sessions https://raw.githubusercontent.com/kkauf/claude-sessions/main/claude-sessions
curl -o /usr/local/bin/session-indexer.py https://raw.githubusercontent.com/kkauf/claude-sessions/main/session-indexer.py
chmod +x /usr/local/bin/claude-sessions
```

The indexer (`session-indexer.py`) must be in the same directory as the main script, or on `$PATH`.

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

## How it works

Claude Code stores session data as `.jsonl` files in `~/.claude/projects/`. This tool:

1. **Scans** all project directories for session files
2. **Extracts** keywords using smart extraction: all user messages + assistant text blocks (first and last per response — the "gems"). Skips tool calls, file reads, and other noise that makes up ~80% of assistant output.
3. **Ranks keywords** by TF-IDF: term frequency in the session × inverse document frequency across all sessions. Keywords unique to a session (like a client name) rank above words that appear everywhere (like "code").
4. **Caches** as a 7-field TSV (`~/.claude/.sessions-unified-cache.tsv`) sorted by last-modified time. Incremental updates — only reprocesses changed files.
5. **Displays** via fzf with recency-first ranking, ANSI formatting, and a preview panel that shows query-matched messages (not raw JSON).

### Search architecture

```
┌─ fzf input (2-field TSV) ──────────────────────────────────┐
│ F1: session_id (hidden)                                     │
│ F2: [date] [title/preview] [project tag] ··· [search text]  │
│                                        300 spaces padding    │
│                                        pushes keywords       │
│                                        off-screen            │
└─────────────────────────────────────────────────────────────┘

Search text order: title → top 15 TF-IDF keywords → all keywords → preview → project
Tiebreak: index (= recency, since cache is sorted by mtime)
```

### Preview

The preview panel extracts text with jq first, then greps for your search terms. This means tool_use blocks, file contents, SQL queries, and other JSON noise never appear in results — only actual human-readable messages.

### Cache

The cache lives at `~/.claude/.sessions-unified-cache.tsv`. Delete it to force a full rebuild:

```bash
rm ~/.claude/.sessions-unified-cache.tsv
```

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

Then add `~/.raycast-scripts/` as a script directory in Raycast preferences.

## Testing

```bash
bash test-session-tools.sh
```

32 tests covering the indexer (TF-IDF, stopwords, timestamps, project labels) and preview (query matching, tool_use exclusion, compacted sessions).

## Session data format

Claude Code stores sessions as `.jsonl` files with these record types:

| Type | Content |
|------|---------|
| `user` | User messages (`.message.content` — string or array of `{type, text}`) |
| `assistant` | Claude's responses (text blocks, tool_use blocks, or both) |
| `summary` | Auto-generated session summary (from compaction) |
| `custom-title` | User-set title (via `/rename`) |
| `compact_boundary` | Marks where older messages were compacted into summaries |

The indexer reads all record types for keywords. It never modifies session files.

## Contributing

Issues and PRs welcome.

- [ ] Configurable keybindings
- [ ] Delete/archive sessions from the picker
- [ ] Session statistics (message count, duration)
- [ ] Export sessions
- [ ] Better Linux testing
- [ ] Terminal.app / Alacritty / Warp Raycast script variants

## License

MIT
