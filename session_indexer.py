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

import json, multiprocessing, os, re, sqlite3, subprocess, sys, time, warnings
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
    mtime_epoch INTEGER DEFAULT 0,
    size_bytes INTEGER DEFAULT 0,
    last_activity_epoch INTEGER DEFAULT 0,
    is_fork INTEGER DEFAULT 0,
    parent_uuid TEXT DEFAULT '',
    parent_sid TEXT DEFAULT '',
    first_uuid TEXT DEFAULT ''
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

MARKER_RE = re.compile(rb'"type":"(user|assistant|summary|custom-title|ai-title)"')
_UUID_FIELD_RE = re.compile(rb'"uuid":"([0-9a-fA-F-]{36})"')

NOISE_PREFIXES = (
    "<local-command-caveat>",
    "Caveat: The messages below",
    "[Request interrupted",
    "<system-reminder>",
    "<command-name>",        # slash-command invocation (e.g. /effort)
    "<local-command-stdout>",  # its stdout echo — skip both so the preview
    "<local-command-stderr>",  # falls through to the real first message
)

# Machine-spawned sessions the user never resumes by hand (code-review/security
# runs, automated probes, bare slash-command artifacts, alert crons). They're
# still indexed and findable via explicit search, but hidden from the default
# recency listing so real work isn't buried. Set SESSIONS_INCLUDE_AUTOMATED=1
# to show them. Matched case-sensitively against the session preview.
AUTOMATED_PREVIEW_PATTERNS = (
    "Review this change for security",   # /code-review, /security-review
    "Reply with only:",                  # automated probes / keepalives
    "Window: last 15 minutes",           # alert-digest cron
    "⚠️ WARNING",              # user-facing-errors alert sessions
)


def is_automated(preview):
    """True if the session is machine-spawned (hidden from default view)."""
    p = preview or ""
    return any(p.startswith(prefix) for prefix in AUTOMATED_PREVIEW_PATTERNS)


SHOW_AUTOMATED = os.environ.get("SESSIONS_INCLUDE_AUTOMATED") == "1"


# --- Database ---

def init_db(db_path=None):
    """Initialize or open the session database."""
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    # Migrate older DBs that predate size_bytes.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    if "size_bytes" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN size_bytes INTEGER DEFAULT 0")
    if "last_activity_epoch" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN last_activity_epoch INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE sessions ADD COLUMN is_fork INTEGER DEFAULT 0")
    if "parent_uuid" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN parent_uuid TEXT DEFAULT ''")
        conn.execute("ALTER TABLE sessions ADD COLUMN parent_sid TEXT DEFAULT ''")
    if "first_uuid" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN first_uuid TEXT DEFAULT ''")
    return conn


# Display/sort recency: last *message* timestamp, not file mtime. Claude Code
# appends metadata lines (last-prompt, bridge-session, ai-title) to a session
# file on mere open/sync, so mtime promotes dead sessions to "today".
# Falls back to mtime for rows indexed before the column existed.
EFF_EPOCH_SQL = "CASE WHEN last_activity_epoch > 0 THEN last_activity_epoch ELSE mtime_epoch END"


def index_session(conn, sid, title, preview, body, project, created_epoch, mtime_epoch, size_bytes=0,
                  last_activity_epoch=0, is_fork=0, parent_uuid='', first_uuid=''):
    """Insert or update a session in the database.

    parent_sid (the resolved parent session) is preserved across re-indexing —
    resolution is expensive; it's only cleared when parent_uuid changes.
    """
    conn.execute("""
        INSERT INTO sessions (sid, title, preview, body, project, created_epoch, mtime_epoch, size_bytes,
                              last_activity_epoch, is_fork, parent_uuid, first_uuid)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sid) DO UPDATE SET
            title=excluded.title, preview=excluded.preview, body=excluded.body,
            project=excluded.project, created_epoch=excluded.created_epoch,
            mtime_epoch=excluded.mtime_epoch, size_bytes=excluded.size_bytes,
            last_activity_epoch=excluded.last_activity_epoch, is_fork=excluded.is_fork,
            parent_sid=CASE WHEN excluded.parent_uuid = sessions.parent_uuid
                            THEN sessions.parent_sid ELSE '' END,
            parent_uuid=excluded.parent_uuid,
            first_uuid=excluded.first_uuid
    """, (sid, title, preview, body, project, created_epoch, mtime_epoch, size_bytes,
          last_activity_epoch, is_fork, parent_uuid, first_uuid))


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
        return content[:2000]
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", "")[:2000])
        return " ".join(parts)[:2000]
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
        return content[:1500]
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
            return text_blocks[0][:1500]
        return text_blocks[0][:1000] + " " + text_blocks[-1][:1000]
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

    first_user = second_user = custom_title = summary = ai_title = None
    compact_fallback = None
    real_user_count = 0
    is_fork = 0
    first_msg_uuid = None
    body_parts = []

    for m in MARKER_RE.finditer(data):
        tp = m.group(1)

        if tp == b'user':
            line = _extract_line(data, m.start())
            if first_msg_uuid is None:
                um = _UUID_FIELD_RE.search(line)
                if um:
                    first_msg_uuid = um.group(1).decode()
            text = _parse_msg_text(line)
            if not text:
                continue  # skip tool_result-only and empty user messages
            clean = ' '.join(text[:200].split())
            # Compact-fork summary ("This session is being continued from a
            # previous conversation..."): not something the user typed. A fork
            # opens with one of these before any real message — mark it, keep
            # it as preview-of-last-resort, and let the preview fall through
            # to the first real post-compact message.
            if b'"isCompactSummary":true' in line:
                if real_user_count == 0:
                    is_fork = 1
                if compact_fallback is None:
                    compact_fallback = clean
                body_parts.append(text[:2000])
                continue
            # Skip noise messages (system artifacts, interrupts)
            is_noise = clean == "Warmup" or any(clean.startswith(p) for p in NOISE_PREFIXES)
            if not is_noise:
                real_user_count += 1
                if real_user_count == 1:
                    first_user = clean
                elif real_user_count == 2:
                    second_user = clean
            body_parts.append(text[:2000])

        elif tp == b'assistant':
            line = _extract_line(data, m.start())
            if first_msg_uuid is None:
                um = _UUID_FIELD_RE.search(line)
                if um:
                    first_msg_uuid = um.group(1).decode()
            if b'"type":"text"' not in line:
                continue
            gems = _extract_gems(line)
            if gems:
                body_parts.append(gems[:1500])

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

        elif tp == b'ai-title':
            # Claude Code's generated tab title — appended on every launch,
            # last one wins. This is the name the user sees in the terminal.
            line = _extract_line(data, m.start())
            try:
                ai_title = json.loads(line).get("aiTitle") or ai_title
            except (json.JSONDecodeError, ValueError):
                pass

    # A fresh compact-fork may have no real user message yet — it still holds
    # the live conversation state, so index it on the summary text.
    if not first_user:
        first_user = compact_fallback
    if not first_user:
        return None

    preview = first_user
    if first_user.startswith("claude --resume"):
        preview = second_user if second_user else None
    if not preview or preview.startswith("[Request interrupted") or preview.startswith("claude --resume"):
        return None

    # Title priority: custom-title > ai-title > summary > plan heading
    title = custom_title or ai_title or summary or ""
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

    # Last activity = last timestamped line (real messages), NOT file mtime —
    # metadata appends on open would otherwise resurrect dead sessions.
    last_activity = 0
    ts_pos = data.rfind(b'"timestamp":"')
    if ts_pos != -1:
        ts_end = data.find(b'"', ts_pos + 13)
        if ts_end != -1:
            try:
                dt = datetime.fromisoformat(data[ts_pos + 13:ts_end].decode().replace('Z', '+00:00'))
                last_activity = int(dt.timestamp())
            except (ValueError, OSError):
                pass

    # A fork's opening compact_boundary references a message uuid in its
    # parent session — the edge that lets the picker collapse fork chains.
    # First occurrence only: later in-place compactions reference this file.
    parent_uuid = ""
    if is_fork:
        pm = re.search(rb'"logicalParentUuid":"([0-9a-fA-F-]{36})"', data)
        if pm:
            parent_uuid = pm.group(1).decode()

    return (mtime, sid, title, preview, body, created_epoch, int(mtime), label, size,
            last_activity, is_fork, parent_uuid, first_msg_uuid or "")


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
    sql = f"""
        SELECT s.sid, s.title, s.preview, s.body, s.project,
               s.created_epoch, {EFF_EPOCH_SQL}, s.size_bytes, s.is_fork
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
    for sid, title, preview, body, project, created, mtime, size_bytes, is_fork in rows:
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
            'size_bytes': size_bytes, 'is_fork': is_fork, 'rank': score,
        })

    # Collapse fork chains: a hit on a superseded ancestor offers the live
    # terminal session instead (that's the one worth resuming), keeping the
    # ancestor's rank. Dedupe when several chain members match the query.
    hidden, terminal_of, cum = lineage_info(conn)
    best = {}
    for r in results:
        t = terminal_of.get(r['sid'], r['sid'])
        if t != r['sid']:
            row = conn.execute(f"""
                SELECT title, preview, body, project, created_epoch, {EFF_EPOCH_SQL}, size_bytes, is_fork
                FROM sessions WHERE sid = ?""", (t,)).fetchone()
            if row:
                r = {'sid': t, 'title': row[0], 'preview': row[1], 'body': row[2],
                     'project': row[3], 'created_epoch': row[4], 'mtime_epoch': row[5],
                     'size_bytes': row[6], 'is_fork': row[7], 'rank': r['rank']}
        r['size_bytes'] = cum.get(r['sid'], r['size_bytes'])
        if r['sid'] not in best or r['rank'] > best[r['sid']]['rank']:
            best[r['sid']] = r

    results = sorted(best.values(), key=lambda r: -r['rank'])  # higher = better
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

    # A session id can exist in multiple project dirs (e.g. a tiny worktree
    # stub + the real file in the main repo). Keep the largest so the picker
    # never indexes a 146-byte stub over a 28MB session.
    file_map = {}
    for path, sid, size, mtime, label in files:
        prev = file_map.get(sid)
        if prev is None or size > prev[2]:
            file_map[sid] = (path, sid, size, mtime, label)

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
            _, sid, title, preview, body, created, mtime_ep, label, size, last_act, is_fork, puuid, fuuid = r
            index_session(conn, sid, title, preview, body, label, created, mtime_ep, size,
                          last_act, is_fork, puuid, fuuid)
            added += 1

    _resolve_parents(conn, file_map)
    _resolve_duplicates(conn)
    conn.commit()
    return added, len(to_process) - added, removed


def _resolve_parents(conn, file_map):
    """Resolve fork parent_uuid -> parent session id, once per fork.

    The parent uuid is a message uuid inside the parent's transcript; grep the
    fork's sibling files (same project) for it. Result is cached in parent_sid
    ('?' = searched, not found — never retried), so this is a no-op on every
    sync without a fresh fork.
    """
    pending = conn.execute(
        "SELECT sid, parent_uuid, project FROM sessions"
        " WHERE parent_uuid != '' AND parent_sid = ''").fetchall()
    for sid, puuid, project in pending:
        # Same project family: a session can be re-homed between a repo's main
        # folder and its worktree folders, so match on the base project label.
        base = project.split('--claude-worktrees')[0]
        candidates = [args[0] for s2, args in file_map.items()
                      if args[4].split('--claude-worktrees')[0] == base and s2 != sid]
        parent_sid = '?'
        if candidates:
            try:
                out = subprocess.run(
                    ['grep', '-l', '-F', f'"uuid":"{puuid}"', *candidates],
                    capture_output=True, text=True, timeout=60)
                hit = out.stdout.split('\n', 1)[0].strip()
                if hit:
                    parent_sid = os.path.basename(hit)[:-6]  # strip .jsonl
            except (OSError, subprocess.SubprocessError):
                parent_sid = ''  # transient failure — retry next sync
        if parent_sid:
            conn.execute("UPDATE sessions SET parent_sid = ? WHERE sid = ?",
                         (parent_sid, sid))


def _resolve_duplicates(conn):
    """Link sessions that share a first message uuid into a fork chain.

    Newer Claude Code can continue a conversation under a fresh session id
    with NO compact_boundary edge (fork-on-resume, bridged/compacted
    continuations). The copied history keeps its original message uuids, so
    a shared first-message uuid within a project family identifies one
    logical conversation. Members are chained oldest -> newest by activity;
    the normal lineage collapse then offers only the live end. A real
    compact edge (or an existing link) is never overwritten. Pure SQL — no
    file access, safe to run every sync.
    """
    rows = conn.execute(
        f"SELECT sid, project, first_uuid, parent_uuid, parent_sid, {EFF_EPOCH_SQL}"
        " FROM sessions WHERE first_uuid != ''").fetchall()
    fams = {}
    for sid, project, fuuid, puuid, psid, eff in rows:
        base = project.split('--claude-worktrees')[0]
        fams.setdefault((base, fuuid), []).append((eff, sid, puuid or '', psid or ''))
    for (_, fuuid), members in fams.items():
        if len(members) < 2:
            continue
        members.sort()
        for (_, osid, _, _), (_, nsid, npuuid, npsid) in zip(members, members[1:]):
            if npuuid or npsid:
                continue  # already linked (compact edge or earlier dup pass)
            conn.execute(
                "UPDATE sessions SET parent_uuid = ?, parent_sid = ?, is_fork = 1 WHERE sid = ?",
                ('dup:' + fuuid, osid, nsid))


def lineage_info(conn):
    """Map fork chains: returns (hidden_sids, terminal_of, cumulative_size).

    A session is hidden when a resolved child session's last activity is at or
    after its own — the conversation moved on under the child's id. (A parent
    resumed AFTER its fork has newer activity and stays visible.) terminal_of
    follows child links to the live end of each chain; cumulative_size charges
    hidden ancestors' bytes to their terminal so the entry reflects the whole
    conversation.
    """
    rows = conn.execute(
        f"SELECT sid, parent_sid, {EFF_EPOCH_SQL}, size_bytes FROM sessions").fetchall()
    eff = {sid: e for sid, _, e, _ in rows}
    size = {sid: s for sid, _, _, s in rows}
    children = {}
    for sid, psid, _, _ in rows:
        if psid and psid != '?' and psid in eff:
            children.setdefault(psid, []).append(sid)

    hidden = {psid for psid, kids in children.items()
              if any(eff[k] >= eff[psid] for k in kids)}

    def terminal(sid):
        cur, seen = sid, {sid}
        while cur in hidden:
            kids = [k for k in children.get(cur, []) if k not in seen]
            if not kids:
                break
            cur = max(kids, key=lambda k: eff[k])
            seen.add(cur)
        return cur

    terminal_of = {sid: terminal(sid) for sid in eff}

    cum = dict(size)
    for sid in hidden:
        t = terminal_of[sid]
        if t != sid:
            cum[t] = cum.get(t, 0) + size.get(sid, 0)
    return hidden, terminal_of, cum


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


# --- Preview (shared by fzf pane and SessionPicker.app) ---
#
# Signal hierarchy (derived with the knowledge-base prefilter, see
# session_prefilter.py there): user messages are the spine of "what is this
# about"; assistant responses follow opening remark → tool noise → closing
# summary, so only the first/last text blocks ("gems") carry signal; injected
# system-reminders, hook outputs, and command echoes are never signal.

_SYSTEM_REMINDER_RE = re.compile(r'<system-reminder>.*?</system-reminder>', re.DOTALL)
_HOOK_OUTPUT_RE = re.compile(r'UserPromptSubmit hook success:.*?(?=\n\n|\Z)', re.DOTALL)
_IMG_ONLY_RE = re.compile(r'^(\[Image[^\]]*\]\s*)+$')

_PREVIEW_HEAD_CAP = 4_000_000   # bytes of file head parsed for the opening
_PREVIEW_TAIL_CAP = 2_000_000   # bytes of file tail parsed for "where it left off"

DIM, BOLD, CYAN, GREEN, YELLOW, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[32m", "\033[1;33m", "\033[0m"


def _clean_user_text(text):
    """Noise-strip a user message; None if nothing user-authored remains."""
    text = _SYSTEM_REMINDER_RE.sub('', text)
    text = _HOOK_OUTPUT_RE.sub('', text)
    clean = ' '.join(text.split())
    if not clean or clean == "Warmup":
        return None
    if any(clean.startswith(p) for p in NOISE_PREFIXES):
        return None
    if clean.startswith("<task-notification>") or clean.startswith("This session is being continued"):
        return None
    if _IMG_ONLY_RE.match(clean):
        return None
    return clean


def _find_session_file(sid):
    """Largest transcript with this sid across project dirs (stubs lose)."""
    best = None
    try:
        for proj in os.scandir(PROJECTS_DIR):
            if not proj.is_dir():
                continue
            p = os.path.join(proj.path, sid + '.jsonl')
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            if best is None or sz > best[1]:
                best = (p, sz, proj.name)
    except OSError:
        pass
    return best  # (path, size, project_dirname) or None


def _chunk_messages(chunk):
    """Parse (role, text) messages from a byte chunk, preserving order."""
    out = []
    for m in MARKER_RE.finditer(chunk):
        tp = m.group(1)
        if tp not in (b'user', b'assistant'):
            continue
        line = _extract_line(chunk, m.start())
        if tp == b'user':
            if b'"isCompactSummary":true' in line:
                continue
            text = _parse_msg_text(line)
            if text:
                clean = _clean_user_text(text)
                if clean:
                    out.append(('user', clean))
        else:
            if b'"type":"text"' not in line:
                continue
            gems = _extract_gems(line)
            if gems:
                out.append(('assistant', ' '.join(gems.split())))
    return out


def _preview_header(sid, path, size, proj_dirname, data_head):
    lines = [f"{DIM}─── {project_label(proj_dirname)} ─────────────────────{RESET}"]
    ts = re.search(rb'"timestamp":"([^"]+)"', data_head)
    if ts:
        try:
            dt = datetime.fromisoformat(ts.group(1).decode().replace('Z', '+00:00'))
            lines.append(f"{DIM}Created: {dt.astimezone().strftime('%b %d, %Y')}{RESET}")
        except ValueError:
            pass
    return lines


def preview_output(sid, query=''):
    """Print a conversation preview: opening user messages + latest state."""
    found = _find_session_file(sid)
    if not found:
        print("Session file not found")
        return
    path, size, proj_dirname = found

    capped = size > _PREVIEW_HEAD_CAP + _PREVIEW_TAIL_CAP
    with open(path, 'rb') as f:
        head = f.read(_PREVIEW_HEAD_CAP if capped else size)
        tail = b''
        if capped:
            f.seek(size - _PREVIEW_TAIL_CAP)
            tail = f.read()
            nl = tail.find(b'\n')
            tail = tail[nl + 1:] if nl != -1 else tail

    for line in _preview_header(sid, path, size, proj_dirname, head):
        print(line)
    scanned = head + tail
    approx = "≥" if capped else ""
    msg_count = scanned.count(b'"type":"user"') + scanned.count(b'"type":"assistant"')
    print(f"{DIM}Messages: {approx}{msg_count}{RESET}")
    compacts = scanned.count(b'"subtype":"compact_boundary"')
    is_fork = b'"isCompactSummary":true' in head[:2_000_000]
    print()
    if compacts:
        print(f"\033[33m⚠ {compacts}x compacted — long session{RESET}")
    if is_fork:
        print(f"{GREEN}↪ continues an earlier session{RESET}")
    if compacts or is_fork:
        print()

    if query.strip():
        _preview_query(path, query)
    else:
        _preview_flow(head, tail, capped)


def _preview_flow(head, tail, capped):
    """Opening user messages, then the latest exchanges from the tail."""
    head_msgs = _chunk_messages(head)
    opening = [(r, t) for r, t in head_msgs if r == 'user'][:8]
    for _, t in opening:
        print(f"{CYAN}>{RESET} {t[:300]}")
        print()

    if not capped:
        # Whole file parsed: show the closing assistant gem if any.
        gems = [t for r, t in head_msgs if r == 'assistant']
        if gems:
            print(f"{DIM}— latest —{RESET}")
            print(f"{DIM}·{RESET} {gems[-1][-300:]}")
        return

    tail_msgs = _chunk_messages(tail)
    recent_users = [(r, t) for r, t in tail_msgs if r == 'user'][-3:]
    gems = [t for r, t in tail_msgs if r == 'assistant']
    if recent_users or gems:
        print(f"{DIM}···{RESET}")
        print(f"{DIM}— latest —{RESET}")
        for _, t in recent_users:
            print(f"{CYAN}>{RESET} {t[:300]}")
            print()
        if gems:
            print(f"{DIM}·{RESET} {gems[-1][-300:]}")


def _preview_query(path, query):
    """Messages matching the query terms, term-highlighted, streaming scan."""
    terms = [t.strip('"').lower() for t in query.split() if t.strip('"')]
    if not terms:
        return
    pattern = re.compile('|'.join(re.escape(t) for t in terms).encode(), re.IGNORECASE)
    hi = re.compile('|'.join(re.escape(t) for t in terms), re.IGNORECASE)
    shown = 0
    with open(path, 'rb') as f:
        for line in f:
            if shown >= 15:
                break
            if not pattern.search(line):
                continue
            is_user = b'"type":"user"' in line
            is_asst = b'"type":"assistant"' in line and b'"type":"text"' in line
            if not (is_user or is_asst):
                continue
            if is_user:
                text = _parse_msg_text(line)
                text = _clean_user_text(text) if text else None
            else:
                text = _extract_gems(line)
                text = ' '.join(text.split()) if text else None
            if not text or not hi.search(text):
                continue
            snippet = hi.sub(lambda m: f"{YELLOW}{m.group(0)}{RESET}", text[:300])
            mark = f"{GREEN}▸{RESET}" if is_user else f"{CYAN}▹{RESET}"
            print(f"{mark} {snippet}")
            print()
            shown += 1


# --- TSV cache (for fzf no-query mode) ---

def write_cache(conn, cache_path=None):
    """Write TSV cache for fzf display. Field 4 = body excerpt (for fzf search)."""
    hidden, _, _ = lineage_info(conn)
    rows = conn.execute(f"""
        SELECT sid, title, preview, body, project, created_epoch, {EFF_EPOCH_SQL}
        FROM sessions ORDER BY {EFF_EPOCH_SQL} DESC
    """).fetchall()

    path = cache_path or CACHE_PATH
    tmp = path + ".tmp"
    written = 0
    with open(tmp, 'w') as f:
        for sid, title, preview, body, project, created, mtime in rows:
            if sid in hidden:
                continue
            if not SHOW_AUTOMATED and is_automated(preview):
                continue
            excerpt = ' '.join(body.split())[:600]
            f.write(f"{sid}\t{title}\t{preview}\t{excerpt}\t{created}\t{mtime}\t{project}\n")
            written += 1
    os.replace(tmp, path)
    return written


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


def _humansize(n):
    """Compact size label for the picker (file bytes \u2192 '29M', '686k', '')."""
    if not n:
        return ""
    if n >= 1_000_000:
        return f"{round(n / 1_000_000)}M"
    if n >= 10_000:
        return f"{round(n / 1_000)}k"
    return ""  # sub-10k stubs read as blank \u2014 nothing worth resuming


def format_fzf_line(sid, title, preview, body, project, created_epoch, mtime_epoch, size_bytes=0, is_fork=0, active=0, current_label=''):
    """Format a session as fzf-ready line: SID\\tDISPLAY+PAD+SEARCH."""
    # Show last-activity date (matches the last-activity DESC sort), not start date.
    rd = _reldate(mtime_epoch)
    # Size column \u2014 disambiguates sessions that share a preview (e.g. a 29M build
    # session vs a 113-message earlier run of the same opening prompt).
    sz = _humansize(size_bytes)

    # Project tag (only for other projects)
    tag = ""
    if project and project != current_label:
        tag = f"  \033[2m\u00b7 {project}\033[0m"

    # Compact-fork marker: this session continues an earlier one under a new
    # id \u2014 its small file size is misleading, it holds the CURRENT state.
    fork = "\033[32m\u21aa \033[0m" if is_fork else ""
    # Live marker: message activity in the last 5 min \u2014 likely attached to an
    # open terminal; resuming it AGAIN would fork state. Go to that window.
    if active:
        fork = f"\033[31m\u25cf \033[0m{fork}"

    # Display: date + size + title or preview + optional project tag
    if title:
        display = f"\033[33m{rd:<5}\033[0m \033[2m{sz:>4}\033[0m {fork}\033[1m{title}\033[0m{tag}"
    else:
        p = (preview[:77] + "...") if len(preview) > 80 else preview
        display = f"\033[33m{rd:<5}\033[0m \033[2m{sz:>4}\033[0m {fork}{p}{tag}"

    # Search text (pushed off-screen by padding)
    excerpt = ' '.join((body or '').split())[:300]
    search_text = f"{title} {excerpt} {preview} {project}"

    return f"{sid}\t{display}{PAD}{search_text}"


def list_rows(conn, query=''):
    """Session rows for any front-end: search results or browse-by-recency.

    Returns dicts with lineage collapse and the automated filter applied,
    plus 'active' (last message within 5 minutes — probably attached to a
    live terminal; resuming it would fork state).
    """
    now = time.time()
    if query and query.strip():
        rows = search(conn, query)
    else:
        hidden, _, cum = lineage_info(conn)
        rows = []
        for row in conn.execute(f"""
            SELECT sid, title, preview, body, project, created_epoch, {EFF_EPOCH_SQL}, size_bytes, is_fork
            FROM sessions ORDER BY {EFF_EPOCH_SQL} DESC
        """).fetchall():
            sid, title, preview, body, project, created, eff, size_bytes, is_fork = row
            if sid in hidden:
                continue  # superseded by a compact-fork child — offer only the live end
            rows.append({
                'sid': sid, 'title': title, 'preview': preview, 'body': body,
                'project': project, 'created_epoch': created, 'mtime_epoch': eff,
                'size_bytes': cum.get(sid, size_bytes), 'is_fork': is_fork,
            })
    searching = bool(query and query.strip())
    out = []
    for r in rows:
        # Automated sessions stay findable via explicit search — they're only
        # hidden from the default recency listing.
        if not searching and not SHOW_AUTOMATED and is_automated(r['preview']):
            continue
        r['active'] = 1 if now - r['mtime_epoch'] < 300 else 0
        out.append(r)
    return out


def fzf_output(conn, query='', current_label=''):
    """Output fzf-formatted lines: search results or all sessions by recency."""
    for r in list_rows(conn, query):
        print(format_fzf_line(
            r['sid'], r['title'], r['preview'], r['body'],
            r['project'], r['created_epoch'], r['mtime_epoch'], r['size_bytes'],
            r.get('is_fork', 0), r['active'],
            current_label=current_label
        ))


def json_output(conn, query='', limit=100):
    """JSON rows for GUI front-ends (SessionPicker.app etc.)."""
    rows = list_rows(conn, query)[:limit]
    print(json.dumps([{
        'sid': r['sid'],
        'title': r['title'],
        'preview': r['preview'][:160],
        'project': r['project'],
        'last_epoch': r['mtime_epoch'],
        'reldate': _reldate(r['mtime_epoch']).strip(),
        'size': _humansize(r['size_bytes']),
        'is_fork': r.get('is_fork', 0),
        'active': r['active'],
    } for r in rows]))


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
    json_mode = "--json" in sys.argv
    search_query = _get_arg("--search")
    label = _get_arg("--label") or ''
    preview_sid = _get_arg("--preview")

    # Preview mode: conversation preview for one session (fzf pane / app)
    if preview_sid:
        preview_output(preview_sid, query=search_query or '')
        return

    # JSON mode: rows for GUI front-ends (browse or search)
    if json_mode:
        conn = init_db()
        json_output(conn, query=search_query or '')
        conn.close()
        return

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
