#!/usr/bin/env bash
# Preview script for claude-sessions fzf picker.
# Called by fzf --preview with: $1=sid $2=home_key $3=query
shopt -s nullglob

sid="$1"
home_key="$2"
query="$3"

files=("$HOME/.claude/projects"/*/${sid}.jsonl)
f="${files[0]}"

if [[ -z "$f" || ! -f "$f" ]]; then
  echo "Session file not found"
  exit 0
fi

# Project name header
d=$(basename "$(dirname "$f")")
d="${d#-${home_key}-}"; d="${d#github-}"
d="${d##*CloudDocs-Documents-}"; d="${d##*Documents-}"
echo -e "\033[2m$d\033[0m"
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
