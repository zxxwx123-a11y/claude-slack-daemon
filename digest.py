#!/usr/bin/env python3
"""Proactive inbox digest — scan your unread Gmail + Slack @-mentions, let your
local Claude Code decide what actually needs you, and push a short digest to your
own Slack DM.

Strictly read-only: it never sends, replies, or marks anything read. Dedup (a seen
ledger) + an optional work-hours window keep it quiet. Run it on a timer
(cron / launchd StartInterval, e.g. every 30 min).

Enable in config:  [digest] enabled = true
Needs:  SLACK_USER_TOKEN (search:read) for mentions; Gmail is optional
        (GMAIL_ADDRESS + GMAIL_APP_PASSWORD app password).
"""
import os
import sys
import json
import time
import pathlib
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _common as C  # noqa: E402

cfg = C.load_config()
DG = cfg.get("digest", {})
if not DG.get("enabled"):
    sys.exit(0)
if not C.in_work_hours(DG.get("work_hours")):
    sys.exit(0)

SEEN = C.STATE / "digest_seen.json"
seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()

sources = DG.get("sources", ["slack", "gmail"])
bt = C.bot_token()
ut = C.user_token()
oid = C.owner_id(cfg)
owner_ctx = cfg.get("bot", {}).get("owner_context", "")
handle = cfg.get("bot", {}).get("slack_handle", "")

# --- Slack @-mentions (search.messages already carries permalinks) ---
slack_items = []
if "slack" in sources and ut and handle:
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - 2 * 86400))
    r = C.slack_get(ut, "search.messages",
                    {"query": f"@{handle} after:{since}", "count": "25", "sort": "timestamp"})
    for m in r.get("messages", {}).get("matches", []):
        uid = "slack:" + m.get("ts", "")
        if uid in seen:
            continue
        slack_items.append({
            "uid": uid,
            "who": m.get("username") or m.get("user", ""),
            "channel": (m.get("channel") or {}).get("name", ""),
            "text": (m.get("text") or "")[:200],
            "link": m.get("permalink", ""),
        })

# --- Gmail unread (PEEK — never marks read) + a deep link back to the message ---
gmail_items = []
C.load_env()
gmail_user = os.environ.get("GMAIL_ADDRESS", "")
gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
if "gmail" in sources and gmail_user and gmail_pass:
    import imaplib
    import email
    from email.header import decode_header

    def _dh(s):
        if not s:
            return ""
        return "".join(
            t.decode(enc or "utf-8", "ignore") if isinstance(t, bytes) else t
            for t, enc in decode_header(s))

    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(gmail_user, gmail_pass)
        M.select("INBOX")
        _, data = M.search(None, "UNSEEN")
        for i in data[0].split()[-40:]:
            _, d = M.fetch(i, "(BODY.PEEK[HEADER])")
            msg = email.message_from_bytes(d[0][1])
            mid = msg.get("Message-ID", "")
            uid = "mail:" + mid
            if not mid or uid in seen:
                continue
            link = ("https://mail.google.com/mail/u/0/#search/"
                    + urllib.parse.quote("rfc822msgid:" + mid.strip("<>")))
            gmail_items.append({
                "uid": uid,
                "from": _dh(msg.get("From", "")),
                "subject": _dh(msg.get("Subject", "")),
                "link": link,
            })
        M.logout()
    except Exception as e:
        sys.stderr.write(f"imap error: {e}\n")

fresh = slack_items + gmail_items
if not fresh:
    sys.exit(0)

# --- Claude triages: which of these actually need the owner, with the link kept ---
prompt = (
    "Below are NEW unread items (Slack @-mentions and/or unread emails), each with a clickable link.\n"
    + (f"Context about the owner (use it to judge what matters): {owner_ctx}\n" if owner_ctx else "")
    + "Pick ONLY items that genuinely need the owner's attention or a reply. "
      "Ignore newsletters, automated notifications, marketing, and noise.\n"
      "If NONE are worth flagging, reply with EXACTLY: SKIP\n"
      "Otherwise output Slack mrkdwn, grouped, dropping any empty group:\n"
      "*Slack*\n• *<#channel or sender>* — <one line: why it matters / what's needed> <LINK|open>\n"
      "*Email*\n• *<sender>* — <one line: why it matters / what's needed> <LINK|open>\n"
      "Use each item's exact `link` value as the URL inside <LINK|open>. Be tight, no preamble.\n\n"
    + json.dumps({"slack": slack_items, "email": gmail_items}, ensure_ascii=False)
)
agent = cfg.get("agent", {})
out = C.claude_run(prompt, agent.get("claude_bin", "claude"),
                   perm=agent.get("permission_mode", "acceptEdits"))
if out is None:
    sys.exit(0)  # transient error — don't burn the seen ledger; retry next run

seen.update(x["uid"] for x in fresh)
SEEN.write_text(json.dumps(list(seen)[-1500:]))

if not out or out.upper().startswith("SKIP"):
    sys.exit(0)

res = C.post_dm(bt, oid, f"📬 *Inbox check* · {time.strftime('%H:%M')}\n{out}")
if not res.get("ok"):
    sys.stderr.write(f"push error: {res.get('error')}\n")
