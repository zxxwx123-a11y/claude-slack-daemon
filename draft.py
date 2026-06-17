#!/usr/bin/env python3
"""Draft assistant — when someone @-mentions you in Slack, read the FULL thread,
let your local Claude Code draft a reply in the thread's language and tone, and push
the draft to your own DM.

It NEVER sends. You read the draft, then hit send yourself in the real thread. This
is deliberate: auto-replying in work channels is a liability you don't want a bot to
own. Run it on a short timer (cron / launchd StartInterval ~150s = near-real-time).

Enable in config:  [draft] enabled = true
Needs:  SLACK_USER_TOKEN with search:read (find mentions) + channels:history /
        groups:history (read the thread).
"""
import os
import sys
import json
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _common as C  # noqa: E402

cfg = C.load_config()
DR = cfg.get("draft", {})
if not DR.get("enabled"):
    sys.exit(0)
if not C.in_work_hours(DR.get("work_hours")):
    sys.exit(0)

ut = C.user_token()
bt = C.bot_token()
oid = C.owner_id(cfg)
me = cfg.get("bot", {}).get("slack_handle", "")
owner_ctx = cfg.get("bot", {}).get("owner_context", "")
if not (ut and me):
    sys.stderr.write("draft: needs SLACK_USER_TOKEN and bot.slack_handle\n")
    sys.exit(0)

SEEN = C.STATE / "draft_seen.json"
seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()

since = time.strftime("%Y-%m-%d", time.localtime(time.time() - 2 * 86400))
r = C.slack_get(ut, "search.messages",
                {"query": f"@{me} after:{since}", "count": "20", "sort": "timestamp"})
matches = r.get("messages", {}).get("matches", [])

# Keep only: someone ELSE @-mentioning you, with real content, not a bot/CI/alert channel.
cands = []
for m in matches:
    ts = m.get("ts", "")
    if ("draft:" + ts) in seen:
        continue
    who = (m.get("username") or m.get("user", "")).lower()
    text = (m.get("text") or "").strip()
    cname = ((m.get("channel") or {}).get("name") or "").lower()
    if who == me.lower() or not text:
        continue
    if any(x in cname for x in ("alert", "-ci", "deploy", "log")) or \
       any(x in who for x in ("ci", "bot", "alert")):
        continue
    cands.append(m)

if not cands:
    sys.exit(0)

# Take the most recent candidate; read its full thread for context.
m = cands[0]
ch = m.get("channel") or {}
th = C.slack_get(ut, "conversations.replies",
                 {"channel": ch.get("id"), "ts": m.get("ts"), "limit": "25"})
if th.get("ok") and th.get("messages"):
    ctx = "\n".join(f"{t.get('user', '?')}: {(t.get('text') or '')[:400]}"
                    for t in th["messages"])
else:
    ctx = m.get("text") or ""

prompt = (
    f"Someone @-mentioned the owner in a Slack channel (#{ch.get('name')}). The FULL thread "
    f"context is below — read ALL of it before drafting.\n\n--- THREAD ---\n{ctx}\n--- END ---\n\n"
    + (f"Context about the owner: {owner_ctx}\n\n" if owner_ctx else "")
    + "Draft ONE concise, professional reply the owner could send, matching the thread's language "
      "and tone, addressing what was actually asked. Output ONLY the draft reply text — no preamble, "
      "no quotes, no explanation."
)
agent = cfg.get("agent", {})
draft = C.claude_run(prompt, agent.get("claude_bin", "claude"),
                     perm=agent.get("permission_mode", "acceptEdits"))
if not draft:
    sys.exit(0)

msg = (f"🔔 *Someone @-mentioned you* · #{ch.get('name')}\n"
       f"> {(m.get('text') or '')[:240]}\n\n"
       f"💡 *Suggested reply (review before sending):*\n{draft}\n\n"
       f"<{m.get('permalink')}|→ open thread to send>")
res = C.post_dm(bt, oid, msg)
seen.add("draft:" + m.get("ts", ""))
SEEN.write_text(json.dumps(list(seen)[-500:]))
if not res.get("ok"):
    sys.stderr.write(f"push error: {res.get('error')}\n")
