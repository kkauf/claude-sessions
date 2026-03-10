#!/usr/bin/env python3
"""Fast session indexer for claude-sessions.

Replaces bash process_file/build_full_cache/do_incremental.
Key optimizations vs bash (27s → ~350ms):
  1. Single process — eliminates ~4,500 subprocess spawns
  2. Single-pass regex — one scan per file instead of separate grep/find passes
  3. Capped keyword parsing — JSON-parses first 50 messages, not every message
  4. Parallel processing with fork (fast worker startup)
"""

import json, multiprocessing, os, re, sys, time, warnings
from concurrent.futures import ProcessPoolExecutor

PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")
CACHE_PATH = os.path.join(os.path.expanduser("~"), ".claude", ".sessions-unified-cache.tsv")
HOME_KEY = os.path.expanduser("~").replace("/", "-").lstrip("-")

TOKEN_RE = re.compile(r'[a-z0-9][a-z0-9_-]{2,40}')

# Single compiled regex for all relevant JSONL record types.
# finditer scans the file once, yielding matches in order.
MARKER_RE = re.compile(rb'"type":"(user|assistant|summary|custom-title)"')

# For focused title search after main scan exits early
TITLE_RE = re.compile(rb'"type":"(summary|custom-title)"')

# Max messages to JSON-parse for keyword extraction per file.
KW_MSG_CAP = 50

NOISE_PREFIXES = (
    "<local-command-caveat>",
    "Caveat: The messages below",
    "[Request interrupted",
)


def project_label(dirname):
    """Derive readable project label from encoded directory name."""
    s = dirname
    prefix = f"-{HOME_KEY}-"
    if s.startswith(prefix):
        s = s[len(prefix):]
    if s.startswith("github-"):
        s = s[7:]
    for marker in ("CloudDocs-Documents-", "Documents-"):
        idx = s.rfind(marker)
        if idx >= 0:
            s = s[idx + len(marker):]
    if not s:
        s = dirname
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s or dirname


def _extract_line(data, pos):
    """Given a position inside a JSONL line, return the full line bytes."""
    ls = data.rfind(b'\n', 0, pos)
    ls = 0 if ls == -1 else ls + 1
    le = data.find(b'\n', pos)
    if le == -1:
        le = len(data)
    return data[ls:le]


def _extract_text(content):
    """Extract text from message content (string or array format)."""
    if isinstance(content, str):
        return content[:500]
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", "")[:500])
        return " ".join(parts)[:500]
    return ""


def _parse_msg_text(line):
    """JSON-parse a message line and return extracted text, or None."""
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    msg = obj.get("message")
    if not msg:
        return None
    return _extract_text(msg.get("content", ""))


def _index_one(args):
    """Process one session file. Returns (mtime, cache_line) or None.

    Single-pass regex scan finds user, assistant, summary, and custom-title
    markers in one forward pass. Exits early once keyword budget is exhausted,
    then does a focused search for title if needed.
    """
    path, sid, size, mtime, label = args

    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return None

    # Quick check: any user messages at all?
    if b'"type":"user"' not in data:
        return None

    first_user = second_user = custom_title = summary = None
    user_count = 0
    kw_chunks = []
    kw_budget = KW_MSG_CAP
    scan_end_pos = 0

    # Single-pass: find all relevant markers in one forward scan
    for m in MARKER_RE.finditer(data):
        tp = m.group(1)

        if tp == b'user':
            user_count += 1
            if user_count <= 2 or kw_budget > 0:
                line = _extract_line(data, m.start())
                text = _parse_msg_text(line)
                if user_count == 1:
                    if not text:
                        return None
                    first_user = ' '.join(text[:200].split())
                elif user_count == 2 and text:
                    second_user = ' '.join(text[:200].split())
                if text and kw_budget > 0:
                    kw_chunks.append(text.lower())
                    kw_budget -= 1

        elif tp == b'assistant':
            if kw_budget > 0:
                line = _extract_line(data, m.start())
                text = _parse_msg_text(line)
                if text:
                    kw_chunks.append(text.lower())
                    kw_budget -= 1

        elif tp == b'summary' and summary is None:
            line = _extract_line(data, m.start())
            try:
                summary = json.loads(line).get("summary") or ""
            except (json.JSONDecodeError, ValueError):
                pass

        elif tp == b'custom-title' and custom_title is None:
            line = _extract_line(data, m.start())
            try:
                custom_title = json.loads(line).get("customTitle") or ""
            except (json.JSONDecodeError, ValueError):
                pass

        # Exit early when we have enough keywords AND title metadata
        if user_count >= 2 and kw_budget <= 0:
            if summary is not None or custom_title is not None:
                break
            # Save position for focused title search below
            scan_end_pos = m.end()
            break

    # If main scan exited early without finding title, do a focused search
    if summary is None and custom_title is None and scan_end_pos > 0:
        tm = TITLE_RE.search(data, scan_end_pos)
        if tm:
            line = _extract_line(data, tm.start())
            try:
                obj = json.loads(line)
                if tm.group(1) == b'summary':
                    summary = obj.get("summary") or ""
                else:
                    custom_title = obj.get("customTitle") or ""
            except (json.JSONDecodeError, ValueError):
                pass

    if not first_user:
        return None

    # --- Filters ---
    if first_user == "Warmup":
        return None
    for p in NOISE_PREFIXES:
        if first_user.startswith(p):
            return None

    preview = first_user
    if first_user.startswith("claude --resume"):
        preview = second_user if second_user else None
    if not preview or preview.startswith("[Request interrupted") or preview.startswith("claude --resume"):
        return None

    # Title priority: custom-title > summary > plan heading
    title = custom_title or summary or ""
    if not title and first_user.startswith("Implement the following plan:"):
        plan = first_user[len("Implement the following plan:"):].strip().lstrip("# ")
        title = plan.split(" ## ")[0][:120]

    # Keywords
    kw_text = " ".join(kw_chunks)
    keywords = " ".join(sorted(set(TOKEN_RE.findall(kw_text))))

    # Sanitize for TSV
    title = title.replace('\t', ' ').replace('\n', ' ')
    preview = preview.replace('\t', ' ').replace('\n', ' ')
    epoch = int(mtime)

    return (mtime, f"{sid}\t{title}\t{preview}\t{keywords}\t{epoch}\t{label}")


def scan_all_files():
    """Scan all project dirs and return file info tuples."""
    results = []
    try:
        for proj in os.scandir(PROJECTS_DIR):
            if not proj.is_dir():
                continue
            label = project_label(proj.name)
            try:
                for entry in os.scandir(proj.path):
                    if not entry.name.endswith('.jsonl') or entry.name.startswith('agent-'):
                        continue
                    st = entry.stat()
                    results.append((entry.path, entry.name[:-6], st.st_size, st.st_mtime, label))
            except OSError:
                continue
    except OSError:
        pass
    return results


def _get_pool_context():
    """Get multiprocessing context. Prefer fork (6ms) over spawn (76ms)."""
    try:
        warnings.filterwarnings('ignore', category=DeprecationWarning)
        return multiprocessing.get_context('fork')
    except ValueError:
        return multiprocessing.get_context('spawn')


def build_full(files):
    """Full cache rebuild with parallel processing."""
    n_workers = min(os.cpu_count() or 4, 8, max(len(files), 1))

    if len(files) < 20:
        results = [_index_one(f) for f in files]
    else:
        # Sort largest files first for better load balancing
        files_sorted = sorted(files, key=lambda f: f[2], reverse=True)
        chunksize = max(1, len(files_sorted) // (n_workers * 4))
        ctx = _get_pool_context()
        with ProcessPoolExecutor(max_workers=n_workers, mp_context=ctx) as pool:
            results = list(pool.map(_index_one, files_sorted, chunksize=chunksize))

    entries = [r for r in results if r]
    entries.sort(key=lambda x: x[0], reverse=True)
    return [e[1] for e in entries]


def incremental(files):
    """Warm start: only process files newer than cache."""
    cache_mtime = os.path.getmtime(CACHE_PATH)

    changed = [(p, sid, sz, mt, lb) for p, sid, sz, mt, lb in files if mt > cache_mtime]

    if not changed:
        return None  # Cache is fresh

    if len(changed) > 20:
        return build_full(files)

    # Process changed files sequentially (few files, not worth pool overhead)
    changed_sids = set()
    new_entries = []
    for f in changed:
        changed_sids.add(f[1])
        result = _index_one(f)
        if result:
            new_entries.append(result)

    # Merge with existing cache (exclude changed sids)
    with open(CACHE_PATH, 'r') as fh:
        for line in fh:
            line = line.rstrip('\n')
            if not line:
                continue
            sid = line.split('\t', 1)[0]
            if sid not in changed_sids:
                parts = line.split('\t')
                try:
                    epoch = float(parts[4]) if len(parts) > 4 else 0
                except (ValueError, IndexError):
                    epoch = 0
                new_entries.append((epoch, line))

    new_entries.sort(key=lambda x: x[0], reverse=True)
    return [e[1] for e in new_entries]


def main():
    t0 = time.monotonic()
    timing = "--timing" in sys.argv

    if not os.path.isdir(PROJECTS_DIR):
        sys.exit(0)

    files = scan_all_files()

    if os.path.isfile(CACHE_PATH):
        result = incremental(files)
        mode = "incremental"
    else:
        result = build_full(files)
        mode = "cold"

    if result is None:
        elapsed = (time.monotonic() - t0) * 1000
        if timing:
            print(f"cache fresh, {elapsed:.0f}ms", file=sys.stderr)
        sys.exit(0)

    # Write cache atomically
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, 'w') as fh:
        for line in result:
            fh.write(line)
            fh.write('\n')
    os.replace(tmp, CACHE_PATH)

    elapsed = (time.monotonic() - t0) * 1000
    if timing:
        print(f"{mode}: {len(result)} sessions, {elapsed:.0f}ms", file=sys.stderr)


if __name__ == "__main__":
    main()
