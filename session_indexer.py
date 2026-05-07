#!/usr/bin/env python3
"""Session indexer with SQLite FTS5 search.

Replaces keyword extraction + custom TF-IDF with full-text search.
FTS5 handles exact phrases, short tokens, and BM25 ranking natively.

Usage:
  python3 session_indexer.py              # Update index + write TSV cache
  python3 session_indexer.py --search Q   # Search, output ranked TSV
  python3 session_indexer.py --rebuild    # Force full rebuild
  python3 session_indexer.py --timing     # Show performance stats
"""

import json, multiprocessing, os, re, sqlite3, sys, time, warnings
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor

# --- Paths ---

PROJECTS_DIR = os.environ.get("SESSION_PROJECTS_DIR",
    os.path.join(os.path.expanduser("~"), ".claude", "projects"))
CACHE_PATH = os.environ.get("SESSION_CACHE_PATH",
    os.path.join(os.path.expanduser("~"), ".claude", ".sessions-unified-cache.tsv"))
DB_PATH = os.environ.get("SESSION_DB_PATH",
    os.path.join(os.path.expanduser("~"), ".claude", ".sessions.db"))
HOME_KEY = os.path.expanduser("~").replace("/", "-").lstrip("-")

# --- Schema ---

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    sid TEXT PRIMARY KEY,
    title TEXT DEFAULT '',
    preview TEXT DEFAULT '',
    body TEXT DEFAULT '',
    project TEXT DEFAULT '',
    created_epoch INTEGER DEFAULT 0,
    mtime_epoch INTEGER DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    title, preview, body,
    content='sessions',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

-- Sync triggers (recreated each init to stay correct)
DROP TRIGGER IF EXISTS sessions_ai;
CREATE TRIGGER sessions_ai AFTER INSERT ON sessions BEGIN
    INSERT INTO sessions_fts(rowid, title, preview, body)
    VALUES (new.rowid, new.title, new.preview, new.body);
END;

DROP TRIGGER IF EXISTS sessions_ad;
CREATE TRIGGER sessions_ad AFTER DELETE ON sessions BEGIN
    INSERT INTO sessions_fts(sessions_fts, rowid, title, preview, body)
    VALUES('delete', old.rowid, old.title, old.preview, old.body);
END;

DROP TRIGGER IF EXISTS sessions_au;
CREATE TRIGGER sessions_au AFTER UPDATE ON sessions BEGIN
    INSERT INTO sessions_fts(sessions_fts, rowid, title, preview, body)
    VALUES('delete', old.rowid, old.title, old.preview, old.body);
    INSERT INTO sessions_fts(rowid, title, preview, body)
    VALUES (new.rowid, new.title, new.preview, new.body);
END;
"""

# --- Constants ---

MARKER_RE = re.compile(rb'"type":"(user|assistant|summary|custom-title)"')

NOISE_PREFIXES = (
    "<local-command-caveat>",
    "Caveat: The messages below",
    "[Request interrupted",
    "<system-reminder>",
)


# --- Database ---

def init_db(db_path=None):
    """Initialize or open the session database."""
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def index_session(conn, sid, title, preview, body, project, created_epoch, mtime_epoch):
    """Insert or update a session in the database."""
    conn.execute("""
        INSERT INTO sessions (sid, title, preview, body, project, created_epoch, mtime_epoch)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sid) DO UPDATE SET
            title=excluded.title, preview=excluded.preview, body=excluded.body,
            project=excluded.project, created_epoch=excluded.created_epoch,
            mtime_epoch=excluded.mtime_epoch
    """, (sid, title, preview, body, project, created_epoch, mtime_epoch))


# --- Text extraction (from JSONL) ---

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

    Claude's responses follow: opening remark -> tool calls -> closing summary.
    The first and last text blocks carry the signal.
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
        return text_blocks[0][:200] + " " + text_blocks[-1][:200]
    return None


def _extract_one(args):
    """Extract session data from a JSONL file.

    Returns (mtime, sid, title, preview, body, created_epoch, mtime_epoch, label)
    or None if the session should be skipped.
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
    real_user_count = 0
    body_parts = []

    for m in MARKER_RE.finditer(data):
        tp = m.group(1)

        if tp == b'user':
            line = _extract_line(data, m.start())
            text = _parse_msg_text(line)
            if not text:
                continue  # skip tool_result-only and empty user messages
            clean = ' '.join(text[:200].split())
            # Skip noise messages (system artifacts, interrupts)
            is_noise = clean == "Warmup" or any(clean.startswith(p) for p in NOISE_PREFIXES)
            if not is_noise:
                real_user_count += 1
                if real_user_count == 1:
                    first_user = clean
                elif real_user_count == 2:
                    second_user = clean
            body_parts.append(text[:500])

        elif tp == b'assistant':
            line = _extract_line(data, m.start())
            if b'"type":"text"' not in line:
                continue
            gems = _extract_gems(line)
            if gems:
                body_parts.append(gems[:300])

        elif tp == b'summary':
            line = _extract_line(data, m.start())
            try:
                obj = json.loads(line)
                s = obj.get("summary") or ""
                if summary is None:
                    summary = s
                if s:
                    body_parts.append(s[:500])
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

    # Body: concatenated conversation text for full-text search
    # No length limit — per-message extraction (500/300 chars) is the real guard rail
    body = "\n".join(body_parts)

    # Append dehyphenated compounds so "therapiekompass" matches "Therapie-Kompass".
    # Preserve frequency: if "Therapie-Kompass" appears 10x, add "TherapieKompass" 10x.
    from collections import Counter
    hyphenated = Counter(re.findall(r'\b(\w+-\w+(?:-\w+)*)\b', body))
    if hyphenated:
        parts = []
        for word, count in hyphenated.items():
            parts.extend([word.replace("-", "")] * count)
        body = body + "\n" + " ".join(parts)

    # Extract creation timestamp from first line
    created_epoch = int(mtime)
    first_nl = data.find(b'\n')
    first_line = data[:first_nl] if first_nl > 0 else data
    ts_match = re.search(rb'"timestamp":"([^"]+)"', first_line)
    if ts_match:
        try:
            dt = datetime.fromisoformat(ts_match.group(1).decode().replace('Z', '+00:00'))
            created_epoch = int(dt.timestamp())
        except (ValueError, OSError):
            pass

    return (mtime, sid, title, preview, body, created_epoch, int(mtime), label)


# --- Search ---

def search(conn, query, limit=200):
    """Search sessions using FTS5 with BM25 ranking.

    Supports: exact phrases ("week 12"), term AND (week 12),
    project filter (--project PS), negation (-standup, --exclude term).
    """
    raw = query

    # Extract --project filter
    project_filter = None
    pm = re.search(r'--project\s+(\S+)', raw)
    if pm:
        project_filter = pm.group(1).lower()
        raw = raw[:pm.start()] + raw[pm.end():]

    # Extract --exclude filter
    exclude_terms = set()
    em = re.search(r'--exclude\s+(\S+)', raw)
    if em:
        exclude_terms.add(em.group(1).lower())
        raw = raw[:em.start()] + raw[em.end():]

    # Extract -term negations
    for neg in re.findall(r'(?:^|\s)-(\w+)', raw):
        exclude_terms.add(neg.lower())
        raw = re.sub(r'(?:^|\s)-' + re.escape(neg), '', raw)

    fts_query = raw.strip()
    if not fts_query:
        return []

    # Multi-word queries: use OR so any term matches (BM25 ranks multi-match higher).
    # Without this, FTS5 defaults to AND — which fails when the user searches across
    # sessions with loosely related terms (e.g., "supabase nano micro plan upgrade").
    # Skip if query already contains FTS5 operators or quoted phrases.
    _FTS5_OPS = {'OR', 'AND', 'NOT', 'NEAR'}
    tokens = fts_query.split()
    if ('"' not in fts_query and len(tokens) > 1
            and not any(t.upper() in _FTS5_OPS for t in tokens)):
        fts_query = ' OR '.join(tokens)

    # Extract search terms for mention-count re-ranking
    rank_terms = set()
    for token in raw.strip().split():
        t = token.strip('"').lower()
        if t and t.upper() not in _FTS5_OPS and len(t) > 1:
            rank_terms.add(t)
            if '-' in t:
                rank_terms.add(t.replace('-', ''))

    # FTS5 for matching, fetch generously for Python re-ranking
    sql = """
        SELECT s.sid, s.title, s.preview, s.body, s.project,
               s.created_epoch, s.mtime_epoch
        FROM sessions s
        JOIN sessions_fts f ON s.rowid = f.rowid
        WHERE sessions_fts MATCH ?
        LIMIT ?
    """

    try:
        rows = conn.execute(sql, (fts_query, limit * 3)).fetchall()
    except sqlite3.OperationalError:
        safe = _sanitize_fts_query(fts_query)
        if not safe:
            return []
        try:
            rows = conn.execute(sql, (safe, limit * 3)).fetchall()
        except sqlite3.OperationalError:
            return []

    # Post-filter, then re-rank by mention count × recency.
    # BM25's document length normalization penalizes long sessions unfairly —
    # a 73K session with 10 mentions is more relevant than a 12K session with 2.
    import math
    now = time.time()
    results = []
    for sid, title, preview, body, project, created, mtime in rows:
        if project_filter and project_filter not in project.lower():
            continue
        if exclude_terms:
            combined = f"{title} {preview} {body}".lower()
            if any(t in combined for t in exclude_terms):
                continue

        # Per-term weighted mention count (title=10x, preview=5x, body=1x).
        title_l = (title or '').lower()
        preview_l = (preview or '').lower()
        body_l = (body or '').lower()
        per_term = {
            t: title_l.count(t) * 10 + preview_l.count(t) * 5 + body_l.count(t)
            for t in rank_terms
        }

        # Saturate each term's contribution (log1p) before summing. Prevents one
        # very common word (e.g. "laura" mentioned 81x in a huge unrelated session)
        # from drowning out a focused session where all query terms are present
        # but each appears only a handful of times.
        saturated = sum(math.log1p(h) for h in per_term.values())

        # Coverage: fraction of query terms this session actually contains.
        # Squared so a 3/3 match strongly beats a 2/3 "one term 50x" fluke.
        distinct = sum(1 for h in per_term.values() if h > 0)
        coverage = distinct / len(rank_terms) if rank_terms else 1.0

        # Coherence bonus: if ALL query terms appear in title+preview (a short,
        # ~200-char window), that's a strong signal the session is *about* those
        # terms — not just a sprawling session where the terms happen to co-occur.
        header = f"{title_l} {preview_l}"
        header_distinct = sum(1 for t in rank_terms if t in header)
        coherence = 2.0 if (rank_terms and header_distinct == len(rank_terms)) else 1.0

        # Recency boost: ~3x for today, ~2x for 1 week old, ~1.3x for 1 month
        age_s = max(0, now - mtime)
        recency = 1.0 + 2.0 / (1.0 + age_s / 604800.0)

        score = saturated * recency * (coverage ** 2) * coherence

        results.append({
            'sid': sid, 'title': title, 'preview': preview,
            'body': body, 'project': project,
            'created_epoch': created, 'mtime_epoch': mtime,
            'rank': score,
        })

    results.sort(key=lambda r: -r['rank'])  # higher = better
    return results[:limit]


def _count_mentions(text, terms):
    """Count total mentions of all terms in text (case-insensitive)."""
    text_lower = text.lower()
    return sum(text_lower.count(t) for t in terms)


def _sanitize_fts_query(query):
    """Make a query safe for FTS5 by quoting problematic terms."""
    parts = []
    i = 0
    while i < len(query):
        if query[i] == '"':
            end = query.find('"', i + 1)
            if end == -1:
                end = len(query)
            parts.append(query[i:end + 1])
            i = end + 1
        elif query[i].isspace():
            i += 1
        else:
            end = i
            while end < len(query) and not query[end].isspace() and query[end] != '"':
                end += 1
            term = query[i:end]
            if re.search(r'[^a-zA-Z0-9_]', term):
                parts.append(f'"{term}"')
            else:
                parts.append(term)
            i = end
    return " ".join(parts)


# --- Sync ---

def sync_db(conn, files, force_rebuild=False):
    """Sync database with filesystem. Returns (added, skipped, removed) counts."""
    if force_rebuild:
        conn.execute("DELETE FROM sessions")
        conn.commit()
        indexed = {}
    else:
        indexed = dict(conn.execute(
            "SELECT sid, mtime_epoch FROM sessions"
        ).fetchall())

    file_map = {sid: (path, sid, size, mtime, label)
                for path, sid, size, mtime, label in files}

    # Remove deleted sessions
    removed = 0
    for sid in set(indexed) - set(file_map):
        conn.execute("DELETE FROM sessions WHERE sid = ?", (sid,))
        removed += 1

    # Find new/changed sessions
    to_process = []
    for sid, args in file_map.items():
        old_mtime = indexed.get(sid)
        if old_mtime is None or int(args[3]) > old_mtime:
            to_process.append(args)

    if not to_process and not removed:
        conn.commit()
        return 0, 0, 0

    # Extract (parallel for large batches)
    if len(to_process) > 30:
        results = _parallel_extract(to_process)
    else:
        results = [_extract_one(f) for f in to_process]

    # Index
    added = 0
    for r in results:
        if r:
            _, sid, title, preview, body, created, mtime_ep, label = r
            index_session(conn, sid, title, preview, body, label, created, mtime_ep)
            added += 1

    conn.commit()
    return added, len(to_process) - added, removed


def _parallel_extract(files):
    """Extract sessions in parallel using fork workers."""
    n_workers = min(os.cpu_count() or 4, 8, max(len(files), 1))
    files_sorted = sorted(files, key=lambda f: f[2], reverse=True)
    chunksize = max(1, len(files_sorted) // (n_workers * 4))
    try:
        warnings.filterwarnings('ignore', category=DeprecationWarning)
        ctx = multiprocessing.get_context('fork')
    except ValueError:
        ctx = multiprocessing.get_context('spawn')
    with ProcessPoolExecutor(max_workers=n_workers, mp_context=ctx) as pool:
        return list(pool.map(_extract_one, files_sorted, chunksize=chunksize))


# --- TSV cache (for fzf no-query mode) ---

def write_cache(conn, cache_path=None):
    """Write TSV cache for fzf display. Field 4 = body excerpt (for fzf search)."""
    rows = conn.execute("""
        SELECT sid, title, preview, body, project, created_epoch, mtime_epoch
        FROM sessions ORDER BY mtime_epoch DESC
    """).fetchall()

    path = cache_path or CACHE_PATH
    tmp = path + ".tmp"
    with open(tmp, 'w') as f:
        for sid, title, preview, body, project, created, mtime in rows:
            excerpt = ' '.join(body.split())[:600]
            f.write(f"{sid}\t{title}\t{preview}\t{excerpt}\t{created}\t{mtime}\t{project}\n")
    os.replace(tmp, path)
    return len(rows)


# --- fzf output formatting ---

PAD = ' ' * 300


def _reldate(epoch):
    """Format epoch as relative date string for display."""
    local = time.localtime()
    midnight = int(time.mktime(time.struct_time((
        local.tm_year, local.tm_mon, local.tm_mday,
        0, 0, 0, 0, 0, local.tm_isdst
    ))))
    if epoch >= midnight:
        return "today"
    d = (midnight - epoch - 1) // 86400 + 1
    if d == 1:
        return " 1d  "
    if d < 7:
        return f"{d:2d}d  "
    if d < 30:
        return f"{d // 7:2d}w  "
    if d < 365:
        return f"{d // 30:2d}mo "
    return f"{d // 365:2d}y  "


def format_fzf_line(sid, title, preview, body, project, created_epoch, mtime_epoch, current_label=''):
    """Format a session as fzf-ready line: SID\\tDISPLAY+PAD+SEARCH."""
    rd = _reldate(created_epoch)

    # Project tag (only for other projects)
    tag = ""
    if project and project != current_label:
        tag = f"  \033[2m\u00b7 {project}\033[0m"

    # Display: date + title or preview + optional project tag
    if title:
        display = f"\033[33m{rd:<5}\033[0m \033[1m{title}\033[0m{tag}"
    else:
        p = (preview[:77] + "...") if len(preview) > 80 else preview
        display = f"\033[33m{rd:<5}\033[0m {p}{tag}"

    # Search text (pushed off-screen by padding)
    excerpt = ' '.join((body or '').split())[:300]
    search_text = f"{title} {excerpt} {preview} {project}"

    return f"{sid}\t{display}{PAD}{search_text}"


def fzf_output(conn, query='', current_label=''):
    """Output fzf-formatted lines: search results or all sessions by recency."""
    if query and query.strip():
        results = search(conn, query)
        for r in results:
            print(format_fzf_line(
                r['sid'], r['title'], r['preview'], r['body'],
                r['project'], r['created_epoch'], r['mtime_epoch'], current_label
            ))
    else:
        rows = conn.execute("""
            SELECT sid, title, preview, body, project, created_epoch, mtime_epoch
            FROM sessions ORDER BY mtime_epoch DESC
        """).fetchall()
        for row in rows:
            print(format_fzf_line(*row, current_label=current_label))


# --- Filesystem scan ---

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


# --- Main ---

def _get_arg(flag):
    """Get the value after a CLI flag, or None."""
    for i, arg in enumerate(sys.argv):
        if arg == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def main():
    t0 = time.monotonic()
    timing = "--timing" in sys.argv
    force = "--rebuild" in sys.argv
    fzf_mode = "--fzf" in sys.argv
    search_query = _get_arg("--search")
    label = _get_arg("--label") or ''

    # fzf mode: output formatted lines for fzf display (used by reload)
    if fzf_mode:
        conn = init_db()
        fzf_output(conn, query=search_query or '', current_label=label)
        conn.close()
        if timing:
            elapsed = (time.monotonic() - t0) * 1000
            print(f"fzf: {elapsed:.0f}ms", file=sys.stderr)
        return

    # Search mode: raw TSV output (for scripts/tests)
    if search_query is not None:
        conn = init_db()
        results = search(conn, search_query)
        for r in results:
            excerpt = ' '.join(r['body'].split())[:600]
            print(f"{r['sid']}\t{r['title']}\t{r['preview']}\t{excerpt}\t{r['created_epoch']}\t{r['mtime_epoch']}\t{r['project']}")
        conn.close()
        if timing:
            elapsed = (time.monotonic() - t0) * 1000
            print(f"search: {len(results)} results, {elapsed:.0f}ms", file=sys.stderr)
        return

    # Index mode: sync DB + write cache
    if not os.path.isdir(PROJECTS_DIR):
        sys.exit(0)

    files = scan_all_files()
    conn = init_db()
    added, skipped, removed = sync_db(conn, files, force_rebuild=force)
    count = write_cache(conn)
    conn.close()

    elapsed = (time.monotonic() - t0) * 1000
    if timing:
        mode = "rebuild" if force else "sync"
        print(f"{mode}: {count} sessions ({added} new, {removed} removed), {elapsed:.0f}ms",
              file=sys.stderr)


if __name__ == "__main__":
    main()
