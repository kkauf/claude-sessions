#!/usr/bin/env python3
"""Generate demo session fixtures for screenshots.

Creates fake-but-plausible Claude Code transcripts under demo/projects/, plus
env exports to point the picker at them:

    python3 demo/gen-fixtures.py
    export SESSION_PROJECTS_DIR=$PWD/demo/projects \
           SESSION_DB_PATH=$PWD/demo/sessions.db \
           SESSION_CACHE_PATH=$PWD/demo/cache.tsv
    claude-sessions            # or: claude-sessions "auth token"

Includes a compact-fork pair (↪), an active session (●), and varied
projects/dates/sizes. Filler tool_use lines inflate file sizes realistically
without polluting the index.
"""

import json
import functools

_dumps = json.dumps
json.dumps = functools.partial(_dumps, separators=(",", ":"))
import os
import uuid
from datetime import datetime, timedelta, timezone

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects")
HOME_KEY = os.path.expanduser("~").replace("/", "-").lstrip("-")
NOW = datetime.now(timezone.utc)

FILLER = json.dumps({
    "type": "assistant",
    "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t0",
                "name": "Bash", "input": {"command": "npm test -- --reporter=dot"}}]},
    "uuid": "filler",
})


def ts(delta):
    return (NOW - delta).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def user(text, when):
    return json.dumps({"type": "user", "timestamp": ts(when),
                       "message": {"role": "user", "content": text},
                       "uuid": str(uuid.uuid4())})


def asst(text, when):
    return json.dumps({"type": "assistant", "timestamp": ts(when),
                       "message": {"role": "assistant",
                                   "content": [{"type": "text", "text": text}]},
                       "uuid": str(uuid.uuid4())})


def title(name):
    return json.dumps({"type": "ai-title", "aiTitle": name})


def write_session(project, sid, lines, filler_kb=0):
    d = os.path.join(BASE, f"-{HOME_KEY}-{project}")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{sid}.jsonl"), "w") as f:
        f.write("\n".join(lines) + "\n")
        for _ in range(filler_kb * 1024 // (len(FILLER) + 1)):
            f.write(FILLER + "\n")


def convo(name, age, pairs):
    """ai-title + alternating user/assistant messages ending `age` ago."""
    lines = [title(name)]
    step = timedelta(minutes=4)
    when = age + step * len(pairs) * 2
    for u, a in pairs:
        lines.append(user(u, when)); when -= step
        lines.append(asst(a, when)); when -= step
    return lines


D = timedelta

# 1 — active right now (red ● in the picker)
write_session("github-api-server", "d1a7e2b0-1111-4a6b-9c1d-000000000001", convo(
    "Debug flaky websocket reconnect test",
    D(seconds=30),
    [("The reconnect test fails maybe 1 in 5 runs on CI. Passes locally every time.",
      "Classic timing flake. The test asserts reconnection within 100ms but CI containers are slower. Let me look at the retry backoff."),
     ("Can we make the test deterministic instead of bumping the timeout?",
      "Yes — inject a fake timer and advance it manually. I'll refactor the backoff to accept a clock."),
     ("Do it, and check if other tests use real timers too.",
      "Refactored with an injectable clock; found 3 more tests using real sleeps and converted them. 40 consecutive green runs locally.")]),
    filler_kb=1800)

# 2 — compact-fork pair: parent (3w) superseded by child (2d) → one ↪ entry
parent_uuid = "af000000-0000-4000-8000-00000000cafe"
parent_lines = convo(
    "Design multi-tenant billing system",
    D(days=21),
    [("We need per-tenant usage metering before the enterprise deal closes. Stripe or homegrown?",
      "Given metered seats + overage tiers, Stripe Billing covers 80%. The gap is per-tenant proration on mid-cycle plan switches."),
     ("Enterprise wants invoices net-30, self-serve stays on cards.",
      "Then split by collection method: subscriptions with invoice collection for enterprise, charge_automatically for self-serve.")])
parent_lines.append(json.dumps({
    "type": "user", "timestamp": ts(D(days=21)),
    "message": {"role": "user", "content": "Draft the schema for the metering tables."},
    "uuid": parent_uuid}))
parent_lines.append(asst("Here's the schema: usage_events (tenant_id, meter, qty, ts) with daily rollups into usage_daily. Idempotency via event_id unique index.", D(days=21)))
write_session("github-api-server", "b2000000-2222-4b00-9000-000000000002",
              parent_lines, filler_kb=700)

child_lines = [
    title("Design multi-tenant billing system"),
    json.dumps({"type": "system", "subtype": "compact_boundary",
                "logicalParentUuid": parent_uuid, "content": "Conversation compacted",
                "compactMetadata": {"trigger": "auto", "preTokens": 180000}}),
    json.dumps({"type": "user", "timestamp": ts(D(days=2, hours=3)),
                "message": {"role": "user", "content":
                            "This session is being continued from a previous conversation that ran out of context. Summary: designing multi-tenant billing with Stripe, metering schema drafted."},
                "isCompactSummary": True, "uuid": str(uuid.uuid4())}),
    user("Metering tables are live in staging. Now wire the Stripe usage records sync.", D(days=2, hours=2)),
    asst("Sync job pushes usage_daily deltas to Stripe meters hourly, with a reconciliation pass at invoice finalization. Draft PR is up.", D(days=2)),
]
write_session("github-api-server", "c3000000-3333-4c00-a000-000000000003",
              child_lines, filler_kb=250)

# 3 — search-demo target: rich in "auth token"
write_session("github-web-app", "e4000000-4444-4d00-b000-000000000004", convo(
    "Fix auth token refresh loop on Safari",
    D(days=1, hours=4),
    [("Users on Safari get logged out every ~10 minutes. Chrome is fine.",
      "Safari's ITP caps script-writable storage at 7 days and partitions it aggressively — if the auth token refresh writes to localStorage, that's the trigger."),
     ("So the refresh token never persists? Where do we put it?",
      "Move the refresh token to an httpOnly SameSite=Lax cookie; keep the short-lived access token in memory. The auth token refresh then survives ITP."),
     ("Ship it behind a flag and add a Safari e2e run.",
      "Flagged rollout done, Safari 18 e2e job added. Token refresh holds through a 30-minute idle test.")]),
    filler_kb=950)

# 4-10 — variety across projects, ages, sizes
write_session("github-web-app", "f5000000-5555-4e00-c000-000000000005", convo(
    "Migrate checkout to server components",
    D(days=4),
    [("Checkout bundle is 480kb, mostly the card form. Can server components help?",
      "Everything except the payment element itself can move server-side. Estimate: 480kb → ~90kb client JS."),
     ("What breaks?", "Optimistic quantity updates need a client island; the rest is forms + redirects.")]),
    filler_kb=2800)

write_session("dotfiles", "a6000000-6666-4f00-d000-000000000006", convo(
    "Set up zsh keybindings for tmux popup",
    D(days=7),
    [("I want ctrl-g to open a tmux popup with my scratch notes.",
      "bindkey -s plus display-popup -E. Here's the snippet with a toggle.")]),
    filler_kb=80)

write_session("github-data-pipeline", "b7000000-7777-4a10-e000-000000000007", convo(
    "Backfill missing analytics events",
    D(days=2, hours=8),
    [("The tracker was down March 3-5. Can we reconstruct signup events from the app DB?",
      "Yes — users.created_at gives us signups; I'll emit synthetic events with a backfill=true flag so dashboards can exclude them."),
     ("Run it against staging first and diff the funnel numbers.",
      "Staging diff looks right: 312 reconstructed signups, funnel conversion within 0.4% of the surrounding weeks.")]),
    filler_kb=550)

write_session("github-api-server", "c8000000-8888-4b10-f000-000000000008", convo(
    "Add rate limiting to public API",
    D(days=14),
    [("We got scraped last night — 2M requests from one ASN. Rate limit the public API.",
      "Token bucket per API key + per-IP fallback for anonymous. 429 with Retry-After. Redis-backed, fails open."),
     ("Fails open? Justify.", "If Redis is down, serving traffic beats dropping legitimate auth token requests; alerting covers the gap.")]),
    filler_kb=420)

write_session("github-web-app", "d9000000-9999-4c10-a100-000000000009", convo(
    "Week 12 planning — ship list and cut lines",
    D(days=5),
    [("What ships this week and what gets cut?",
      "Ship: checkout migration, Safari auth fix, rate limits. Cut: dark mode (design not final), CSV export (no pull).")]),
    filler_kb=110)

write_session("github-data-pipeline", "ea000000-aaaa-4d10-b100-00000000000a", convo(
    "Profile slow Parquet ingestion job",
    D(days=3),
    [("Nightly ingestion went from 20 to 95 minutes over a month. Nothing obvious in the code.",
      "Row-group size drifted — upstream started writing 1000-row groups. Coalescing to 128MB groups before the join brings it back to 22 minutes.")]),
    filler_kb=1100)

write_session("dotfiles", "fb000000-bbbb-4e10-c100-00000000000b", convo(
    "Write install script for new laptop",
    D(days=21),
    [("New machine Friday. Script the whole setup: brew, dotfiles, keys, apps.",
      "One idempotent install.sh: Brewfile bundle, stow for dotfiles, mas for App Store, a manual-steps checklist for keys.")]),
    filler_kb=40)

print(f"Fixtures written to {BASE}")
print("export SESSION_PROJECTS_DIR=" + BASE)
