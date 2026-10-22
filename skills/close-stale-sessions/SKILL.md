---
name: close-stale-sessions
description: Clear clutter from the `claude agents` view by closing background sessions that are no longer relevant — ones that handed off to a newer session that has started, completed ones that have sat idle, and (after asking the user) ones left waiting on input, stopped or abandoned. Use when asked to "close stale sessions", "clean up the agents view", "clear old sessions", "sweep sessions", or when a session-sweep interview session asks you to run it in interview mode.
---

# Close stale sessions

All the work is done by `scripts/session_sweep.py` in this skill's base directory (shown when the
skill loads). Below, `sweep` means `python3 <base directory>/scripts/session_sweep.py`. It uses only
the Python standard library and never sends anything off the machine.

## Step 0: the hourly job (every time)

Run `sweep schedule ensure`. On the first run it installs a job (launchd on macOS, a systemd user
timer or cron on Linux) that runs `run --apply --spawn-interview` every hour, and prints a line
saying so. Tell the user that line in your reply, including how to remove it
(`sweep schedule uninstall`). If they uninstalled it before, `ensure` leaves it off.

## Step 1: on demand (the user asked you to clean up)

1. `sweep scan`. Report the counts and the CLOSE rows as one short list (id, name, why). Don't
   paste the whole table.
2. If there are ASK rows, interview the user (format below) and record the answers with
   `sweep decide close ID...` and `sweep decide keep ID...`.
3. `sweep run --apply`, then report what closed and anything `claude rm` refused.

If a permission check denies `run --apply`, don't retry it or look for another way around. Tell the
user to type `! <the same command>`, or that the hourly job will apply the recorded answers.

## Interview mode (started by the hourly job)

The job has already closed the CLOSE rows. Run `sweep scan --json` and take the rows whose
`verdict` is `ask`. Show them as one numbered list, suggested-close first:

```
#  id        state    idle  name: why (suggestion)
1  7830fc17  waiting  119d  log search fix: waiting on you, "awaiting decision on …" (close)
```

Quote a few words of `last_message` only where the why is unclear. End the turn with:

`needs input:` Reply `close all suggested`, `close 1 3 7`, `keep 2 5`, `close all`, or `later`.

When the user replies, map the numbers to ids, run `decide close …` and `decide keep …` (leave
unmentioned rows undecided), then `sweep run --apply` (same permission rule as above), and finish
with one line saying how many closed and how many were kept. Never close a row the user didn't
answer for unless they said `close all`.

## How it decides

- **CLOSE** (no question asked):
  - a newer session picked up this one's work: its opening prompt reuses this session's last
    message, resumes the same handoff doc, or names this session's id, and it was started after
    this session's last activity;
  - the session completed, its process exited, it has no worktree, and it has been idle 24h;
  - a finished session-sweep interview, or the user said close.
- **ASK**: waiting on input, stopped, failed, completed with a worktree, marked working with no
  live process, or `claude rm` refused (for example unpushed worktree commits). Each has a
  suggestion: close if idle 7+ days, stopped/failed, or its last message pointed to a fresh
  session; otherwise keep.
- **KEEP**: working or running background tasks, active in the last 2h, interactive terminal
  sessions, the session running this skill, or the user said keep (until it shows new activity).

Closing archives `state.json`, `timeline.jsonl` and a hard link of the transcript to
`~/.claude/session-sweep/archive/<id>/` (kept 30 days), then runs `claude rm <id>`.
`sweep restore <id>` brings the transcript back and prints the resume command. Thresholds live in
`~/.claude/session-sweep/config.json` (see the README).
