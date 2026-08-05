#!/usr/bin/env python3
"""Tests for the session map JSON exporter.

Covers the parts that can silently produce wrong numbers: streaming-duplicate
message dedup, local-date attribution, worktree collapse, automated exclusion,
lineage hidden flags, the incremental parse cache, and missing transcripts.

Run: python3 test_map_export.py
"""

import json, os, shutil, sys, tempfile, unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import session_map_export as mx
from session_indexer import init_db, index_session


def local_date(ts):
    """Independent expected-value helper: ISO timestamp -> local YYYY-MM-DD."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")


# Real transcripts are compact JSON (no spaces after ':'); fixtures mirror that.
def compact(obj):
    return json.dumps(obj, separators=(",", ":"))


def usage_line(mid, ts, inp=0, out=0, cr=0, cc=0, type_="assistant"):
    return compact({
        "type": type_,
        "timestamp": ts,
        "message": {
            "id": mid,
            "role": "assistant",
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cc,
                "cache_creation": {"ephemeral_5m_input_tokens": cc},
                "service_tier": "standard",
            },
        },
    })


def user_line(ts, text="hello"):
    return compact({"type": "user", "timestamp": ts,
                    "message": {"role": "user", "content": text}})


class ExportTestCase(unittest.TestCase):
    """Temp index DB + temp usage DB + temp ~/.claude/projects tree."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mapexport-")
        self.db_path = os.path.join(self.tmp, "sessions.db")
        self.usage_db_path = os.path.join(self.tmp, "usage.db")
        self.projects = os.path.join(self.tmp, "projects")
        os.makedirs(self.projects)
        self.conn = init_db(self.db_path)
        self._saved_projects_dir = mx.indexer.PROJECTS_DIR

    def tearDown(self):
        self.conn.close()
        mx.indexer.PROJECTS_DIR = self._saved_projects_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- fixture helpers ---

    def add_session(self, sid, project="repo", preview="do the thing", title="",
                    created=1000, mtime=2000, last_activity=0, size=123,
                    parent_sid=""):
        index_session(self.conn, sid, title, preview, "body text", project,
                      created, mtime, size, last_activity)
        if parent_sid:
            self.conn.execute(
                "UPDATE sessions SET parent_sid = ?, parent_uuid = 'u-' || ?,"
                " is_fork = 1 WHERE sid = ?", (parent_sid, parent_sid, sid))
        self.conn.commit()

    def write_transcript(self, sid, lines, project_dir="-Users-x-github-repo"):
        d = os.path.join(self.projects, project_dir)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, sid + ".jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
        return path

    def run_export(self, **kw):
        out = os.path.join(self.tmp, "map.json")
        payload, path, _size = mx.export(
            out_path=out, db_path=self.db_path, usage_db_path=self.usage_db_path,
            projects_dir=self.projects, progress=False, **kw)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["version"], payload["version"])
        return payload

    def by_sid(self, payload):
        return {s["sid"]: s for s in payload["sessions"]}


class TestUsageDedup(ExportTestCase):
    """The same message.id repeats on every streaming append — count it once."""

    def test_repeated_message_id_counted_once_last_wins(self):
        ts = "2026-03-04T12:00:00.000Z"
        self.add_session("s1")
        self.write_transcript("s1", [
            usage_line("msg_a", ts, inp=3, out=10, cr=100, cc=5),
            usage_line("msg_a", ts, inp=3, out=42, cr=100, cc=5),   # streaming update
            usage_line("msg_a", ts, inp=3, out=42, cr=100, cc=5),   # final
            usage_line("msg_b", ts, inp=7, out=1, cr=0, cc=0),
        ])
        tok = self.by_sid(self.run_export())["s1"]["tokens"]
        self.assertEqual(tok, {"input": 10, "output": 43,
                               "cache_read": 100, "cache_creation": 5})

    def test_non_assistant_and_usageless_lines_ignored(self):
        ts = "2026-03-04T12:00:00.000Z"
        self.add_session("s1")
        self.write_transcript("s1", [
            user_line(ts),
            usage_line("msg_x", ts, inp=999, out=999, type_="system"),
            compact({"type": "assistant", "timestamp": ts,
                     "message": {"id": "msg_y", "usage": "not-a-dict"}}),
            compact({"type": "assistant", "timestamp": ts,
                     "message": {"id": "msg_z"}}),
            "{not json at all",
            usage_line("msg_ok", ts, inp=5, out=6),
        ])
        tok = self.by_sid(self.run_export())["s1"]["tokens"]
        self.assertEqual((tok["input"], tok["output"]), (5, 6))


class TestDateAttribution(ExportTestCase):
    """Tokens land on the calendar date (local tz) of the line's timestamp."""

    def test_split_across_days(self):
        d1, d2 = "2026-03-04T12:00:00.000Z", "2026-03-06T12:00:00.000Z"
        self.add_session("s1", project="repo")
        self.write_transcript("s1", [
            usage_line("m1", d1, inp=10, out=1),
            usage_line("m2", d1, inp=5, out=2),
            usage_line("m3", d2, inp=100, out=9),
        ])
        payload = self.run_export()
        rows = {(r["date"], r["project"]): r for r in payload["daily"]}
        self.assertEqual(rows[(local_date(d1), "repo")]["input"], 15)
        self.assertEqual(rows[(local_date(d1), "repo")]["output"], 3)
        self.assertEqual(rows[(local_date(d2), "repo")]["input"], 100)

    def test_daily_sorted_by_date_ascending(self):
        self.add_session("s1")
        self.write_transcript("s1", [
            usage_line("m1", "2026-03-09T12:00:00.000Z", inp=1),
            usage_line("m2", "2026-03-05T12:00:00.000Z", inp=1),
            usage_line("m3", "2026-03-07T12:00:00.000Z", inp=1),
        ])
        dates = [r["date"] for r in self.run_export()["daily"]]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(len(dates), 3)


class TestWorktreeCollapse(ExportTestCase):
    """Worktree project labels charge to the base repo, in sessions AND daily."""

    def test_worktree_sessions_merge_into_base_project(self):
        ts = "2026-03-04T12:00:00.000Z"
        self.add_session("main1", project="repo")
        self.add_session("wt1", project="repo--claude-worktrees-fix-thing")
        self.write_transcript("main1", [usage_line("a", ts, inp=10, out=1)])
        self.write_transcript("wt1", [usage_line("b", ts, inp=20, out=2)],
                              project_dir="-Users-x-github-repo--claude-worktrees-fix-thing")
        payload = self.run_export()
        self.assertEqual({s["project"] for s in payload["sessions"]}, {"repo"})
        self.assertEqual([p["name"] for p in payload["projects"]], ["repo"])
        self.assertEqual(payload["projects"][0]["sessions"], 2)
        self.assertEqual(payload["projects"][0]["tokens_input"], 30)
        self.assertEqual(len(payload["daily"]), 1)
        self.assertEqual(payload["daily"][0]["project"], "repo")
        self.assertEqual(payload["daily"][0]["input"], 30)


class TestAutomatedExclusion(ExportTestCase):
    """Machine-spawned sessions are out unless explicitly asked for."""

    AUTO_PREVIEW = "Review this change for security vulnerabilities. Changed files"

    def setUp(self):
        super().setUp()
        ts = "2026-03-04T12:00:00.000Z"
        self.add_session("real", preview="fix the email sender")
        self.add_session("auto", preview=self.AUTO_PREVIEW)
        self.write_transcript("real", [usage_line("a", ts, inp=10)])
        self.write_transcript("auto", [usage_line("b", ts, inp=999)])

    def test_excluded_by_default_including_from_aggregates(self):
        payload = self.run_export()
        self.assertEqual(list(self.by_sid(payload)), ["real"])
        self.assertEqual(payload["projects"][0]["tokens_input"], 10)
        self.assertEqual(payload["daily"][0]["input"], 10)

    def test_included_with_flag(self):
        payload = self.run_export(include_automated=True)
        self.assertEqual(set(self.by_sid(payload)), {"real", "auto"})
        self.assertEqual(payload["projects"][0]["tokens_input"], 1009)


class TestHiddenLineage(ExportTestCase):
    """Superseded fork ancestors ship with hidden=1 and still count."""

    def test_ancestor_hidden_but_exported_and_counted(self):
        ts = "2026-03-04T12:00:00.000Z"
        self.add_session("old", mtime=1000)
        self.add_session("new", mtime=2000, parent_sid="old")
        self.write_transcript("old", [usage_line("a", ts, inp=10)])
        self.write_transcript("new", [usage_line("b", ts, inp=20)])
        payload = self.run_export()
        s = self.by_sid(payload)
        self.assertEqual(s["old"]["hidden"], 1)
        self.assertEqual(s["new"]["hidden"], 0)
        self.assertEqual(s["new"]["parent_sid"], "old")
        # Tokens of the hidden ancestor still land in the aggregates…
        self.assertEqual(payload["daily"][0]["input"], 30)
        self.assertEqual(payload["projects"][0]["tokens_input"], 30)
        # …but the session COUNT tracks what the front-end renders.
        self.assertEqual(payload["projects"][0]["sessions"], 1)

    def test_revived_parent_not_hidden(self):
        ts = "2026-03-04T12:00:00.000Z"
        self.add_session("parent", mtime=3000)
        self.add_session("child", mtime=2000, parent_sid="parent")
        self.write_transcript("parent", [usage_line("a", ts, inp=1)])
        self.write_transcript("child", [usage_line("b", ts, inp=1)])
        self.assertEqual({s["hidden"] for s in self.run_export()["sessions"]}, {0})

    def test_unresolved_parent_marker_not_emitted_as_sid(self):
        self.add_session("a")
        self.conn.execute("UPDATE sessions SET parent_sid = '?' WHERE sid = 'a'")
        self.conn.commit()
        self.assertEqual(self.by_sid(self.run_export())["a"]["parent_sid"], "")


class TestIncrementalCache(ExportTestCase):
    """A file is parsed once; only changed files are re-parsed."""

    def setUp(self):
        super().setUp()
        ts = "2026-03-04T12:00:00.000Z"
        for sid, n in (("s1", 10), ("s2", 20), ("s3", 30)):
            self.add_session(sid)
            self.write_transcript(sid, [usage_line("m" + sid, ts, inp=n)])

    def parsed_paths(self, **kw):
        """Run an export, returning which transcripts actually got parsed."""
        seen = []
        real = mx.parse_usage_file

        def spy(path, fallback_date=None):
            seen.append(os.path.basename(path))
            return real(path, fallback_date=fallback_date)

        mx.parse_usage_file = spy
        try:
            payload = self.run_export(**kw)
        finally:
            mx.parse_usage_file = real
        return seen, payload

    def test_cold_run_parses_everything_warm_run_parses_nothing(self):
        seen, _ = self.parsed_paths()
        self.assertEqual(sorted(seen), ["s1.jsonl", "s2.jsonl", "s3.jsonl"])
        seen2, payload = self.parsed_paths()
        self.assertEqual(seen2, [])
        # Cached numbers are still correct without touching a transcript.
        self.assertEqual(self.by_sid(payload)["s2"]["tokens"]["input"], 20)

    def test_only_the_changed_file_is_reparsed(self):
        self.parsed_paths()
        ts = "2026-03-05T12:00:00.000Z"
        path = self.write_transcript("s2", [usage_line("m2new", ts, inp=77)])
        os.utime(path, (9_000_000_000, 9_000_000_000))  # bump mtime deterministically
        seen, payload = self.parsed_paths()
        self.assertEqual(seen, ["s2.jsonl"])
        self.assertEqual(self.by_sid(payload)["s2"]["tokens"]["input"], 77)
        # Reinsert-not-append: the old day's row for s2 is gone.
        rows = mx.init_usage_db(self.usage_db_path).execute(
            "SELECT date FROM usage_daily WHERE sid = 's2'").fetchall()
        self.assertEqual([r[0] for r in rows], [local_date(ts)])

    def test_file_bookkeeping_records_mtime_and_size(self):
        self.parsed_paths()
        uconn = mx.init_usage_db(self.usage_db_path)
        rows = dict((sid, (m, s)) for sid, m, s in
                    uconn.execute("SELECT sid, mtime, size FROM usage_files"))
        self.assertEqual(sorted(rows), ["s1", "s2", "s3"])
        st = os.stat(os.path.join(self.projects, "-Users-x-github-repo", "s1.jsonl"))
        self.assertEqual(rows["s1"], (int(st.st_mtime), st.st_size))

    def test_deleted_transcript_purges_cache_rows(self):
        self.parsed_paths()
        os.unlink(os.path.join(self.projects, "-Users-x-github-repo", "s3.jsonl"))
        self.run_export()
        uconn = mx.init_usage_db(self.usage_db_path)
        left = [r[0] for r in uconn.execute("SELECT sid FROM usage_files")]
        self.assertNotIn("s3", left)
        self.assertEqual(uconn.execute(
            "SELECT count(*) FROM usage_daily WHERE sid = 's3'").fetchone()[0], 0)


class TestMissingTranscript(ExportTestCase):
    """An indexed session whose file is gone exports zeros, not an error."""

    def test_zero_tokens_no_error(self):
        self.add_session("ghost")
        payload = self.run_export()
        s = self.by_sid(payload)["ghost"]
        self.assertEqual(s["tokens"], {"input": 0, "output": 0,
                                       "cache_read": 0, "cache_creation": 0})
        self.assertEqual(payload["daily"], [])
        self.assertEqual(payload["projects"][0]["sessions"], 1)

    def test_parse_of_nonexistent_path_returns_empty(self):
        self.assertEqual(mx.parse_usage_file("/no/such/file.jsonl"), {})


class TestOutputContract(ExportTestCase):
    """Field names and shapes the front-end is built against."""

    def test_top_level_shape(self):
        self.add_session("s1")
        payload = self.run_export()
        self.assertEqual(payload["version"], 1)
        self.assertIsInstance(payload["generated_at"], int)
        self.assertEqual(sorted(payload), ["daily", "generated_at", "projects",
                                           "sessions", "version"])

    def test_session_fields(self):
        self.add_session("s1", title="Title", preview="P" * 500,
                         created=111, mtime=222, last_activity=333, size=44)
        s = self.by_sid(self.run_export())["s1"]
        self.assertEqual(sorted(s), ["created", "eff", "hidden", "parent_sid",
                                     "preview", "project", "sid", "size_bytes",
                                     "title", "tokens"])
        self.assertEqual(s["created"], 111)
        self.assertEqual(s["eff"], 333)          # last_activity wins
        self.assertEqual(s["size_bytes"], 44)
        self.assertEqual(len(s["preview"]), 300)  # truncated

    def test_eff_falls_back_to_mtime(self):
        self.add_session("s1", mtime=222, last_activity=0)
        self.assertEqual(self.by_sid(self.run_export())["s1"]["eff"], 222)

    def test_sessions_sorted_by_eff_desc(self):
        self.add_session("a", mtime=100)
        self.add_session("b", mtime=300)
        self.add_session("c", mtime=200)
        payload = self.run_export()
        self.assertEqual([s["sid"] for s in payload["sessions"]], ["b", "c", "a"])

    def test_daily_row_fields(self):
        self.add_session("s1")
        self.write_transcript("s1", [
            usage_line("m", "2026-03-04T12:00:00.000Z", inp=1, out=2, cr=3, cc=4)])
        row = self.run_export()["daily"][0]
        self.assertEqual(sorted(row), ["cache_creation", "cache_read", "date",
                                       "input", "output", "project"])
        self.assertEqual((row["input"], row["output"], row["cache_read"],
                          row["cache_creation"]), (1, 2, 3, 4))

    def test_project_row_fields(self):
        self.add_session("s1")
        row = self.run_export()["projects"][0]
        self.assertEqual(sorted(row), ["name", "sessions", "tokens_input",
                                       "tokens_output"])


if __name__ == "__main__":
    unittest.main()
