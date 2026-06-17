#!/usr/bin/env python3
"""claude-slack-daemon — reach your local Claude Code from Slack.

A single-owner Slack daemon: each DM spawns headless Claude Code (`claude -p --resume`)
as the brain, with persistent per-conversation memory. Socket Mode — no public endpoint,
no webhook, no tunnel. Everything deployment-specific lives in config.toml + .env
(see the *.example files). MIT licensed.
"""
import os, re, json, uuid, html, subprocess, time, threading, pathlib

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

ROOT = pathlib.Path(__file__).resolve().parent
STATE = pathlib.Path(os.environ.get("CCSLACK_STATE", str(pathlib.Path.home() / ".claude-slack-daemon")))
STATE.mkdir(parents=True, exist_ok=True)
HEARTBEAT = STATE / "heartbeat"
LOG = STATE / "agent.log"
SESSIONS_FILE = STATE / "sessions.json"


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG, "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass
    print(f"[{ts}] {msg}", flush=True)


def load_config() -> dict:
    path = pathlib.Path(os.environ.get("CCSLACK_CONFIG", str(ROOT / "config.toml")))
    if not path.exists():
        raise SystemExit(f"config not found: {path}  (copy config.example.toml -> config.toml)")
    return tomllib.loads(path.read_text())


CFG = load_config()
BOT_NAME = CFG.get("bot", {}).get("name", "Assistant")
OWNER = CFG.get("bot", {}).get("owner", "the owner")
REPLY_LANG = CFG.get("bot", {}).get("reply_language", "the same language the user writes in")
SYSTEM_EXTRA = CFG.get("bot", {}).get("system_extra", "")
ALLOWED = set(CFG.get("access", {}).get("allowed_user_ids", []))
CLAUDE_BIN = os.path.expanduser(CFG.get("agent", {}).get("claude_bin", "claude"))
BOT_CWD = os.path.expanduser(CFG.get("agent", {}).get("cwd", "") or str(STATE / "work"))
MODEL = CFG.get("agent", {}).get("model", "sonnet")
PERM = CFG.get("agent", {}).get("permission_mode", "acceptEdits")
MCP_CONFIG = CFG.get("agent", {}).get("mcp_config", "")
TIMEOUT = int(CFG.get("agent", {}).get("timeout_seconds", 900))

BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
if not (BOT_TOKEN and APP_TOKEN):
    raise SystemExit("Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN in the environment (.env)")

pathlib.Path(BOT_CWD).mkdir(parents=True, exist_ok=True)

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

app = App(token=BOT_TOKEN)
BOT_USER_ID = app.client.auth_test()["user_id"]


def heartbeat_loop() -> None:
    while True:
        try:
            HEARTBEAT.write_text(str(int(time.time())))
        except Exception:
            pass
        time.sleep(30)


threading.Thread(target=heartbeat_loop, daemon=True).start()


def to_mrkdwn(t: str) -> str:
    """Standard Markdown -> Slack mrkdwn (bold/heading/link/list)."""
    t = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", t, flags=re.M)
    t = re.sub(r"\*\*([^*\n]+)\*\*", r"*\1*", t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", t)
    t = re.sub(r"^(\s*)[-*]\s+", r"\1• ", t, flags=re.M)
    return t


def _sessions() -> dict:
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text())
        except Exception:
            return {}
    return {}


def build_prompt(text: str) -> str:
    base = (
        f"[New Slack DM]\n{text}\n\n---\n"
        f"You are {BOT_NAME}, {OWNER}'s private personal agent, reachable only by them via Slack DM, "
        f"with persistent memory of this conversation. Use your full local capabilities — read files, "
        f"run shell commands, use any configured MCP tools — to actually help, not just chat. "
        f"Reply in {REPLY_LANG}, conclusion first, concise. Your reply goes straight to their Slack."
    )
    return base + (f"\n{SYSTEM_EXTRA}" if SYSTEM_EXTRA else "")


def run_claude(prompt: str, convo_key: str) -> str:
    """Headless claude with a persistent per-conversation session (= memory)."""
    store = _sessions()
    sid = store.get(convo_key)
    base = [CLAUDE_BIN, "--print", "-p", prompt,
            "--permission-mode", PERM, "--model", MODEL, "--output-format", "json"]
    if MCP_CONFIG:
        base += ["--mcp-config", os.path.expanduser(MCP_CONFIG)]
    if sid:
        cmd = base + ["--resume", sid]
    else:
        sid = str(uuid.uuid4())
        cmd = base + ["--session-id", sid]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, cwd=BOT_CWD)
        if r.returncode != 0 and "--resume" in cmd:
            sid = str(uuid.uuid4())                       # session lost -> fresh window
            r = subprocess.run(base + ["--session-id", sid], capture_output=True,
                               text=True, timeout=TIMEOUT, cwd=BOT_CWD)
        if r.returncode != 0:
            return f"(claude error) {(r.stderr or '')[-300:]}"
        out = (json.loads(r.stdout).get("result") or "").strip()
        store[convo_key] = sid
        SESSIONS_FILE.write_text(json.dumps(store))
        return out or "(empty reply)"
    except subprocess.TimeoutExpired:
        return f"Timed out (>{TIMEOUT}s). Try a smaller ask."
    except Exception as e:
        return f"Local error: {e}"


_seen: set = set()


def handle(event, say, client, via_mention: bool = False) -> None:
    if event.get("bot_id") or event.get("user") == BOT_USER_ID or event.get("subtype"):
        return
    user = event.get("user")
    if user not in ALLOWED:                            # default-deny: empty allowlist = answer no one
        log(f"IGNORE user={user} (not allowlisted — add this id to config.toml to allow)")
        return
    if event.get("channel_type") != "im" and not via_mention:
        return                                        # in channels: only reply when @-mentioned
    ts = event.get("ts")
    if not ts or ts in _seen:
        return
    _seen.add(ts)
    text = html.unescape(re.sub(rf"<@{BOT_USER_ID}>", "", event.get("text") or "").strip())
    if not text:
        return
    ch = event.get("channel")
    log(f"IN  {ch} {text[:80]!r}")
    try:
        client.reactions_add(channel=ch, timestamp=ts, name="eyes")
    except Exception:
        pass
    is_dm = event.get("channel_type") == "im"
    post_kw = {} if is_dm else {"thread_ts": ts}
    try:
        ph_ts = say(text="…", **post_kw).get("ts")    # instant ack, edited into the answer
    except Exception:
        ph_ts = None
    convo_key = event.get("thread_ts") or ch          # DM = one lasting session; thread = its own
    reply = to_mrkdwn(run_claude(build_prompt(text), convo_key))
    try:
        if ph_ts:
            client.chat_update(channel=ch, ts=ph_ts, text=reply)
        else:
            say(text=reply, **post_kw)
    except Exception as e:
        log(f"send failed: {e}")
        say(text=reply[:3000], **post_kw)
    log(f"OUT {len(reply)} chars")


@app.event("message")
def on_message(event, say, client):
    try:
        handle(event, say, client, via_mention=False)
    except Exception as e:
        log(f"handler error: {e}")


@app.event("app_mention")
def on_mention(event, say, client):
    try:
        handle(event, say, client, via_mention=True)
    except Exception as e:
        log(f"handler error: {e}")


if __name__ == "__main__":
    log(f"{BOT_NAME} online (bot {BOT_USER_ID})")
    while True:
        try:
            SocketModeHandler(app, APP_TOKEN).start()
        except Exception as e:
            log(f"socket loop crashed, reconnect in 5s: {e}")
            time.sleep(5)
