# claude-sessions

Fast session picker for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Search and resume any session across all projects with fuzzy finding, TF-IDF ranked keywords, and query-matched previews.

## Features

- **Relevance-ranked search** — when you search with arguments, Python scores results using field-weighted TF-IDF: title matches (10x) beat preview matches (8x) beat keyword matches (1x). Recency is a tiebreaker, not the primary signal.
- **Interactive browse** — no arguments opens all sessions sorted by recency with fzf's interactive search
- **TF-IDF keyword ranking** — distinctive terms (e.g., "outreach", "webhook") rank above generic ones (e.g., "feature", "update")
- **Smart extraction** — indexes all user messages + assistant "gems" (opening remarks and closing recaps), skips tool call noise
- **Query-matched preview** — right panel shows messages containing your search terms, with user (▸) and assistant (▹) distinguished. Metadata header shows project, creation date, and message count.
- **Advanced query syntax** — exact phrases (`"auth bug"`), project filter (`--project kh`), negation (`--exclude standup` or `-standup`)
- **Incremental caching** — instant startup after first run (~0.3s warm, ~1.3s cold for 370 sessions). Search scoring reads from cache in ~60ms.
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
# Browse all sessions (recency order, interactive search)
claude-sessions

# Relevance-ranked search (Python scores, fzf displays)
claude-sessions "auth bug"
claude-sessions roadmap notion database

# Advanced queries
claude-sessions '"cold email outreach"'    # exact phrase
claude-sessions "email --project kh"       # project filter
claude-sessions "email --exclude standup"  # negation
claude-sessions "email -standup"           # shorthand negation
```

**Controls:**
- Type to narrow results (within ranked results when searching, full interactive when browsing)
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

Two modes depending on whether a query is provided:

**With query** (`claude-sessions "auth bug"`): Python reads the cached TSV, scores each session with field-weighted TF-IDF, and pipes pre-ranked results to fzf with `--no-sort`. fzf is display + selection only — you can type to narrow within the ranked results.

```
Score = Σ(query_terms) IDF(term) × (10·title + 8·preview + 1·keywords)
      + phrase_proximity_bonus
      + exact_phrase_bonus
      + 0.1 × recency_normalized
```

**Without query** (`claude-sessions`): All sessions display in recency order. fzf handles interactive search with `--tiebreak=index`.

```
┌─ fzf input (2-field TSV) ──────────────────────────────────┐
│ F1: session_id (hidden)                                     │
│ F2: [date] [title/preview] [project tag] ··· [search text]  │
│                                        300 spaces padding    │
│                                        pushes keywords       │
│                                        off-screen            │
└─────────────────────────────────────────────────────────────┘
```

### Preview

The preview panel extracts text with jq first, then greps for your search terms. This means tool_use blocks, file contents, SQL queries, and other JSON noise never appear in results — only actual human-readable messages.

### Cache

The cache lives at `~/.claude/.sessions-unified-cache.tsv`. Delete it to force a full rebuild:

```bash
rm ~/.claude/.sessions-unified-cache.tsv
```

## Launch methods

### Shell alias

```bash
# Add to ~/.bashrc or ~/.zshrc
alias cs="claude-sessions"

# With a default search term
alias cs-auth="claude-sessions auth"
```

### Terminal keybinding

Bind a key combo in your terminal to launch the picker instantly.

**iTerm2** (Preferences → Keys → Key Bindings → +):
- Action: "Send Text with vim Special Chars"
- Text: `claude-sessions\n`

**Wezterm** (`~/.wezterm.lua`):
```lua
{ key = "s", mods = "CTRL|SHIFT", action = wezterm.action.SendString("claude-sessions\n") }
```

### Raycast / Alfred (macOS)

Launch from a macOS app launcher with an optional search query.

<details>
<summary>Raycast script command</summary>

Create `~/.raycast-scripts/claude-sessions.sh`:

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

Add `~/.raycast-scripts/` as a script directory in Raycast preferences.

</details>

<details>
<summary>Alfred workflow</summary>

Create a workflow with a **Keyword** input (e.g., `cs`) connected to a **Run Script** action:

```bash
osascript -e "tell application \"Terminal\" to do script \"claude-sessions '{query}'\""
```

Replace `Terminal` with `iTerm` and adjust the AppleScript if you use iTerm2 (see Raycast example above).

</details>

### Claude Code custom slash command

Add to your `~/.claude/commands/sessions.md`:

```
Find and resume a Claude Code session. Run: claude-sessions $ARGUMENTS
```

Then type `/sessions auth bug` inside Claude Code to launch the picker with a pre-filled search.

## Testing

```bash
bash test-session-tools.sh
```

47 tests covering the indexer, search scoring (field weighting, IDF, exact phrases, negation, project filter), and preview (query matching, tool_use exclusion, compacted sessions, metadata header).

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
- [x] Session statistics (message count in preview header)
- [ ] Export sessions
- [ ] Better Linux testing
- [ ] Terminal.app / Alacritty / Warp Raycast script variants

## License

MIT
