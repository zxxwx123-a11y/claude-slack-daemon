#!/usr/bin/env python3
"""Background task queue for claude-slack-daemon.

When the daemon offloads a big multi-step job (`agent.dispatch_big_jobs = true`),
it enqueues the task here and spawns `python tasks.py run <id>` as a detached
background process. That runner executes the job with headless Claude Code and
posts start / done / failed updates to the owner's Slack DM — so you can fire off
a long job from your phone and get pinged when it lands, instead of waiting on a
single blocking reply.

Headless by design (no terminal window to babysit) so it also works over SSH and
will port cleanly to Linux/systemd.

CLI:  tasks.py list | show <id> | run <id> | stop <id>
"""
import os
import sys
import json
import time
import signal
import sqlite3
import subprocess
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _common as C  # noqa: E402

DB = C.STATE / "tasks.db"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=10)
    c.execute(
        """CREATE TABLE IF NOT EXISTS tasks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created INTEGER, updated INTEGER, request TEXT,
            status TEXT DEFAULT 'pending', pid INTEGER, result TEXT)""")
    return c


def enqueue(request: str) -> int:
    c = _conn()
    now = int(time.time())
    tid = c.execute(
        "INSERT INTO tasks(created,updated,request) VALUES(?,?,?)",
        (now, now, request)).lastrowid
    c.commit()
    c.close()
    return tid


def update(tid: int, **kw) -> None:
    if not kw:
        return
    kw["updated"] = int(time.time())
    c = _conn()
    c.execute(f"UPDATE tasks SET {','.join(k + '=?' for k in kw)} WHERE id=?",
              (*kw.values(), tid))
    c.commit()
    c.close()


def get(tid: int):
    c = _conn()
    c.row_factory = sqlite3.Row
    r = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    c.close()
    return dict(r) if r else None


def recent(limit: int = 12):
    c = _conn()
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))]
    c.close()
    return rows


def summary() -> str:
    rows = recent()
    if not rows:
        return "Task queue is empty."
    ico = {"pending": "🕓", "running": "🏗️", "done": "✅",
           "failed": "❌", "blocked": "⚠️", "stopped": "🛑"}
    return "*Task queue*\n" + "\n".join(
        f"{ico.get(t['status'], '•')} *#{t['id']}* [{t['status']}] {t['request'][:60]}"
        for t in rows)


def stop(tid: int) -> None:
    t = get(tid)
    if t and t.get("pid"):
        try:
            os.kill(t["pid"], signal.SIGTERM)
        except Exception:
            pass
    update(tid, status="stopped", pid=None)


def spawn(tid: int) -> None:
    """Launch the runner for a queued task.

    Default is a detached headless runner (portable, SSH/Linux-friendly). If
    `[tasks] mode = "window"` and we're on macOS, instead pop a visible
    Terminal/iTerm window running Claude Code interactively — so you can watch it
    live and keep working in that window when you're back at your desk.
    """
    cfg = C.load_config()
    if (cfg.get("tasks", {}) or {}).get("mode", "headless") == "window" and sys.platform == "darwin":
        _spawn_window(tid, cfg)
        return
    py = sys.executable or "python3"
    subprocess.Popen(
        [py, str(pathlib.Path(__file__).resolve()), "run", str(tid)],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _app_installed(name: str) -> bool:
    try:
        r = subprocess.run(["osascript", "-e", f'id of application "{name}"'],
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def _spawn_window(tid: int, cfg: dict) -> None:
    """macOS only: pop a visible Terminal/iTerm window that runs the job interactively.

    Claude Code starts with the task as its first message; you can watch every step,
    jump in at any point, and the session stays open so you continue at your desk.
    Closing the session posts a `done` ping to your DM. iTerm is used if installed,
    otherwise the built-in Terminal.app.
    """
    import shlex
    agent = cfg.get("agent", {})
    claude_bin = os.path.expanduser(agent.get("claude_bin", "claude"))
    cwd = os.path.expanduser(agent.get("cwd", "") or str(C.STATE / "work"))
    pathlib.Path(cwd).mkdir(parents=True, exist_ok=True)
    t = get(tid)
    if not t:
        return
    update(tid, status="running")
    tdir = C.STATE / "tasks"
    tdir.mkdir(exist_ok=True)
    taskfile = tdir / f"{tid}.txt"
    taskfile.write_text(t["request"])
    runner = tdir / f"run_{tid}.sh"
    self_py = sys.executable or "python3"
    self_path = str(pathlib.Path(__file__).resolve())
    runner.write_text(
        "#!/bin/bash\n"
        f"cd {shlex.quote(cwd)}\n"
        f'echo "=== Task #{tid}  (live — watch it, jump in anytime) ==="\n'
        f"cat {shlex.quote(str(taskfile))}\n"
        'echo "------------------------------------------------"\n'
        f'{shlex.quote(claude_bin)} "$(cat {shlex.quote(str(taskfile))})"\n'
        f'{shlex.quote(self_py)} {shlex.quote(self_path)} done {tid}\n')
    runner.chmod(0o755)
    if _app_installed("iTerm"):
        osa = ('tell application "iTerm"\n'
               '  create window with default profile\n'
               f'  tell current session of current window to write text "bash {runner}"\n'
               '  activate\n'
               'end tell')
    else:
        osa = ('tell application "Terminal"\n'
               f'  do script "bash {runner}"\n'
               '  activate\n'
               'end tell')
    subprocess.Popen(["osascript", "-e", osa], start_new_session=True)


def _run(tid: int) -> None:
    cfg = C.load_config()
    bt = C.bot_token()
    oid = C.owner_id(cfg)
    agent = cfg.get("agent", {})
    tasks_cfg = cfg.get("tasks", {})
    claude_bin = agent.get("claude_bin", "claude")
    cwd = os.path.expanduser(agent.get("cwd", "") or str(C.STATE / "work"))
    pathlib.Path(cwd).mkdir(parents=True, exist_ok=True)
    perm = tasks_cfg.get("permission_mode") or agent.get("permission_mode", "acceptEdits")
    timeout = int(tasks_cfg.get("timeout_seconds", 1800))

    t = get(tid)
    if not t:
        return
    update(tid, status="running", pid=os.getpid())
    C.post_dm(bt, oid, f"🏗️ *#{tid} started*\n_{t['request'][:140]}_")
    cmd = [os.path.expanduser(claude_bin), "--print", "-p", t["request"],
           "--permission-mode", perm]
    mcp = agent.get("mcp_config", "")
    if mcp:
        cmd += ["--mcp-config", os.path.expanduser(mcp)]
    model = agent.get("model", "")
    if model:
        cmd += ["--model", model]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        out = (r.stdout or r.stderr or "").strip()
        st = "done" if r.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        out, st = f"(timed out after {timeout}s — try a smaller ask)", "blocked"
    except Exception as e:  # pragma: no cover
        out, st = str(e), "failed"
    update(tid, status=st, result=out[-3000:], pid=None)
    ico = {"done": "✅", "failed": "❌", "blocked": "⚠️"}.get(st, "•")
    C.post_dm(bt, oid, f"{ico} *#{tid} {st}*\n_{t['request'][:80]}_\n\n{out[-1400:]}")


def _mark_done(tid: int) -> None:
    """Window-mode session closed → mark done + ping the owner's DM."""
    update(tid, status="done")
    cfg = C.load_config()
    C.post_dm(C.bot_token(), C.owner_id(cfg), f"✅ *#{tid} done* — window session closed.")


if __name__ == "__main__":
    a = sys.argv[1:] or ["list"]
    if a[0] == "run" and len(a) > 1:
        _run(int(a[1]))
    elif a[0] == "done" and len(a) > 1:
        _mark_done(int(a[1]))
    elif a[0] == "show" and len(a) > 1:
        print(json.dumps(get(int(a[1])), ensure_ascii=False, indent=2))
    elif a[0] == "stop" and len(a) > 1:
        stop(int(a[1]))
        print(f"stopped #{a[1]}")
    else:
        print(summary())
