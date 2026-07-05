#!/bin/bash
# Regression tests for resolve_resume_dir / encode_path in claude-sessions.
#
# These cover the failure modes that broke resume in production:
#   1. worktree sessions re-homed to the worktree's project folder, then the
#      worktree deleted by gc (2026-07-04)
#   2. paths with spaces / '~' (iCloud Drive) encoding differently than the
#      naive slash-and-dot substitution (2026-07-05)
#   3. transcripts stranded under legacy space-preserving folder names
#
# Runs entirely inside a mktemp sandbox — never touches ~/.claude.

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_SESSIONS_LIB=1 source "$SCRIPT_DIR/claude-sessions"

SANDBOX=$(mktemp -d)
trap 'rm -rf "$SANDBOX"' EXIT
CLAUDE_PROJECTS="$SANDBOX/projects"
mkdir -p "$CLAUDE_PROJECTS"

pass=0 fail=0
ok()  { pass=$((pass+1)); echo "  ok   - $1"; }
bad() { fail=$((fail+1)); echo "  FAIL - $1"; }
check() {
  local desc="$1" expected="$2" actual="$3"
  if [[ "$expected" == "$actual" ]]; then ok "$desc"; else bad "$desc
         expected: $expected
         got:      $actual"; fi
}

# mkses <project-folder-name> <sid> <cwd>... — fabricate a transcript
mkses() {
  local key="$1" sid="$2" c
  shift 2
  mkdir -p "$CLAUDE_PROJECTS/$key"
  : > "$CLAUDE_PROJECTS/$key/$sid.jsonl"
  for c in "$@"; do
    printf '{"cwd":"%s"}\n' "$c" >> "$CLAUDE_PROJECTS/$key/$sid.jsonl"
  done
}

echo "encode_path:"
check "plain repo path" \
  "-Users-kkaufmann-github-kaufmann-health" \
  "$(encode_path /Users/kkaufmann/github/kaufmann-health)"
check "worktree path (dot -> dash, double dash)" \
  "-Users-kkaufmann-github-kaufmann-health--claude-worktrees-portal-qa-fixes" \
  "$(encode_path "/Users/kkaufmann/github/kaufmann-health/.claude/worktrees/portal-qa-fixes")"
check "spaces and tilde (iCloud)" \
  "-Users-k-Library-Mobile-Documents-com-apple-CloudDocs-Documents-Personal-Support" \
  "$(encode_path "/Users/k/Library/Mobile Documents/com~apple~CloudDocs/Documents/Personal Support")"

echo "case: normal session, directory exists"
dir="$SANDBOX/repo"; mkdir -p "$dir"
mkses "$(encode_path "$dir")" sid-normal "$dir"
check "returns the session dir" "$dir" "$(resolve_resume_dir sid-normal)"
[[ -f "$CLAUDE_PROJECTS/$(encode_path "$dir")/sid-normal.jsonl" ]] \
  && ok "transcript not moved" || bad "transcript moved unexpectedly"

echo "case: session entered a worktree that was later deleted"
main="$SANDBOX/repo2"; wt="$main/.claude/worktrees/gone"
mkdir -p "$main"   # worktree itself deliberately NOT created
mkses "$(encode_path "$wt")" sid-wtgone "$main" "$wt"
check "falls back to surviving main repo" "$main" "$(resolve_resume_dir sid-wtgone 2>/dev/null)"
[[ -f "$CLAUDE_PROJECTS/$(encode_path "$main")/sid-wtgone.jsonl" ]] \
  && ok "transcript relocated to main repo's folder" || bad "transcript not relocated"

echo "case: path with spaces and '~' (iCloud), correctly-encoded folder"
icloud="$SANDBOX/Mobile Documents/com~apple~CloudDocs/Personal Support"
mkdir -p "$icloud"
mkses "$(encode_path "$icloud")" sid-icloud "$icloud"
check "returns the iCloud dir" "$icloud" "$(resolve_resume_dir sid-icloud)"
[[ -f "$CLAUDE_PROJECTS/$(encode_path "$icloud")/sid-icloud.jsonl" ]] \
  && ok "transcript not moved (regression: 2026-07-05 bug moved it)" \
  || bad "transcript moved out of the correct folder"

echo "case: transcript stranded under legacy space-preserving folder name"
legacy_key="${icloud//\//-}"   # slashes -> dashes only; spaces/~ preserved
mkses "$legacy_key" sid-legacy "$icloud"
check "returns the real dir" "$icloud" "$(resolve_resume_dir sid-legacy 2>/dev/null)"
[[ -f "$CLAUDE_PROJECTS/$(encode_path "$icloud")/sid-legacy.jsonl" ]] \
  && ok "transcript migrated to modern folder name" || bad "transcript not migrated"

echo "case: every recorded cwd deleted — climb to surviving ancestor"
repo3="$SANDBOX/repo3"; wt3="$repo3/.claude/worktrees/zapped"
mkdir -p "$repo3/.claude/worktrees"   # scaffolding exists, worktree doesn't
mkses "$(encode_path "$wt3")" sid-ancestor "$wt3"
check "climbs out of .claude scaffolding to repo root" "$repo3" \
  "$(resolve_resume_dir sid-ancestor 2>/dev/null)"

echo "case: stale index — no transcript anywhere"
check "returns current dir, does not error" "$PWD" "$(resolve_resume_dir sid-does-not-exist)"

echo ""
echo "$pass passed, $fail failed"
[[ $fail -eq 0 ]]
