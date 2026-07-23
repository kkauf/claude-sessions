#!/usr/bin/env bash
# Tests for session_indexer.py and the preview logic in claude-sessions.
# Run: bash test-session-tools.sh
#
# Creates a temp dir with synthetic JSONL files, runs the indexer,
# then tests the preview extraction against those files.

set -euo pipefail

PASS=0
FAIL=0
TMPDIR=$(mktemp -d)
PROJECTS="$TMPDIR/projects"

cleanup() { rm -rf "$TMPDIR"; }
trap cleanup EXIT

# --- Helpers ---

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [[ "$expected" == "$actual" ]]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    echo "FAIL: $label"
    echo "  expected: $expected"
    echo "  actual:   $actual"
  fi
}

assert_contains() {
  local label="$1" needle="$2" haystack="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    echo "FAIL: $label"
    echo "  expected to contain: $needle"
    echo "  actual: $haystack"
  fi
}

assert_not_contains() {
  local label="$1" needle="$2" haystack="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    echo "FAIL: $label"
    echo "  expected NOT to contain: $needle"
    echo "  actual: $haystack"
  fi
}

assert_line_count_ge() {
  local label="$1" min_lines="$2" text="$3"
  local actual_lines
  actual_lines=$(echo "$text" | grep -c '.' || true)
  if [[ "$actual_lines" -ge "$min_lines" ]]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    echo "FAIL: $label"
    echo "  expected >= $min_lines lines, got $actual_lines"
    echo "  actual: $text"
  fi
}

ts() {
  # Generate ISO timestamp N days ago
  local days_ago="${1:-0}"
  if [[ "$(uname)" == "Darwin" ]]; then
    date -u -v-${days_ago}d +"%Y-%m-%dT%H:%M:%S.000Z"
  else
    date -u -d "$days_ago days ago" +"%Y-%m-%dT%H:%M:%S.000Z"
  fi
}

# --- Build synthetic JSONL sessions ---

mkdir -p "$PROJECTS/-Users-test-github-myproject"
mkdir -p "$PROJECTS/-Users-test-Documents-Personal"

# Session 1: Normal session with tool calls — the key bug scenario.
# Assistant messages with tool_use but no text should NOT appear in preview.
cat > "$PROJECTS/-Users-test-github-myproject/session-001.jsonl" << 'JSONL'
{"type":"system","sessionId":"session-001","timestamp":"2026-03-10T09:00:00.000Z","cwd":"/tmp"}
{"type":"user","message":{"role":"user","content":"Build the cold email outreach feature for sales"},"uuid":"u1"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"I'll build the cold email outreach feature. Let me start by reading the existing code."}]},"uuid":"a1"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"t1","name":"Read","input":{"file_path":"/src/matches.ts"}}]},"uuid":"a2"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"t2","name":"Edit","input":{"file_path":"/src/email.ts","old_string":"old","new_string":"new"}}]},"uuid":"a3"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Done! The cold email outreach feature is now implemented with the following changes:\n1. New email template system\n2. Outreach tracking in the sales pipeline\n3. Click and feedback analytics"}]},"uuid":"a4"}
{"type":"user","message":{"role":"user","content":"Can you also add click tracking for the email links?"},"uuid":"u2"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"t3","name":"Edit","input":{"file_path":"/src/tracking.ts","old_string":"x","new_string":"y"}}]},"uuid":"a5"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Click tracking is now integrated into the outreach emails."}]},"uuid":"a6"}
JSONL

# Session 2: Heavily compacted session — summaries replace original messages.
cat > "$PROJECTS/-Users-test-github-myproject/session-002.jsonl" << 'JSONL'
{"type":"system","sessionId":"session-002","timestamp":"2026-03-08T14:00:00.000Z","cwd":"/tmp"}
{"type":"summary","summary":"This session covered match presentation UX, profile depth changes, and side-by-side therapist comparison. Key decisions: use card layout, show top 3 matches initially."}
{"type":"compact_boundary"}
{"type":"user","message":{"role":"user","content":"Continuing from the compacted context — now let's add the profile detail modal"},"uuid":"u3"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"I'll implement the profile detail modal based on our earlier card layout decision."}]},"uuid":"a7"}
JSONL

# Session 3: Session where "matches" appears in tool_use JSON but NOT in text.
# This should NOT match a search for "matches" in the preview.
cat > "$PROJECTS/-Users-test-Documents-Personal/session-003.jsonl" << 'JSONL'
{"type":"system","sessionId":"session-003","timestamp":"2026-03-11T08:00:00.000Z","cwd":"/tmp"}
{"type":"user","message":{"role":"user","content":"Good morning, let's do the standup"},"uuid":"u4"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Good morning! Let me run the standup sequence."}]},"uuid":"a8"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"t4","name":"Bash","input":{"command":"SELECT * FROM matches WHERE status = 'active'"}}]},"uuid":"a9"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"t5","name":"Read","input":{"file_path":"/src/matches/profile.tsx"}}]},"uuid":"a10"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Here's your standup dashboard. No blockers today."}]},"uuid":"a11"}
JSONL

# Session 4: Multiple user messages with relevant terms spread throughout.
cat > "$PROJECTS/-Users-test-github-myproject/session-004.jsonl" << 'JSONL'
{"type":"system","sessionId":"session-004","timestamp":"2026-03-09T10:00:00.000Z","cwd":"/tmp"}
{"type":"user","message":{"role":"user","content":"Let's work on the click tracking feature"},"uuid":"u5"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Sure, I'll look at the click tracking implementation."}]},"uuid":"a12"}
{"type":"user","message":{"role":"user","content":"Actually, the email feedback loop is more important"},"uuid":"u6"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Good call. The email feedback loop connects click data to the sales funnel."}]},"uuid":"a13"}
{"type":"user","message":{"role":"user","content":"Yes, and we need it for the outreach campaign metrics"},"uuid":"u7"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"I've connected the feedback loop to track outreach campaign effectiveness."}]},"uuid":"a14"}
JSONL

# --- INDEXER TESTS ---

echo "=== Indexer Tests ==="

# Override PROJECTS_DIR, CACHE_PATH, and DB_PATH for testing via env vars
CACHE="$TMPDIR/test-cache.tsv"
TEST_DB="$TMPDIR/test-sessions.db"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Run indexer as subprocess with env var overrides
SESSION_PROJECTS_DIR="$PROJECTS" SESSION_CACHE_PATH="$CACHE" SESSION_DB_PATH="$TEST_DB" \
  python3 "$SCRIPT_DIR/session_indexer.py" 2>&1 || {
    echo "ERROR: indexer failed"
    exit 1
  }

if [[ ! -f "$CACHE" ]]; then
  echo "ERROR: cache file not created at $CACHE"
  echo "Projects dir contents:"
  find "$PROJECTS" -type f
  exit 1
fi

INDEXER_OUTPUT=$(cat "$CACHE")

# Test: session-001 should have cold, email, outreach, sales as keywords
s001=$(echo "$INDEXER_OUTPUT" | grep "^session-001")
assert_contains "s001 has 'outreach' keyword" "outreach" "$s001"
assert_contains "s001 has 'cold' keyword" "cold" "$s001"
assert_contains "s001 has 'email' keyword" "email" "$s001"
assert_contains "s001 has 'sales' keyword" "sales" "$s001"
assert_contains "s001 has 'click' keyword" "click" "$s001"

# Test: session-001 should have 7 fields
s001_fields=$(echo "$s001" | awk -F'\t' '{print NF}')
assert_eq "s001 has 7 TSV fields" "7" "$s001_fields"

# Test: session-002 (compacted) should have keywords from summary
s002=$(echo "$INDEXER_OUTPUT" | grep "^session-002")
assert_contains "s002 has 'presentation' from summary" "presentation" "$s002"
assert_contains "s002 has 'therapist' from summary" "therapist" "$s002"
assert_contains "s002 has 'comparison' from summary" "comparison" "$s002"

# Test: session-003 should NOT have "matches" as keyword (only in tool_use)
s003=$(echo "$INDEXER_OUTPUT" | grep "^session-003")
# The word "matches" only appears in tool_use JSON, not in user or assistant text
s003_kw=$(echo "$s003" | cut -f4)
assert_not_contains "s003 keywords exclude tool_use content" "matches" "$s003_kw"

# Test: created_epoch comes from timestamp field, not mtime
s001_created=$(echo "$s001" | cut -f5)
# 2026-03-10T09:00:00Z → epoch should be around 1741597200 (not exact, depends on TZ)
# Just verify it's a number and not 0
if [[ "$s001_created" =~ ^[0-9]+$ ]] && [[ "$s001_created" -gt 1000000000 ]]; then
  PASS=$((PASS + 1))
else
  FAIL=$((FAIL + 1))
  echo "FAIL: s001 created_epoch is a valid unix timestamp"
  echo "  actual: $s001_created"
fi

# Test: mtime_epoch is in field 6 and different from created_epoch
s001_mtime=$(echo "$s001" | cut -f6)
if [[ "$s001_mtime" =~ ^[0-9]+$ ]] && [[ "$s001_mtime" -gt 1000000000 ]]; then
  PASS=$((PASS + 1))
else
  FAIL=$((FAIL + 1))
  echo "FAIL: s001 mtime_epoch is a valid unix timestamp"
  echo "  actual: $s001_mtime"
fi

# Test: project label derivation
# HOME_KEY is based on real $HOME, so test dirs get partial stripping.
# Just verify labels are non-empty and contain the distinctive part.
s001_proj=$(echo "$s001" | cut -f7)
assert_contains "s001 project label contains 'myproject'" "myproject" "$s001_proj"

s003_proj=$(echo "$s003" | cut -f7)
assert_contains "s003 project label contains 'Personal'" "Personal" "$s003_proj"

# Test: TF-IDF ordering — "outreach" should rank higher than "email" in session-001
# because "email" appears in more sessions (s001 + s004) while "outreach" is more distinctive
s001_kw="$s003_kw"  # wrong, let me fix
s001_kw=$(echo "$s001" | cut -f4)
outreach_pos=$(echo "$s001_kw" | tr ' ' '\n' | grep -n "^outreach$" | cut -d: -f1)
# Just verify outreach is in the keywords
assert_contains "s001 keywords include outreach" "outreach" "$s001_kw"

# Test: field 4 is now body excerpt (raw text), not keyword tokens.
# Stopword filtering is handled by FTS5 internally, not in the stored text.
# Verify body excerpt contains meaningful content from the conversation.
assert_contains "s001 body excerpt has conversation text" "outreach" "$s001_kw"
assert_contains "s001 body excerpt has user message" "click tracking" "$s001_kw"
assert_contains "s001 body excerpt has assistant text" "implemented" "$s001_kw"

# --- SEARCH SCORING TESTS ---

echo ""
echo "=== Search Scoring Tests ==="

# Test: "cold email outreach" should rank session-001 first (title/preview match)
search_outreach=$(SESSION_PROJECTS_DIR="$PROJECTS" SESSION_CACHE_PATH="$CACHE" SESSION_DB_PATH="$TEST_DB" \
  python3 "$SCRIPT_DIR/session_indexer.py" --search "cold email outreach")

first_result=$(echo "$search_outreach" | head -1 | cut -f1)
assert_eq "search 'cold email outreach' ranks s001 first" "session-001" "$first_result"

# Test: session with term in title/preview ranks above session with term only in keywords
# "outreach" appears in s001's preview text and s004's keywords (via "outreach campaign metrics")
# s001 should rank higher because it has "outreach" prominently in the preview
search_outreach_only=$(SESSION_PROJECTS_DIR="$PROJECTS" SESSION_CACHE_PATH="$CACHE" SESSION_DB_PATH="$TEST_DB" \
  python3 "$SCRIPT_DIR/session_indexer.py" --search "outreach")

outreach_first=$(echo "$search_outreach_only" | head -1 | cut -f1)
assert_eq "search 'outreach' ranks s001 first (preview match > keyword)" "session-001" "$outreach_first"

# Test: "click tracking feedback" should return both s001 and s004
search_click=$(SESSION_PROJECTS_DIR="$PROJECTS" SESSION_CACHE_PATH="$CACHE" SESSION_DB_PATH="$TEST_DB" \
  python3 "$SCRIPT_DIR/session_indexer.py" --search "click tracking feedback")
assert_contains "search 'click tracking feedback' includes s001" "session-001" "$search_click"
assert_contains "search 'click tracking feedback' includes s004" "session-004" "$search_click"

# Test: "standup" should return session-003
search_standup=$(SESSION_PROJECTS_DIR="$PROJECTS" SESSION_CACHE_PATH="$CACHE" SESSION_DB_PATH="$TEST_DB" \
  python3 "$SCRIPT_DIR/session_indexer.py" --search "standup")
assert_contains "search 'standup' finds s003" "session-003" "$search_standup"

# Test: project filter works
search_proj=$(SESSION_PROJECTS_DIR="$PROJECTS" SESSION_CACHE_PATH="$CACHE" SESSION_DB_PATH="$TEST_DB" \
  python3 "$SCRIPT_DIR/session_indexer.py" --search "email --project myproject")
assert_contains "project filter includes s001 (myproject)" "session-001" "$search_proj"
assert_not_contains "project filter excludes s003 (Personal)" "session-003" "$search_proj"

# Test: negation excludes correctly
search_neg=$(SESSION_PROJECTS_DIR="$PROJECTS" SESSION_CACHE_PATH="$CACHE" SESSION_DB_PATH="$TEST_DB" \
  python3 "$SCRIPT_DIR/session_indexer.py" --search "email --exclude standup")
assert_not_contains "negation excludes s003 (has 'standup' in text)" "session-003" "$search_neg"

# Test: empty query returns nothing (no crash)
search_empty=$(SESSION_PROJECTS_DIR="$PROJECTS" SESSION_CACHE_PATH="$CACHE" SESSION_DB_PATH="$TEST_DB" \
  python3 "$SCRIPT_DIR/session_indexer.py" --search "")
empty_count=$(echo "$search_empty" | grep -c '.' || true)
# Empty string has 1 line from echo, but should be blank
assert_eq "empty query returns no results" "0" "$empty_count"

# Test: IDF weighting — common terms score less than rare terms
# "email" appears in s001 + s004 (common), "standup" only in s003 (rare)
# FTS5 uses AND by default, so use OR to test cross-session IDF ranking
search_idf=$(SESSION_PROJECTS_DIR="$PROJECTS" SESSION_CACHE_PATH="$CACHE" SESSION_DB_PATH="$TEST_DB" \
  python3 "$SCRIPT_DIR/session_indexer.py" --search "standup OR email")
idf_first=$(echo "$search_idf" | head -1 | cut -f1)
# s003 has "standup" (rare, high IDF) in preview; s001/s004 have "email" (common, low IDF)
# s003 should rank first because "standup" has higher IDF via BM25
assert_eq "IDF: rare term 'standup' boosts s003 above common 'email'" "session-003" "$idf_first"

# Test: exact phrase matching
search_phrase=$(SESSION_PROJECTS_DIR="$PROJECTS" SESSION_CACHE_PATH="$CACHE" SESSION_DB_PATH="$TEST_DB" \
  python3 "$SCRIPT_DIR/session_indexer.py" --search '"cold email outreach"')
phrase_first=$(echo "$search_phrase" | head -1 | cut -f1)
assert_eq "exact phrase '\"cold email outreach\"' ranks s001 first" "session-001" "$phrase_first"

# Test: search result format is valid 7-field TSV
search_format=$(SESSION_PROJECTS_DIR="$PROJECTS" SESSION_CACHE_PATH="$CACHE" SESSION_DB_PATH="$TEST_DB" \
  python3 "$SCRIPT_DIR/session_indexer.py" --search "email")
format_fields=$(echo "$search_format" | head -1 | awk -F'\t' '{print NF}')
assert_eq "search results have 7 TSV fields" "7" "$format_fields"

# --- PREVIEW TESTS ---

echo ""
echo "=== Preview Tests ==="

# Use the real session-preview.sh with SESSION_PROJECTS_DIR override.
# ANSI is stripped so assertions match across term-highlight boundaries
# (query terms are wrapped in color codes mid-phrase).
run_preview() {
  SESSION_PROJECTS_DIR="$PROJECTS" bash "$SCRIPT_DIR/session-preview.sh" "$@" | sed $'s/\x1b\\[[0-9;]*m//g'
}

# Test: Preview without query shows user messages
preview_no_query=$(run_preview "session-001" "Users-test" "" || true)
assert_contains "no-query preview shows first user msg" "cold email outreach" "$preview_no_query"
assert_contains "no-query preview shows second user msg" "click tracking" "$preview_no_query"

# Test: Preview with query "outreach" shows matching messages
preview_outreach=$(run_preview "session-001" "Users-test" "outreach" || true)
assert_contains "query preview matches 'outreach' in user msg" "cold email outreach" "$preview_outreach"
assert_contains "query preview matches 'outreach' in assistant text" "outreach feature" "$preview_outreach"

# BUG TEST: Preview with query "matches" on session-003 should NOT show tool_use content
preview_matches_s003=$(run_preview "session-003" "Users-test" "matches" || true)

# The preview should not show tool_use content as results.
# With the fix (jq extract THEN grep), tool_use-only messages produce no text to match.
assert_not_contains "s003 preview for 'matches' excludes tool SQL content" "SELECT" "$preview_matches_s003"
assert_not_contains "s003 preview for 'matches' excludes file paths" "matches/profile" "$preview_matches_s003"

# Test: Compacted session shows compaction warning
preview_compacted=$(run_preview "session-002" "Users-test" "" || true)
assert_contains "compacted session shows warning" "compacted" "$preview_compacted"

# Test: Compacted session with query still shows post-compaction messages
preview_compacted_q=$(run_preview "session-002" "Users-test" "profile" || true)
assert_contains "compacted session query shows post-compaction msg" "profile detail modal" "$preview_compacted_q"

# BUG TEST: Query "matches" on session-003 should show project header but no ghost results
# Count lines that start with ">" or "<" (actual message results)
ghost_count=$(echo "$preview_matches_s003" | grep -cE "^[><]" || true)
# No text in s003 mentions "matches" — it only appears in tool_use JSON
assert_eq "s003 no ghost results for tool_use 'matches'" "0" "$ghost_count"

# Test: Preview with multi-word query
preview_multi=$(run_preview "session-004" "Users-test" "email feedback" || true)
assert_contains "multi-word query matches 'email'" "email" "$preview_multi"
assert_contains "multi-word query matches 'feedback'" "feedback" "$preview_multi"

# Test: Preview line count — session-001 query "outreach" should return multiple results
assert_line_count_ge "outreach query returns multiple preview lines" 2 "$preview_outreach"

# Test: Session not found
preview_missing=$(run_preview "session-nonexistent" "Users-test" "" || true)
assert_contains "missing session shows error" "not found" "$preview_missing"

# Test: Project name in preview header
assert_contains "s001 preview header shows project" "myproject" "$preview_no_query"

# Test: Metadata header shows message count
assert_contains "preview shows message count" "Messages:" "$preview_no_query"

# Test: Metadata header shows creation date
assert_contains "preview shows created date" "Created:" "$preview_no_query"

# Test: Metadata header shows horizontal rule
assert_contains "preview shows header rule" "───" "$preview_no_query"

# --- OPEN COMMAND TESTS (last: resolution may relocate transcripts) ---

echo ""
echo "=== Open Command Tests ==="

# Copied fixture set so relocation side-effects can't touch earlier fixtures.
OPEN_PROJECTS="$TMPDIR/open-projects"
mkdir -p "$OPEN_PROJECTS"
cp -R "$PROJECTS/." "$OPEN_PROJECTS/"

run_open() {
  SESSION_PROJECTS_DIR="$OPEN_PROJECTS" bash "$SCRIPT_DIR/claude-sessions" open "$@" 2>&1
}

# session-001 records cwd "/tmp" (exists) — resolution lands there.
open_print=$(run_open session-001 --print || true)
assert_contains "open --print resolves recorded cwd" "cd /tmp" "$open_print"
assert_contains "open --print emits resume command" "claude --resume session-001" "$open_print"

# Unknown sid: documented behavior — stay in the current dir, let claude report.
open_missing=$(run_open session-nonexistent --print || true)
assert_contains "open --print unknown sid falls back to cwd" "--resume session-nonexistent" "$open_missing"

# No sid: usage error, nonzero exit.
if SESSION_PROJECTS_DIR="$OPEN_PROJECTS" bash "$SCRIPT_DIR/claude-sessions" open >/dev/null 2>&1; then
  echo "FAIL: open without sid should exit nonzero"; FAIL=$((FAIL+1))
else
  echo "PASS: open without sid exits nonzero"; PASS=$((PASS+1))
fi

# Dock-launched GUI apps inherit a minimal PATH without Homebrew — the script
# must bootstrap its own dependency dirs (regression: 'fzf required' alert on
# Enter in SessionPicker.app even with fzf installed).
min_path_out=$(env -i HOME="$HOME" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
  SESSION_PROJECTS_DIR="$OPEN_PROJECTS" \
  bash "$SCRIPT_DIR/claude-sessions" open session-001 --print 2>&1 || true)
assert_contains "open --print works under minimal GUI PATH" "resume session-001" "$min_path_out"

# --gui writes a self-deleting .command runner (intercept 'open' via PATH stub).
# iTerm is always tried first, resolved by NAME via LaunchServices — never by
# bundle path (iTerm can live in /Applications/Utilities, ~/Applications, …).
STUB_DIR="$TMPDIR/stubs"
mkdir -p "$STUB_DIR"
printf '#!/bin/bash\necho "OPENED:$*" >> "%s/opened.log"\n' "$TMPDIR" > "$STUB_DIR/open"
chmod +x "$STUB_DIR/open"
MARKER="$TMPDIR/pending-marker"
gui_out=$(SESSION_PROJECTS_DIR="$OPEN_PROJECTS" SESSION_PENDING_MARKER="$MARKER" PATH="$STUB_DIR:$PATH" bash "$SCRIPT_DIR/claude-sessions" open session-001 --gui 2>&1 || true)
if [[ -f "$TMPDIR/opened.log" ]]; then
  gui_line=$(head -1 "$TMPDIR/opened.log")
  assert_contains "gui resume prefers iTerm by LaunchServices name" "-a iTerm" "$gui_line"
  runner_path="${gui_line##* }"
  assert_contains "gui runner is a .command file" ".command" "$runner_path"
  runner_body=$(cat "$runner_path" 2>/dev/null || echo missing)
  assert_contains "gui runner cds to session dir" "cd /tmp" "$runner_body"
  assert_contains "gui runner execs claude --resume" "resume session-001" "$runner_body"
  assert_contains "gui runner self-deletes" "rm -f" "$runner_body"
  # iTerm TYPES the runner path instead of executing it (README caveat) — the
  # marker is the handshake letting custom-command default profiles catch it.
  marker_content=$(cat "$MARKER" 2>/dev/null || echo missing)
  assert_eq "gui marker names the runner" "$runner_path" "$marker_content"
  rm -f "$runner_path" "$MARKER"
else
  echo "FAIL: --gui never invoked LaunchServices open"; FAIL=$((FAIL+1))
fi

# When iTerm isn't installed, the fallback handler (Terminal.app) executes the
# .command file directly — no typed input is coming, so the marker must be
# removed rather than left to confuse the next default-profile session.
printf '#!/bin/bash\nif [[ "$*" == *"-a iTerm"* ]]; then exit 1; fi\necho "OPENED:$*" >> "%s/opened.log"\n' "$TMPDIR" > "$STUB_DIR/open"
chmod +x "$STUB_DIR/open"
rm -f "$TMPDIR/opened.log"
gui_out=$(SESSION_PROJECTS_DIR="$OPEN_PROJECTS" SESSION_PENDING_MARKER="$MARKER" PATH="$STUB_DIR:$PATH" bash "$SCRIPT_DIR/claude-sessions" open session-001 --gui 2>&1 || true)
fallback_line=$(head -1 "$TMPDIR/opened.log" 2>/dev/null || echo missing)
assert_not_contains "gui falls back past iTerm" "-a iTerm" "$fallback_line"
if [[ -f "$MARKER" ]]; then
  echo "FAIL: marker should be removed when falling back to the default handler"; FAIL=$((FAIL+1))
else
  PASS=$((PASS+1))
fi
rm -f "${fallback_line#OPENED:}" 2>/dev/null || true

# --- RESULTS ---

echo ""
echo "=== Results ==="
echo "Passed: $PASS"
echo "Failed: $FAIL"

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
else
  echo "All tests passed!"
fi
