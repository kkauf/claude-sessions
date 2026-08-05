#!/usr/bin/env python3
"""Export session metadata + per-day token usage as JSON for a visualization front-end.

The session index (`session_indexer.py`) knows *what* every session is about;
it does not know what any of them cost. Token usage lives only in the raw
transcripts — every assistant message carries a `message.usage` block. This
script joins the two: index rows for identity/lineage, transcripts for tokens.

Reads (never writes) the indexer's database, and keeps its own parse cache in a
separate SQLite file so `session_indexer.py`'s schema stays untouched.

Usage:
  python3 session_map_export.py                    # -> ~/.claude/.sessions-map.json
  python3 session_map_export.py -o /tmp/map.json   # alternate destination
  python3 session_map_export.py --include-automated

Stdlib only — no pip dependencies.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Reused wholesale so the export can never disagree with the picker:
#   is_automated  — machine-spawned sessions the user never resumes
#   lineage_info  — which sessions are superseded fork-chain ancestors
#   EFF_EPOCH_SQL — last *message* activity, falling back to mtime
#   scan_all_files/PROJECTS_DIR — transcript discovery (skips agent-* sidecars)
import session_indexer as indexer
from session_indexer import EFF_EPOCH_SQL, init_db, is_automated, lineage_info

# --- Paths / constants ---

OUT_PATH = os.path.join(os.path.expanduser("~"), ".claude", ".sessions-map.json")
USAGE_DB_PATH = os.path.join(os.path.expanduser("~"), ".claude", ".sessions-usage.db")

# A repo's worktree folders index as `<repo>--claude-worktrees-<name>`; the map
# charges their sessions to the repo (same rule as _resolve_duplicates).
WORKTREE_MARKER = "--claude-worktrees"

SCHEMA_VERSION = 1
PREVIEW_CHARS = 300

USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_daily (
    sid TEXT,
    date TEXT,
    input INTEGER DEFAULT 0,
    output INTEGER DEFAULT 0,
    cache_read INTEGER DEFAULT 0,
    cache_creation INTEGER DEFAULT 0,
    PRIMARY KEY (sid, date)
);

CREATE TABLE IF NOT EXISTS usage_files (
    sid TEXT PRIMARY KEY,
    mtime INTEGER DEFAULT 0,
    size INTEGER DEFAULT 0
);
"""


def base_project(project):
    """Collapse a worktree project label onto its base repo."""
    return (project or "").split(WORKTREE_MARKER)[0]


# --- Usage cache database ---

def init_usage_db(db_path=None):
    """Open (creating if needed) the per-day usage cache database."""
    conn = sqlite3.connect(db_path or USAGE_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(USAGE_SCHEMA)
    return conn


# --- Transcript parsing ---

def _local_date(ts, cache):
    """ISO-8601 timestamp -> local 'YYYY-MM-DD'. Cached per minute prefix.

    Millions of lines share a handful of minutes; parsing each one is the single
    most expensive thing in a cold run. Minute granularity (not hour) keeps the
    cache correct for zones with :30/:45 offsets.
    """
    key = ts[:16]
    hit = cache.get(key)
    if hit is not None:
        return hit
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    day = dt.astimezone().strftime("%Y-%m-%d")
    cache[key] = day
    return day


def parse_usage_file(path, fallback_date=None):
    """Per-day token usage for one transcript: {date: [in, out, cache_read, cache_creation]}.

    Each `message.id` is counted ONCE. Claude Code appends a line per streaming
    update, so the same assistant message recurs with identical or progressively
    larger usage numbers — the last occurrence is the final tally.
    """
    seen = {}  # message.id -> (date, input, output, cache_read, cache_creation)
    date_cache = {}
    try:
        f = open(path, "rb")
    except OSError:
        return {}
    with f:
        for line in f:
            # Cheap byte prefilter: only a minority of lines carry usage, and
            # json.loads on multi-KB tool-result lines is the hot cost. Matched
            # on substrings, not exact key spelling — whitespace between the key
            # and value is legal JSON and would slip past a `"type":"assistant"`
            # literal; the real type check happens after the parse.
            if b'"usage"' not in line or b'assistant' not in line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
                continue
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            ts = obj.get("timestamp")
            day = _local_date(ts, date_cache) if isinstance(ts, str) else None
            if day is None:
                day = fallback_date
            if day is None:
                continue
            # No id (shouldn't happen) -> key on identity so it still counts once.
            mid = msg.get("id") or f"_line{len(seen)}"
            seen[mid] = (
                day,
                int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0),
                int(usage.get("cache_read_input_tokens") or 0),
                int(usage.get("cache_creation_input_tokens") or 0),
            )

    daily = {}
    for day, i, o, cr, cc in seen.values():
        acc = daily.get(day)
        if acc is None:
            daily[day] = [i, o, cr, cc]
        else:
            acc[0] += i
            acc[1] += o
            acc[2] += cr
            acc[3] += cc
    return daily


# --- Incremental sync ---

def scan_transcripts(projects_dir=None):
    """{sid: (path, size, mtime)} for every transcript on disk.

    A sid can appear in several project dirs (a worktree stub plus the real
    file); the largest wins, matching the indexer.
    """
    if projects_dir:
        indexer.PROJECTS_DIR = projects_dir
    out = {}
    for path, sid, size, mtime, _label in indexer.scan_all_files():
        prev = out.get(sid)
        if prev is None or size > prev[1]:
            out[sid] = (path, size, int(mtime))
    return out


def sync_usage(uconn, file_map, sids=None, progress=True):
    """Parse transcripts whose (mtime, size) changed; return {sid: {date: [4 ints]}}.

    Unchanged files are served straight from the cache — the full parse happens
    at most once per file version.
    """
    cached = {sid: (m, s) for sid, m, s in
              uconn.execute("SELECT sid, mtime, size FROM usage_files")}

    targets = {sid: v for sid, v in file_map.items()
               if sids is None or sid in sids}

    # Drop rows for transcripts that no longer exist (or left the index).
    for sid in set(cached) - set(targets):
        uconn.execute("DELETE FROM usage_daily WHERE sid = ?", (sid,))
        uconn.execute("DELETE FROM usage_files WHERE sid = ?", (sid,))

    stale = [(sid, path, size, mtime) for sid, (path, size, mtime) in targets.items()
             if cached.get(sid) != (mtime, size)]

    parsed = 0
    for sid, path, size, mtime in stale:
        fallback = time.strftime("%Y-%m-%d", time.localtime(mtime))
        daily = parse_usage_file(path, fallback_date=fallback)
        uconn.execute("DELETE FROM usage_daily WHERE sid = ?", (sid,))
        uconn.executemany(
            "INSERT INTO usage_daily (sid, date, input, output, cache_read, cache_creation)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(sid, d, v[0], v[1], v[2], v[3]) for d, v in daily.items()])
        uconn.execute(
            "INSERT INTO usage_files (sid, mtime, size) VALUES (?, ?, ?)"
            " ON CONFLICT(sid) DO UPDATE SET mtime=excluded.mtime, size=excluded.size",
            (sid, mtime, size))
        parsed += 1
        if progress and parsed % 100 == 0:
            print(f"parsed {parsed}/{len(stale)} transcripts…", file=sys.stderr)
    uconn.commit()

    usage = {}
    for sid, date, i, o, cr, cc in uconn.execute(
            "SELECT sid, date, input, output, cache_read, cache_creation FROM usage_daily"):
        usage.setdefault(sid, {})[date] = [i, o, cr, cc]
    return usage


# --- Export ---

def build_export(conn, usage, include_automated=False, generated_at=None):
    """Assemble the JSON payload from index rows + parsed usage."""
    hidden, _, _ = lineage_info(conn)

    rows = conn.execute(f"""
        SELECT sid, title, preview, project, created_epoch, {EFF_EPOCH_SQL},
               size_bytes, parent_sid
        FROM sessions
    """).fetchall()

    sessions = []
    projects = {}
    daily = {}
    for sid, title, preview, project, created, eff, size_bytes, parent_sid in rows:
        preview = preview or ""
        if not include_automated and is_automated(preview):
            continue
        proj = base_project(project)
        tokens = usage.get(sid, {})
        tot = [0, 0, 0, 0]
        for date, v in tokens.items():
            acc = daily.setdefault((date, proj), [0, 0, 0, 0])
            for k in range(4):
                acc[k] += v[k]
                tot[k] += v[k]
        is_hidden = 1 if sid in hidden else 0

        pagg = projects.setdefault(proj, {"name": proj, "sessions": 0,
                                          "tokens_input": 0, "tokens_output": 0})
        # Session COUNT tracks what the front-end renders (hidden=0); token sums
        # cover the whole chain, superseded ancestors included.
        if not is_hidden:
            pagg["sessions"] += 1
        pagg["tokens_input"] += tot[0]
        pagg["tokens_output"] += tot[1]

        sessions.append({
            "sid": sid,
            "title": title or "",
            "preview": preview[:PREVIEW_CHARS],
            "project": proj,
            "created": created or 0,
            "eff": eff or 0,
            "size_bytes": size_bytes or 0,
            # '?' means "parent searched for, not found" — not a resolvable sid.
            "parent_sid": "" if (parent_sid or "") == "?" else (parent_sid or ""),
            "hidden": is_hidden,
            "tokens": {"input": tot[0], "output": tot[1],
                       "cache_read": tot[2], "cache_creation": tot[3]},
        })

    sessions.sort(key=lambda s: -s["eff"])
    project_list = sorted(projects.values(),
                          key=lambda p: (-(p["tokens_input"] + p["tokens_output"]), p["name"]))
    daily_list = [{"date": d, "project": p, "input": v[0], "output": v[1],
                   "cache_read": v[2], "cache_creation": v[3]}
                  for (d, p), v in sorted(daily.items())]

    return {
        "version": SCHEMA_VERSION,
        "generated_at": int(generated_at if generated_at is not None else time.time()),
        "projects": project_list,
        "sessions": sessions,
        "daily": daily_list,
    }


def write_export(payload, out_path):
    """Atomically write the payload; returns bytes written."""
    tmp = out_path + ".tmp"
    data = json.dumps(payload, ensure_ascii=False)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, out_path)
    return len(data.encode("utf-8"))


def export(out_path=None, include_automated=False, db_path=None,
           usage_db_path=None, projects_dir=None, progress=True):
    """Full run: sync usage from disk, join with the index, write JSON."""
    conn = init_db(db_path)
    uconn = init_usage_db(usage_db_path)
    try:
        db_sids = {r[0] for r in conn.execute("SELECT sid FROM sessions")}
        file_map = scan_transcripts(projects_dir)
        usage = sync_usage(uconn, file_map, sids=db_sids, progress=progress)
        payload = build_export(conn, usage, include_automated=include_automated)
    finally:
        conn.close()
        uconn.close()
    path = out_path or OUT_PATH
    size = write_export(payload, path)
    return payload, path, size


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-o", "--out", default=OUT_PATH,
                    help=f"output JSON path (default: {OUT_PATH})")
    ap.add_argument("--include-automated", action="store_true",
                    help="include machine-spawned sessions (security reviews, probes)")
    ap.add_argument("--db", default=None, help="session index database (testing)")
    ap.add_argument("--usage-db", default=None, help="usage cache database (testing)")
    ap.add_argument("--projects-dir", default=None,
                    help="transcript root, default ~/.claude/projects (testing)")
    ap.add_argument("--quiet", action="store_true", help="suppress progress output")
    args = ap.parse_args()

    t0 = time.monotonic()
    payload, path, size = export(
        out_path=args.out,
        include_automated=args.include_automated,
        db_path=args.db,
        usage_db_path=args.usage_db,
        projects_dir=args.projects_dir,
        progress=not args.quiet,
    )
    if not args.quiet:
        elapsed = time.monotonic() - t0
        dates = [d["date"] for d in payload["daily"]]
        span = f"{dates[0]}..{dates[-1]}" if dates else "none"
        print(f"{path}: {len(payload['sessions'])} sessions, "
              f"{len(payload['projects'])} projects, {len(payload['daily'])} daily rows "
              f"({span}), {size / 1000:.0f}kB, {elapsed:.2f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
