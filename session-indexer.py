#!/usr/bin/env python3
"""Fast session indexer for claude-sessions.

Replaces bash process_file/build_full_cache/do_incremental.
Key design choices:
  1. Single process — eliminates ~4,500 subprocess spawns
  2. Single-pass regex — one scan per file instead of separate grep/find passes
  3. Smart extraction — all user text + assistant "gems" (first/last text blocks)
  4. TF-IDF keyword ordering — distinctive keywords rank above generic ones
  5. Parallel processing with fork (fast worker startup)
"""

import json, math, multiprocessing, os, re, sys, time, warnings
from datetime import datetime, timezone
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

PROJECTS_DIR = os.environ.get("SESSION_PROJECTS_DIR",
    os.path.join(os.path.expanduser("~"), ".claude", "projects"))
CACHE_PATH = os.environ.get("SESSION_CACHE_PATH",
    os.path.join(os.path.expanduser("~"), ".claude", ".sessions-unified-cache.tsv"))
HOME_KEY = os.path.expanduser("~").replace("/", "-").lstrip("-")

TOKEN_RE = re.compile(r'[a-z0-9][a-z0-9_-]{2,40}')

# Single compiled regex for all relevant JSONL record types.
# finditer scans the file once, yielding matches in order.
MARKER_RE = re.compile(rb'"type":"(user|assistant|summary|custom-title)"')

# Stopwords to exclude from keyword index (matches the bash query filter)
STOP_WORDS = frozenset(
    'the and are not was for that this with from but have has had been '
    'will would could should into your they them their what when where '
    'which does also just than then each only very here there some other '
    'about more over such you all now let can may must got get gets '
    'its who how why yet nor were did done being having shall might '
    'both every most these those'.split()
)

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


def _extract_gems(line):
    """Extract keyword-rich text from an assistant message.

    Claude's responses follow: opening remark → tool calls → closing summary.
    The first and last text blocks carry the signal; tool_use blocks and
    intermediate status updates are noise for keyword purposes.
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    msg = obj.get("message")
    if not msg:
        return None
    content = msg.get("content", "")
    if isinstance(content, str):
        return content[:300]
    if isinstance(content, list):
        text_blocks = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text", "")
                if t and len(t) > 2:
                    text_blocks.append(t)
        if not text_blocks:
            return None
        if len(text_blocks) == 1:
            return text_blocks[0][:300]
        # First (acknowledgment) + last (recap) = the gems
        return text_blocks[0][:200] + " " + text_blocks[-1][:200]
    return None


def _extract_one(args):
    """Extract session data. Returns (mtime, sid, title, preview, kw_counter, epoch, label) or None.

    Full-scan: extracts keywords from ALL user messages and assistant
    "gems" (first + last text blocks per response). Returns raw Counter
    for TF-IDF scoring in a later pass.
    """
    path, sid, size, mtime, label = args

    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return None

    if b'"type":"user"' not in data:
        return None

    first_user = second_user = custom_title = summary = None
    user_count = 0
    kw_counter = Counter()

    for m in MARKER_RE.finditer(data):
        tp = m.group(1)

        if tp == b'user':
            user_count += 1
            line = _extract_line(data, m.start())
            text = _parse_msg_text(line)
            if user_count == 1:
                if not text:
                    return None
                first_user = ' '.join(text[:200].split())
            elif user_count == 2 and text:
                second_user = ' '.join(text[:200].split())
            # Index ALL user messages — short and high-signal
            if text:
                kw_counter.update(t for t in TOKEN_RE.findall(text.lower()) if t not in STOP_WORDS)

        elif tp == b'assistant':
            line = _extract_line(data, m.start())
            # Skip assistant messages with no text (pure tool calls)
            if b'"type":"text"' not in line:
                continue
            gems = _extract_gems(line)
            if gems:
                kw_counter.update(t for t in TOKEN_RE.findall(gems.lower()) if t not in STOP_WORDS)

        elif tp == b'summary':
            line = _extract_line(data, m.start())
            try:
                obj = json.loads(line)
                s = obj.get("summary") or ""
                if summary is None:
                    summary = s
                # Keywords from summaries cover compacted content
                if s:
                    kw_counter.update(t for t in TOKEN_RE.findall(s.lower()[:500]) if t not in STOP_WORDS)
            except (json.JSONDecodeError, ValueError):
                pass

        elif tp == b'custom-title' and custom_title is None:
            line = _extract_line(data, m.start())
            try:
                custom_title = json.loads(line).get("customTitle") or ""
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

    # Sanitize for TSV
    title = title.replace('\t', ' ').replace('\n', ' ')
    preview = preview.replace('\t', ' ').replace('\n', ' ')

    # Extract creation timestamp from first line (for display date)
    # Use mtime for ranking (recency = last activity)
    created_epoch = int(mtime)  # fallback
    first_nl = data.find(b'\n')
    first_line = data[:first_nl] if first_nl > 0 else data
    ts_match = re.search(rb'"timestamp":"([^"]+)"', first_line)
    if ts_match:
        try:
            dt = datetime.fromisoformat(ts_match.group(1).decode().replace('Z', '+00:00'))
            created_epoch = int(dt.timestamp())
        except (ValueError, OSError):
            pass

    return (mtime, sid, title, preview, kw_counter, created_epoch, int(mtime), label)


def _tfidf_sort(kw_counter, df, n_docs):
    """Sort keywords by TF-IDF score descending, alphabetical tiebreak."""
    scores = {}
    for kw, tf in kw_counter.items():
        idf = math.log(n_docs / max(df.get(kw, 0), 1))
        scores[kw] = tf * idf
    return sorted(scores, key=lambda k: (-scores[k], k))


def _format_entry(session_data, df, n_docs):
    """Format a session's extracted data into a cache line with TF-IDF keyword ordering."""
    mtime, sid, title, preview, kw_counter, created_epoch, mtime_epoch, label = session_data
    kw_sorted = _tfidf_sort(kw_counter, df, n_docs)
    keywords = " ".join(kw_sorted)
    return (mtime, f"{sid}\t{title}\t{preview}\t{keywords}\t{created_epoch}\t{mtime_epoch}\t{label}")


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
    """Full cache rebuild: parallel extraction → TF-IDF scoring → cache."""
    n_workers = min(os.cpu_count() or 4, 8, max(len(files), 1))

    if len(files) < 20:
        raw = [_extract_one(f) for f in files]
    else:
        files_sorted = sorted(files, key=lambda f: f[2], reverse=True)
        chunksize = max(1, len(files_sorted) // (n_workers * 4))
        ctx = _get_pool_context()
        with ProcessPoolExecutor(max_workers=n_workers, mp_context=ctx) as pool:
            raw = list(pool.map(_extract_one, files_sorted, chunksize=chunksize))

    valid = [r for r in raw if r]

    # Compute document frequencies across all sessions
    n_docs = len(valid)
    df = Counter()
    for _, _, _, _, kw_counter, _, _, _ in valid:
        df.update(kw_counter.keys())  # +1 per keyword per session (not per occurrence)

    # Format cache lines with TF-IDF keyword ordering
    entries = [_format_entry(r, df, n_docs) for r in valid]
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

    # Approximate DF from existing cache (for TF-IDF scoring of changed sessions)
    df = Counter()
    existing = []
    with open(CACHE_PATH, 'r') as fh:
        for line in fh:
            line = line.rstrip('\n')
            if not line:
                continue
            parts = line.split('\t')
            # Detect old 6-field format → full rebuild
            if len(parts) < 7:
                return build_full(files)
            sid = parts[0]
            if len(parts) > 3:
                df.update(parts[3].split())  # approximate: DF=1 per keyword per session
            try:
                mtime_epoch = float(parts[5])  # mtime_epoch is field 5 (0-indexed)
            except (ValueError, IndexError):
                mtime_epoch = 0
            existing.append((sid, mtime_epoch, line))

    n_docs = len(existing) + len(changed)

    # Process changed files
    changed_sids = set()
    new_entries = []
    for f in changed:
        changed_sids.add(f[1])
        result = _extract_one(f)
        if result:
            new_entries.append(_format_entry(result, df, n_docs))

    # Merge with existing cache (exclude changed sids)
    for sid, epoch, line in existing:
        if sid not in changed_sids:
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
