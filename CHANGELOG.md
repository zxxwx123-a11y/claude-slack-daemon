# Changelog

## v0.2 — Proactive layer & background jobs

The daemon learns to reach *out*, not just answer. Everything new is config-gated and
**off by default**, so the v0.1 DM behavior is unchanged unless you opt in.

- **Background jobs** (`tasks.py`) — offload a big, multi-step request to a detached
  background runner instead of blocking on one reply. Enable with `agent.dispatch_big_jobs`.
  The runner posts `started` / `done` / `failed` pings to your DM, and you get
  `tasks` / `stop #N` / `show #N` commands. Headless by default (no window to babysit), so it
  also works over SSH and ports cleanly to Linux later.
  - **`[tasks] mode = "window"` (macOS, optional)** — instead of headless, pop a live
    Terminal/iTerm window running Claude Code interactively on the task: watch it run step by
    step, jump in whenever, and keep the session open to continue at your desk. Uses iTerm if
    installed, else the built-in Terminal.app. Closing the session pings your DM.
- **Proactive inbox digest** (`digest.py`) — on a timer, scans your unread Gmail + Slack
  @-mentions, lets your local Claude Code decide what actually needs you, and pushes a short
  digest to your own DM. Strictly read-only — never sends or marks anything. Enable with
  `[digest]`.
- **Draft assistant** (`draft.py`) — on a short timer, drafts a reply to the latest Slack
  @-mention (reading the full thread) and pushes it to your DM. It never sends — you review
  and hit send yourself. Enable with `[draft]`.
- **`_common.py`** — shared stdlib-only helpers (config + `.env` loading, owner-DM resolution,
  Slack Web API, work-hours gate). No new third-party dependencies.
- **Manifest** now also requests the **user scopes** (`search:read` + history) the digest/draft
  need, so they're minted on install.
- **Config** gained `[tasks]` / `[digest]` / `[draft]` sections plus `bot.owner_slack_id`,
  `bot.slack_handle`, and `bot.owner_context`.

## v0.1

Initial release: single-owner Slack DM → headless Claude Code, Socket Mode (no public
endpoint), persistent per-thread memory via `--resume`, default-deny allowlist, self-healing
launchd + heartbeat watchdog. MIT.
