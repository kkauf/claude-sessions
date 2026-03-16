#!/usr/bin/env bash
# Preview script for claude-sessions fzf picker.
# Called by fzf --preview with: $1=sid $2=home_key $3=query
shopt -s nullglob

sid="$1"
home_key="$2"
query="$3"

# Support override for testing
projects_dir="${SESSION_PROJECTS_DIR:-$HOME/.claude/projects}"
files=("$projects_dir"/*/${sid}.jsonl)
f="${files[0]}"

if [[ -z "$f" || ! -f "$f" ]]; then
  echo "Session file not found"
  exit 0
fi

# --- Metadata header ---
d=$(basename "$(dirname "$f")")
d="${d#-${home_key}-}"; d="${d#github-}"
d="${d##*CloudDocs-Documents-}"; d="${d##*Documents-}"

# Message count (user + assistant, single pass)
msg_count=$(grep -cE '"type":"(user|assistant)"' "$f" 2>/dev/null) || msg_count=0

# Created date from first line timestamp
created_str=""
first_ts=$(head -1 "$f" | grep -oE '"timestamp":"[^"]+"' | head -1 | cut -d'"' -f4)
if [[ -n "$first_ts" ]]; then
  # Parse ISO timestamp to readable date
  if [[ "$(uname)" == "Darwin" ]]; then
    # macOS: convert ISO to epoch then format
    epoch=$(date -j -f "%Y-%m-%dT%H:%M:%S" "${first_ts%%.*}" +%s 2>/dev/null) || epoch=""
    if [[ -n "$epoch" ]]; then
      today_start=$(date -j -v0H -v0M -v0S +%s 2>/dev/null)
      if [[ "$epoch" -ge "$today_start" ]]; then
        created_str="today"
      else
        created_str=$(date -j -r "$epoch" "+%b %d, %Y" 2>/dev/null)
      fi
    fi
  else
    created_str=$(date -d "${first_ts}" "+%b %d, %Y" 2>/dev/null) || created_str=""
  fi
fi

# Header line: project · branch (if available)
echo -e "\033[2m─── $d ─────────────────────\033[0m"
[[ -n "$created_str" ]] && echo -e "\033[2mCreated: $created_str\033[0m"
echo -e "\033[2mMessages: $msg_count\033[0m"
echo ""

# Compaction warning
compact_n=$(grep -c '"compact_boundary"' "$f" 2>/dev/null) || compact_n=0
if [[ "$compact_n" -gt 0 ]]; then
  echo -e "\033[33m⚠ ${compact_n}x compacted — long session\033[0m"
  echo ""
fi

# jq filter: extract text from user/assistant messages, prefix with [U]/[A]
jq_text='
  select(.message.content) |
  .type as $t |
  .message.content |
  (if type == "string" then .
   elif type == "array" then
     [.[] | select(.type == "text") | .text] | join(" ")
   else "" end) |
  select(length > 0) |
  gsub("\n"; " ") |
  if $t == "user" then "[U] " + .[0:300]
  else "[A] " + .[0:300] end'

if [[ -n "$query" ]]; then
  pat=$(echo "$query" | tr " " "\n" | grep -v "^$" | paste -sd "|" -)
  if [[ -n "$pat" ]]; then
    grep -E '"type":"(user|assistant)"' "$f" 2>/dev/null | \
      jq -r "$jq_text" 2>/dev/null | \
      grep -iE "$pat" | head -15 | while IFS= read -r line; do
      role="${line:0:3}"
      text="${line:4}"
      if [[ "$role" == "[U]" ]]; then
        echo -e "\033[32m▸\033[0m $text" && echo
      else
        echo -e "\033[36m▹\033[0m $text" && echo
      fi
    done
  fi
else
  grep '"type":"user"' "$f" 2>/dev/null | head -12 | jq -r "$jq_text" 2>/dev/null | \
    while IFS= read -r line; do
    text="${line:4}"
    [[ -n "$text" ]] && echo -e "\033[36m>\033[0m $text" && echo
  done
fi
