# session-sweep

Keep the `claude agents` view down to the Claude Code sessions that still matter. session-sweep closes the background sessions you've moved on from: the ones that handed off to a newer session, the ones that finished a day ago, and, after asking you, the ones left waiting on an answer you're never going to give.

[![test](https://github.com/jachai/session-sweep/actions/workflows/test.yml/badge.svg)](https://github.com/jachai/session-sweep/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/jachai/session-sweep/blob/main/LICENSE)

If you work in long chains of sessions (each one writes a handoff, a fresh session picks it up), the old ones pile up as "needs input", "ready for review" or "completed". session-sweep runs every 12 hours, closes what's clearly done, and starts one short interview session when something needs your call.

```
$ session-sweep scan --all
CLOSE      c9d0e1f2 done        2d  flaky test triage                        completed, idle 2d
CLOSE      a1b2c3d4 blocked    30h  billing export wave 2                    handed off: e5f6a7b8 started from its handoff prompt (billing export wave 3, working)
ASK→close  f7a8b9c0 blocked    12d  cache migration plan                     waiting on you 12d: should I drop the old cache table now?
ASK→close  f9e8d7c6 stopped     4d  docs site search                         stopped, idle 4d
ASK→keep   b5c6d7e8 done       30h  hotfix login redirect                    completed with worktree hotfix-login, idle 30h
ASK→keep   d1e2f3a4 blocked    20h  dependency bump                          waiting on you 20h: keep the old timeout default?
KEEP       a3b4c5d6 done        5h  release notes draft                      done, idle 5h
KEEP       e5f6a7b8 working    12m  billing export wave 3                    working

2 to close, 4 to ask about, 2 to keep
```

(That's `python3 scripts/demo.py`, made-up sessions.)

## Install

As a Claude Code plugin:

```
/plugin marketplace add jachai/session-sweep
/plugin install session-sweep@session-sweep
```

Or as a plain skill, without the plugin system:

```sh
git clone https://github.com/jachai/session-sweep
cd session-sweep && ./install.sh          # copies the skill to ~/.claude/skills (--link to symlink instead)
```

You need Claude Code with background sessions (`claude agents`) and Python 3.9+ on macOS or Linux. There are no dependencies.

## Use

Ask Claude to "close stale sessions" or "clean up the agents view", or run the skill directly: `/session-sweep:close-stale-sessions` (plugin) or `/close-stale-sessions` (plain skill). Claude shows what it will close, asks you about the unclear ones, and closes them.

**The first run installs a job that runs every 12 hours** and tells you it did. The job runs launchd on macOS, and a systemd user timer (or cron, if there's no systemd) on Linux. Each run it:

1. closes every CLOSE row;
2. if there are ASK rows it hasn't asked you about yet, starts one background session named **session-sweep interview (N to review)**. It shows up in `claude agents` as needing your input. Attach to it and answer with something like `close all suggested`, `close 1 3`, `keep 2` or `later`. Your answers are remembered.

Don't want the job? Run `session-sweep schedule uninstall`. It stays off; later runs won't reinstall it.

The installed job also puts a `session-sweep` command in `~/.local/bin` (if that folder exists):

```sh
session-sweep scan                  # what would happen; changes nothing (--all adds KEEP rows, --json for scripts)
session-sweep run --apply           # close the CLOSE rows now (without --apply it's a dry run)
session-sweep decide close ID...    # answer ASK rows yourself
session-sweep decide keep ID...
session-sweep restore ID            # undo: bring back a closed session's transcript and print the resume command
session-sweep status                # job state, last run, current counts
session-sweep schedule install|uninstall|status
```

## How it decides

Each session gets one of three verdicts. The rules are checked in this order, and the first match wins.

**KEEP (never touched)**

- It's working: its state is "working", or its live process is running a background shell, monitor or subagent. A background task left over in the state file of a process that has exited doesn't count, and neither do passive artifact watches.
- It was active in the last 2 hours. Activity is the newer of the session's last state update and its transcript's last write.
- It's an interactive terminal session rather than a background one, or it's the session running the sweep.
- You said keep. That holds until the session shows new activity, and then it's re-evaluated.

**CLOSE (no question asked)**

- **It handed off and the newer session started.** A newer session counts as its successor when it was created after this session's last activity and one of these is true:
  - its opening prompt reuses this session's final message: they share at least 4 runs of 8 words that appear in no more than 3 sessions overall. That's how a pasted resume prompt is recognised, while shared boilerplate (templates every handoff uses) isn't;
  - its opening prompt names the same handoff file (`*handoff*.md` or `.agent-handoff/*.md`) this session's final message pointed to;
  - its opening prompt names this session's id.
  Closed sessions still count as successors, so the link survives the newer session being closed first.
- **It completed and has sat idle**: state "done", process exited, no worktree, idle 24 hours.
- It's a finished session-sweep interview, or you said close.

**ASK (you decide)**

Everything else that's idle: waiting on your input, stopped, failed, completed but still holding a worktree, marked working although its process is gone, or a `claude rm` that refused (usually unpushed commits in its worktree). Each ASK row carries a suggestion:

- **→close** if it's been idle 7+ days, it's stopped or failed, or its last message pointed you to a fresh session ("start a new session with the resume prompt…");
- **→keep** otherwise.

## Closing is reversible

Before `claude rm <id>`, session-sweep copies the session's `state.json` and `timeline.jsonl` to `~/.claude/session-sweep/archive/<id>/` and adds a hard link to its transcript (no extra disk space, and the transcript survives even if `rm` deletes the original). `session-sweep restore <id>` puts the transcript back and prints `cd <folder> && claude --resume <session id>`. Archives are kept for 30 days.

`claude rm` deletes a session's worktree too. It refuses when the worktree has unpushed commits, and session-sweep never forces it; that session becomes an ASK row instead.

## Settings

`~/.claude/session-sweep/config.json` (all optional):

```json
{
  "min_idle_hours": 2,
  "done_idle_hours": 24,
  "abandoned_days": 7,
  "archive_days": 30,
  "interval_minutes": 720,
  "interview": true,
  "interview_model": "sonnet"
}
```

`interval_minutes` is how often the job runs (720 = every 12 hours; 60 for hourly). Change it and then run `session-sweep schedule install` to apply it. `CLAUDE_CONFIG_DIR` is respected everywhere. The job's log is `~/.claude/session-sweep/job.log`, and every close, refusal and answer is recorded in `log.jsonl`.

## Notes

- Session states and pids come from `claude agents --json --all`, the same source as the built-in view. Names, prompts and last messages come from Claude Code's local files (`~/.claude/jobs`), which aren't a public API. A Claude Code update can change them; please [open an issue](https://github.com/jachai/session-sweep/issues/new/choose) if something stops matching.
- Claude Code's auto mode may refuse to let a session run `claude rm` for you. If it does, the skill tells you to run the command yourself with `!`; the scheduled job runs outside Claude Code and isn't affected.
- Nothing leaves your machine. session-sweep writes only to `~/.claude/session-sweep/`, the job definition (`~/Library/LaunchAgents/io.github.jachai.session-sweep.plist`, `~/.config/systemd/user/session-sweep.*` or one tagged crontab line) and the `~/.local/bin/session-sweep` link.
- Works well with [claude-top](https://github.com/jachai/claude-top), a live view of your sessions and plan usage.
- This is an independent project, not affiliated with Anthropic.

See [CONTRIBUTING.md](CONTRIBUTING.md) for tests and [CHANGELOG.md](CHANGELOG.md) for changes. MIT licensed.
