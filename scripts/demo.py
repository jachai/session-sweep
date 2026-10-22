#!/usr/bin/env python3
"""Run `session-sweep scan --all` on made-up sessions in a temp dir. Nothing touches your real ~/.claude.

    python3 scripts/demo.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills/close-stale-sessions/scripts/session_sweep.py"
H = 3600
HANDOFF = ("Resume prompt: continue the billing export rewrite on branch feat/billing-export. Wave two is done and "
           "pushed at 4f2a91c; next run the reconciliation script against the March ledger and fix the rounding drift.")

# id, name, state, idle hours, created hours ago, intent, last message, extra state fields
SESSIONS = [
    ("a1b2c3d4", "billing export wave 2", "blocked", 30, 40, "build wave 2 of the billing export", HANDOFF, {}),
    ("e5f6a7b8", "billing export wave 3", "working", 0.2, 29, HANDOFF + " Go.", "", {}),
    ("c9d0e1f2", "flaky test triage", "done", 52, 60, "find why test_sync flakes", "result: fixed the race", {}),
    ("a3b4c5d6", "release notes draft", "done", 5, 8, "draft release notes", "result: drafted", {}),
    ("f7a8b9c0", "cache migration plan", "blocked", 24 * 12, 24 * 13, "plan the cache migration",
     "needs input: should I drop the old cache table now?", {}),
    ("d1e2f3a4", "dependency bump", "blocked", 20, 22, "bump the http client",
     "needs input: keep the old timeout default?", {}),
    ("b5c6d7e8", "hotfix login redirect", "done", 30, 34, "fix the login redirect loop", "result: pushed",
     {"worktreePath": "/work/app/.claude/worktrees/hotfix-login"}),
    ("f9e8d7c6", "docs site search", "stopped", 24 * 4, 24 * 5, "add search to the docs site", "", {}),
]


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def main():
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / ".claude"
        for sid, name, state, idle, created, intent, last, extra in SESSIONS:
            sdir = cfg / "jobs" / sid
            sdir.mkdir(parents=True)
            st = dict(state=state, name=name, intent=intent, detail=last.split(": ", 1)[-1], sessionId=sid + "-uuid",
                      cwd="/work/app", createdAt=iso(now - created * H), updatedAt=iso(now - idle * H), **extra)
            (sdir / "state.json").write_text(json.dumps(st))
            (sdir / "timeline.jsonl").write_text(json.dumps({"state": state, "text": last}) + "\n" if last else "")
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg), SESSION_SWEEP_LIVE="0", CLAUDE_JOB_DIR="")
        subprocess.run([sys.executable, str(SCRIPT), "scan", "--all"], env=env, check=True)


if __name__ == "__main__":
    main()
