#!/usr/bin/env python3
"""Shared helpers for the standalone, scheduled scripts (digest.py, draft.py, tasks.py).

agent.py keeps its own config/token loading and uses the slack_bolt client, so it does
NOT import this. These helpers exist so the cron/launchd-launched scripts read the same
config.toml + .env, resolve the owner's DM channel, and talk to the Slack Web API —
with no third-party dependencies (stdlib only).
"""
import os
import sys
import json
import time
import pathlib
import urllib.request
import urllib.parse

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

ROOT = pathlib.Path(__file__).resolve().parent
STATE = pathlib.Path(os.environ.get(
    "CCSLACK_STATE", str(pathlib.Path.home() / ".claude-slack-daemon")))
STATE.mkdir(parents=True, exist_ok=True)


def load_env() -> None:
    """Merge the repo-root .env into os.environ WITHOUT overriding already-set vars.

    The daemon is usually started with these exported already; scripts launched on a
    bare cron/launchd timer are not — this gives them the same tokens.
    """
    envp = pathlib.Path(os.environ.get("CCSLACK_ENV", str(ROOT / ".env")))
    if not envp.exists():
        return
    for line in envp.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config() -> dict:
    path = pathlib.Path(os.environ.get("CCSLACK_CONFIG", str(ROOT / "config.toml")))
    if not path.exists():
        raise SystemExit(f"config not found: {path}  (copy config.example.toml -> config.toml)")
    return tomllib.loads(path.read_text())


def bot_token() -> str:
    load_env()
    t = os.environ.get("SLACK_BOT_TOKEN", "")
    if not t:
        raise SystemExit("SLACK_BOT_TOKEN not set (.env)")
    return t


def user_token() -> str:
    """Optional — only the digest/draft features need it (search:read + history)."""
    load_env()
    return os.environ.get("SLACK_USER_TOKEN", "")


def owner_id(cfg: dict) -> str:
    """The single owner's Slack user id: explicit bot.owner_slack_id, else first allowlisted id."""
    explicit = cfg.get("bot", {}).get("owner_slack_id", "")
    if explicit:
        return explicit
    ids = cfg.get("access", {}).get("allowed_user_ids", [])
    return ids[0] if ids else ""


def slack_get(token: str, method: str, params: dict) -> dict:
    url = "https://slack.com/api/" + method + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        return json.load(urllib.request.urlopen(req, timeout=25))
    except Exception as e:
        return {"ok": False, "error": str(e)}


def slack_post(token: str, method: str, params: dict) -> dict:
    data = json.dumps(params).encode()
    req = urllib.request.Request(
        "https://slack.com/api/" + method, data=data,
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json; charset=utf-8"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=25))
    except Exception as e:
        return {"ok": False, "error": str(e)}


def owner_dm_channel(token: str, oid: str) -> str:
    """Resolve (and cache) the IM channel id for the owner, so scripts can DM them."""
    if not oid:
        return ""
    cache = STATE / "dm_channel"
    if cache.exists():
        cached = cache.read_text().strip()
        if cached:
            return cached
    r = slack_post(token, "conversations.open", {"users": oid})
    ch = (r.get("channel") or {}).get("id", "") if r.get("ok") else ""
    if ch:
        cache.write_text(ch)
    return ch


def post_dm(token: str, oid: str, text: str, unfurl: bool = False) -> dict:
    ch = owner_dm_channel(token, oid)
    if not ch:
        return {"ok": False, "error": "could not resolve owner DM channel (set bot.owner_slack_id)"}
    return slack_post(token, "chat.postMessage",
                      {"channel": ch, "text": text, "unfurl_links": unfurl})


def in_work_hours(hours) -> bool:
    """hours = [start, end] local 24h ints; empty / None = always on.

    CCSLACK_FORCE=1 bypasses the window (handy for testing).
    """
    if os.environ.get("CCSLACK_FORCE"):
        return True
    if not hours:
        return True
    h = int(time.strftime("%H"))
    return int(hours[0]) <= h < int(hours[1])


def claude_run(prompt: str, claude_bin: str = "claude",
               perm: str = "acceptEdits", cwd=None, timeout: int = 300):
    """Run headless Claude Code on a text prompt. Returns stdout, or None on error."""
    import subprocess
    try:
        r = subprocess.run(
            [os.path.expanduser(claude_bin), "--print", "-p", prompt,
             "--permission-mode", perm],
            capture_output=True, text=True, timeout=timeout,
            cwd=cwd or str(pathlib.Path.home()))
        return (r.stdout or "").strip()
    except Exception as e:
        sys.stderr.write(f"claude_run error: {e}\n")
        return None
