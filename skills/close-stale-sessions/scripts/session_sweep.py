#!/usr/bin/env python3
"""session-sweep: close the Claude Code background sessions you no longer need.

Reads ~/.claude/jobs (or $CLAUDE_CONFIG_DIR/jobs) and `claude agents --json --all`, decides which
sessions are stale, and closes them with `claude rm` after archiving their files.

  session-sweep scan [--json] [--all]       show what would happen, change nothing
  session-sweep run [--apply] [--spawn-interview]
                                           close the CLOSE rows (dry run without --apply)
  session-sweep decide close|keep ID...     record your answer for ASK rows
  session-sweep restore ID                  bring back a closed session's transcript
  session-sweep schedule ensure|install|uninstall|status
                                           the scheduled job (launchd, systemd or cron)
  session-sweep status                      schedule, last run and counts

See https://github.com/jachai/session-sweep for how it decides.
"""
import argparse
import datetime as dt
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

__version__ = "0.1.2"

LABEL = "io.github.jachai.session-sweep"
INTERVIEW_NAME = "session-sweep interview"
INTERVIEW_PROMPT = ("Use the close-stale-sessions skill in interview mode: ask me about the sessions "
                    "session-sweep could not decide on, then record my answers and close them.")
DEFAULTS = {
    "min_idle_hours": 2,       # never touch a session active more recently than this
    "done_idle_hours": 24,     # completed + exited + no worktree + idle this long -> close
    "abandoned_days": 7,       # ASK rows idle this long are suggested for closing
    "archive_days": 30,        # how long closed sessions stay restorable
    "interval_minutes": 720,   # how often the scheduled job runs (12 hours)
    "interview": True,         # let the scheduled job start an interview session
    "interview_model": "sonnet",
}
SHINGLE = 8               # words per shingle when matching a handoff prompt
RARE_DF = 3               # a shingle found in more sessions than this is boilerplate
MIN_RARE_OVERLAP = 4      # rare shingles a newer session's prompt must share
HANDOFF_OFFER = re.compile(r"fresh session|new session|resume prompt|next-agent prompt|hand(ed)? ?off", re.I)
HANDOFF_PATH = re.compile(r"[\w./~-]*\.agent-handoff/[\w.-]+\.md|[\w./~-]*handoff[\w./-]*\.md", re.I)


# ---------------------------------------------------------------- paths and config

def home():
    return Path(os.path.expanduser("~"))


def config_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or home() / ".claude")


def jobs_dir():
    return config_dir() / "jobs"


def data_dir():
    return config_dir() / "session-sweep"


def archive_dir():
    return data_dir() / "archive"


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True))
    os.replace(tmp, path)


def config():
    cfg = dict(DEFAULTS)
    cfg.update(load_json(data_dir() / "config.json", {}))
    return cfg


def log(event, **kw):
    path = data_dir() / "log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    kw.update(event=event, at=dt.datetime.now().isoformat(timespec="seconds"))
    with open(path, "a") as f:
        f.write(json.dumps(kw) + "\n")


def trim_log(keep=2000):
    path = data_dir() / "log.jsonl"
    if path.exists():
        lines = path.read_text().splitlines(True)
        if len(lines) > keep:
            path.write_text("".join(lines[-keep:]))


def claude_bin():
    for p in (os.environ.get("SESSION_SWEEP_CLAUDE"), shutil.which("claude"), str(home() / ".local/bin/claude")):
        if p and os.path.exists(p):
            return p
    return None


def claude(*args, timeout=120):
    exe = claude_bin()
    if not exe:
        raise FileNotFoundError("claude CLI not found (set SESSION_SWEEP_CLAUDE)")
    return subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, cwd=str(home()))


def ts(s):
    if not s:
        return 0.0
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def every(minutes):
    minutes = int(minutes)
    if minutes % 60:
        return "every %d minutes" % minutes
    return "every hour" if minutes == 60 else "every %d hours" % (minutes // 60)


def ago(h):
    if h < 1:
        return "%dm" % (h * 60)
    if h < 48:
        return "%.0fh" % h
    return "%.0fd" % (h / 24)


# ---------------------------------------------------------------- reading sessions

def live_view():
    """`claude agents --json --all` keyed by short id. It is the only source of pid, status and kind."""
    if os.environ.get("SESSION_SWEEP_LIVE") == "0":
        return {}
    try:
        return {a["id"]: a for a in json.loads(claude("agents", "--json", "--all", timeout=60).stdout)}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return {}


def timeline_texts(sdir):
    texts = []
    try:
        with open(sdir / "timeline.jsonl") as f:
            for line in f:
                try:
                    t = json.loads(line).get("text")
                except ValueError:
                    continue
                if t:
                    texts.append(t)
    except OSError:
        pass
    return texts


def load_sessions(view=None):
    view = live_view() if view is None else view
    now = time.time()
    sessions = []
    if not jobs_dir().is_dir():
        return sessions
    for sdir in sorted(jobs_dir().iterdir()):
        st = load_json(sdir / "state.json", None)
        if not isinstance(st, dict):
            continue
        texts = timeline_texts(sdir)
        tp = st.get("linkScanPath") or ""
        t_mtime = os.path.getmtime(tp) if tp and os.path.exists(tp) else 0
        last_act = max(ts(st.get("updatedAt")), t_mtime) or ts(st.get("createdAt"))
        v = view.get(sdir.name, {})
        sessions.append(dict(
            id=sdir.name, st=st, name=st.get("name") or "", state=v.get("state") or st.get("state"),
            kind=v.get("kind", "background"), pid=v.get("pid"), in_view=bool(v) or not view,
            created=ts(st.get("createdAt")), last_act=last_act, idle_h=(now - last_act) / 3600.0,
            intent=st.get("intent") or "", final="\n".join(texts[-2:]) + "\n" + (st.get("detail") or ""),
            transcript=tp, worktree=st.get("worktreePath"),
        ))
    return sessions


def load_archived():
    """Closed sessions still count as successors, so a handoff link survives the newer session closing first."""
    out = []
    if not archive_dir().is_dir():
        return out
    for adir in archive_dir().iterdir():
        st = load_json(adir / "state.json", None)
        if isinstance(st, dict):
            out.append(dict(id=adir.name, st=st, name=st.get("name") or "", state="closed",
                            created=ts(st.get("createdAt")), intent=st.get("intent") or "", final=""))
    return out


# ---------------------------------------------------------------- deciding

def shingles(text):
    w = re.findall(r"\w+", text.lower())
    return {" ".join(w[i:i + SHINGLE]) for i in range(len(w) - SHINGLE + 1)}


def doc_names(text):
    return {p.split("/")[-1].lower() for p in HANDOFF_PATH.findall(text)}


def find_successors(sessions):
    """Map id -> (newer session, how) for every session whose work a newer session picked up."""
    live = {s["id"] for s in sessions}
    pool = sessions + [a for a in load_archived() if a["id"] not in live]
    sh_final = {s["id"]: shingles(s["final"]) for s in sessions}
    sh_intent = {s["id"]: shingles(s["intent"]) for s in pool}
    df = {}
    for s in pool:
        for g in sh_final.get(s["id"], set()) | sh_intent[s["id"]]:
            df[g] = df.get(g, 0) + 1
    rare = {k: {g for g in v if df[g] <= RARE_DF} for k, v in sh_final.items()}
    by_created = sorted(pool, key=lambda s: s["created"])
    out = {}
    for a in sessions:
        docs = doc_names(a["final"])
        for b in by_created:
            if b["id"] == a["id"] or b["created"] <= a["created"] or b["created"] < a["last_act"] - 600:
                continue
            how = None
            if a["id"] in b["intent"] or a["st"].get("sessionId", "-") in b["intent"]:
                how = "names it"
            elif len(rare[a["id"]] & sh_intent[b["id"]]) >= MIN_RARE_OVERLAP:
                how = "started from its handoff prompt"
            elif docs and docs & doc_names(b["intent"]):
                how = "resumed its handoff doc"
            if how:
                out[a["id"]] = (b, how)
                break
    return out


def busy_tasks(s):
    """Live background work. Artifact watches are passive, and an exited process runs nothing
    whatever its leftover inFlight says."""
    f = s["st"].get("inFlight") or {}
    return bool(s["pid"] and f.get("tasks") and set(f.get("kinds") or []) - {"artifact_watch"})


def classify(sessions, decisions, cfg=None):
    cfg = cfg or config()
    own = os.path.basename(os.environ.get("CLAUDE_JOB_DIR", "").rstrip("/")) or None
    succ = find_successors(sessions)
    rows = []
    for s in sessions:
        sid, state, idle = s["id"], s["state"], s["idle_h"]
        d = decisions.get(sid) or {}
        unchanged = d.get("last_act", 0) >= s["last_act"] - 1
        verdict, reason = "keep", ""
        if sid == own:
            reason = "this session"
        elif not s["in_view"]:
            reason = "not in the agents view"
        elif s["kind"] != "background":
            reason = "interactive terminal session"
        elif state == "working" and not s["pid"] and idle >= cfg["done_idle_hours"]:
            if d.get("decision") == "close":
                verdict, reason = "close", "you said close"
            elif d.get("decision") == "keep" and unchanged:
                reason = "you said keep"
            else:
                verdict, reason = "ask", "marked working but its process is gone, idle %s" % ago(idle)
        elif state == "working" or busy_tasks(s):
            reason = "working"
        elif s["name"].startswith(INTERVIEW_NAME):
            if state in ("done", "stopped", "failed") and idle >= 1:
                verdict, reason = "close", "finished session-sweep interview"
            else:
                reason = "session-sweep interview"
        elif idle < cfg["min_idle_hours"]:
            reason = "active %s ago" % ago(idle)
        elif d.get("decision") == "close":
            verdict, reason = "close", "you said close"
        elif d.get("decision") == "keep" and unchanged:
            reason = "you said keep"
        elif d.get("decision") == "refused" and unchanged:
            verdict, reason = "ask", "claude rm refused: " + d.get("msg", "")[:160]
        elif sid in succ:
            b, how = succ[sid]
            verdict = "close"
            reason = "handed off: %s %s (%s, %s)" % (b["id"], how, b["name"][:40], b["state"])
        elif state == "done" and not s["worktree"] and idle >= cfg["done_idle_hours"]:
            verdict, reason = "close", "completed, idle %s" % ago(idle)
        elif state == "done" and s["worktree"]:
            verdict, reason = "ask", "completed with worktree %s, idle %s" % (os.path.basename(s["worktree"]), ago(idle))
        elif state == "blocked":
            verdict, reason = "ask", "waiting on you %s: %s" % (ago(idle), (s["st"].get("detail") or "")[:140])
        elif state in ("stopped", "failed"):
            verdict, reason = "ask", "%s, idle %s" % (state, ago(idle))
        else:
            reason = "%s, idle %s" % (state, ago(idle))
        suggest = None
        if verdict == "ask":
            stale = idle >= cfg["abandoned_days"] * 24 or state in ("stopped", "failed")
            suggest = "close" if stale or HANDOFF_OFFER.search(s["final"]) else "keep"
        rows.append(dict(id=sid, name=s["name"], state=state, idle_h=round(idle, 1), verdict=verdict,
                         reason=reason, suggest=suggest, cwd=s["st"].get("cwd"), worktree=s["worktree"],
                         last_message=s["final"].strip()[-400:]))
    order = {"close": 0, "ask": 1, "keep": 2}
    rows.sort(key=lambda r: (order[r["verdict"]], r["suggest"] != "close", -r["idle_h"]))
    return rows


# ---------------------------------------------------------------- closing

TMP_MAX_BYTES = 50 * 1024 * 1024


def archive_tmp(sid, dest):
    """Copy the job's tmp/ into dest/tmp/ (closing deletes it). Skips symlinks and files over 50 MB;
    returns the skipped paths, relative to tmp/."""
    src = jobs_dir() / sid / "tmp"
    skipped = []
    if not src.is_dir():
        return skipped

    def ignore(d, names):
        out = []
        for n in names:
            p = Path(d) / n
            if p.is_symlink():
                out.append(n)
                skipped.append(str(p.relative_to(src)) + " (symlink)")
            elif p.is_file() and p.stat().st_size > TMP_MAX_BYTES:
                out.append(n)
                skipped.append(str(p.relative_to(src)) + " (over 50 MB)")
        return out

    shutil.copytree(src, dest / "tmp", ignore=ignore, symlinks=True, dirs_exist_ok=True)
    return skipped


def archive_and_rm(sess, reason, decisions):
    sid, st = sess["id"], sess["st"]
    dest = archive_dir() / sid
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("state.json", "timeline.jsonl"):
        src = jobs_dir() / sid / name
        if src.exists():
            shutil.copy2(src, dest / name)
    tmp_skipped = archive_tmp(sid, dest)
    tp, kept = sess["transcript"], dest / "transcript.jsonl"
    if tp and os.path.exists(tp) and not kept.exists():
        try:
            os.link(tp, kept)   # costs no space and survives if `claude rm` deletes the original
        except OSError:
            shutil.copy2(tp, kept)
    save_json(dest / "manifest.json", dict(
        id=sid, name=sess["name"], sessionId=st.get("sessionId"), cwd=st.get("cwd"), transcript=tp,
        worktree=sess["worktree"], reason=reason, tmp_skipped=tmp_skipped, closed_at=dt.datetime.now().isoformat(timespec="seconds")))
    os.utime(dest)
    try:
        r = claude("rm", sid)
        msg, ok = (r.stdout + r.stderr).strip(), r.returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        msg, ok = str(e), False
    if not ok or (jobs_dir() / sid).is_dir():
        decisions[sid] = dict(decision="refused", msg=msg, last_act=sess["last_act"])
        log("refused", id=sid, name=sess["name"], msg=msg)
        return False
    decisions.pop(sid, None)
    log("closed", id=sid, name=sess["name"], reason=reason)
    return True


def prune_archive(days):
    cutoff = time.time() - days * 86400
    if archive_dir().is_dir():
        for adir in archive_dir().iterdir():
            if adir.stat().st_mtime < cutoff:
                shutil.rmtree(adir, ignore_errors=True)


class RunLock:
    """One run at a time: the scheduled job and an on-demand run must not both `claude rm`."""

    def __enter__(self):
        import fcntl
        data_dir().mkdir(parents=True, exist_ok=True)
        self.f = open(data_dir() / "run.lock", "w")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            sys.exit("another session-sweep run is in progress")
        return self

    def __exit__(self, *exc):
        self.f.close()


def spawn_interview(rows, cfg):
    """Start one background session that interviews you about ASK rows it hasn't asked about yet."""
    asked = load_json(data_dir() / "asked.json", {})
    ask = [r for r in rows if r["verdict"] == "ask"]
    if not [r for r in ask if r["id"] not in asked]:
        return None
    if any(r["name"].startswith(INTERVIEW_NAME) and r["verdict"] != "close" for r in rows):
        return None
    name = "%s (%d to review)" % (INTERVIEW_NAME, len(ask))
    try:
        r = claude("--bg", "-n", name, "--model", cfg["interview_model"], INTERVIEW_PROMPT)
        out, rc = (r.stdout + r.stderr).strip()[-300:], r.returncode
    except (OSError, subprocess.SubprocessError) as e:
        out, rc = str(e), -1
    asked.update({x["id"]: time.time() for x in ask})
    save_json(data_dir() / "asked.json", asked)
    log("interview", ids=[x["id"] for x in ask], rc=rc, out=out)
    return name if rc == 0 else None


# ---------------------------------------------------------------- the scheduled job

def stable_script():
    return data_dir() / "session_sweep.py"


def sync_copy():
    """The job runs a copy in the data dir, so plugin updates and moved checkouts don't break it."""
    src, dst = Path(__file__).resolve(), stable_script()
    if src != dst.resolve() and (not dst.exists() or dst.read_bytes() != src.read_bytes()):
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        dst.chmod(0o755)
    return dst


def python_for_job():
    for p in ("/usr/bin/python3", shutil.which("python3"), sys.executable):
        if p and os.path.exists(p):
            return p
    return sys.executable


def job_path_env():
    dirs = [os.path.dirname(claude_bin() or ""), str(home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin",
            "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(dict.fromkeys(d for d in dirs if d))


def job_command():
    return [python_for_job(), str(stable_script()), "run", "--apply", "--spawn-interview"]


def scheduler():
    forced = os.environ.get("SESSION_SWEEP_SCHEDULER")
    if forced:
        return forced
    if sys.platform == "darwin":
        return "launchd"
    if shutil.which("systemctl") and subprocess.run(["systemctl", "--user", "show-environment"],
                                                    capture_output=True).returncode == 0:
        return "systemd"
    return "cron" if shutil.which("crontab") else "none"


def launchd_plist(cfg):
    env = {"PATH": job_path_env()}
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        env["CLAUDE_CONFIG_DIR"] = os.environ["CLAUDE_CONFIG_DIR"]
    return {"Label": LABEL, "ProgramArguments": job_command(), "StartInterval": int(cfg["interval_minutes"]) * 60,
            "RunAtLoad": False, "EnvironmentVariables": env,
            "StandardOutPath": str(data_dir() / "job.log"), "StandardErrorPath": str(data_dir() / "job.log")}


def launchd_path():
    return home() / "Library/LaunchAgents" / (LABEL + ".plist")


def systemd_units(cfg):
    env = 'Environment="PATH=%s"' % job_path_env()
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        env += '\nEnvironment="CLAUDE_CONFIG_DIR=%s"' % os.environ["CLAUDE_CONFIG_DIR"]
    service = ("[Unit]\nDescription=session-sweep: close stale Claude Code sessions\n\n[Service]\nType=oneshot\n"
               "%s\nExecStart=%s\n" % (env, " ".join('"%s"' % a for a in job_command())))
    timer = ("[Unit]\nDescription=Run session-sweep %s\n\n[Timer]\nOnBootSec=15min\n"
             "OnUnitActiveSec=%dmin\n\n[Install]\nWantedBy=timers.target\n") % (every(cfg["interval_minutes"]), int(cfg["interval_minutes"]))
    return service, timer


def systemd_dir():
    return home() / ".config/systemd/user"


def cron_line(cfg):
    n = int(cfg["interval_minutes"])
    when = "*/%d * * * *" % n if n < 60 else "17 */%d * * *" % max(1, n // 60)
    env = "PATH=%s" % job_path_env()
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        env += " CLAUDE_CONFIG_DIR=%s" % os.environ["CLAUDE_CONFIG_DIR"]
    cmd = " ".join('"%s"' % a for a in job_command())
    return '%s %s %s >> "%s" 2>&1 # session-sweep' % (when, env, cmd, data_dir() / "job.log")


def crontab_without_us():
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return [l for l in (r.stdout.splitlines() if r.returncode == 0 else []) if not l.endswith("# session-sweep")]


def schedule_install():
    cfg, backend = config(), scheduler()
    sync_copy()
    if backend == "launchd":
        domain = "gui/%d" % os.getuid()
        subprocess.run(["launchctl", "bootout", domain + "/" + LABEL], capture_output=True)
        launchd_path().parent.mkdir(parents=True, exist_ok=True)
        with open(launchd_path(), "wb") as f:
            plistlib.dump(launchd_plist(cfg), f)
        subprocess.run(["launchctl", "bootstrap", domain, str(launchd_path())], check=True, capture_output=True)
    elif backend == "systemd":
        service, timer = systemd_units(cfg)
        systemd_dir().mkdir(parents=True, exist_ok=True)
        (systemd_dir() / "session-sweep.service").write_text(service)
        (systemd_dir() / "session-sweep.timer").write_text(timer)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "session-sweep.timer"], check=True, capture_output=True)
    elif backend == "cron":
        lines = crontab_without_us() + [cron_line(cfg)]
        subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)
    link_cli()
    save_json(data_dir() / "schedule.json", dict(state="installed", backend=backend, at=time.time(),
                                                 interval_minutes=cfg["interval_minutes"]))
    log("schedule_installed", backend=backend)
    return backend


def schedule_uninstall():
    backend = (load_json(data_dir() / "schedule.json", {}) or {}).get("backend") or scheduler()
    if backend == "launchd":
        subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), LABEL)], capture_output=True)
        if launchd_path().exists():
            launchd_path().unlink()
    elif backend == "systemd":
        subprocess.run(["systemctl", "--user", "disable", "--now", "session-sweep.timer"], capture_output=True)
        for unit in ("session-sweep.service", "session-sweep.timer"):
            if (systemd_dir() / unit).exists():
                (systemd_dir() / unit).unlink()
    elif backend == "cron":
        subprocess.run(["crontab", "-"], input="\n".join(crontab_without_us()) + "\n", text=True)
    save_json(data_dir() / "schedule.json", dict(state="declined", backend=backend, at=time.time()))
    log("schedule_uninstalled", backend=backend)
    return backend


def link_cli():
    """Put `session-sweep` on PATH via ~/.local/bin when that dir exists and the name is free or ours."""
    bindir = home() / ".local/bin"
    link = bindir / "session-sweep"
    if bindir.is_dir() and (not link.exists() or link.is_symlink()):
        if link.is_symlink():
            link.unlink()
        link.symlink_to(stable_script())


def schedule_state():
    return (load_json(data_dir() / "schedule.json", {}) or {}).get("state", "never")


def schedule_ensure():
    """First run installs the job; later runs refresh its copy. An uninstall is remembered and respected."""
    state = schedule_state()
    if state == "declined":
        return "declined"
    if state == "installed":
        sync_copy()
        return "installed"
    backend = schedule_install()
    return "newly-installed:" + backend


# ---------------------------------------------------------------- commands

def print_rows(rows, show_keep):
    for r in rows:
        if r["verdict"] == "keep" and not show_keep:
            continue
        tag = r["verdict"].upper() + ("→" + r["suggest"] if r["suggest"] else "")
        print("%-10s %-8s %-8s %5s  %-40s %s" % (tag, r["id"], r["state"], ago(r["idle_h"]), r["name"][:40], r["reason"]))
    n = {v: sum(1 for r in rows if r["verdict"] == v) for v in ("close", "ask", "keep")}
    print("\n%(close)d to close, %(ask)d to ask about, %(keep)d to keep" % n)


def cmd_scan(a):
    rows = classify(load_sessions(), load_json(data_dir() / "decisions.json", {}))
    if a.json:
        print(json.dumps([r for r in rows if a.all or r["verdict"] != "keep"], indent=1))
    else:
        print_rows(rows, a.all)


def cmd_run(a):
    cfg = config()
    with RunLock():
        decisions = load_json(data_dir() / "decisions.json", {})
        sessions = {s["id"]: s for s in load_sessions()}
        rows = classify(list(sessions.values()), decisions, cfg)
        closing = [r for r in rows if r["verdict"] == "close"]
        if not a.apply:
            print_rows(rows, False)
            print("dry run: add --apply to close %d" % len(closing))
            return
        closed = sum(archive_and_rm(sessions[r["id"]], r["reason"], decisions) for r in closing)
        save_json(data_dir() / "decisions.json", decisions)
        prune_archive(cfg["archive_days"])
        trim_log()
        log("run", closed=closed, attempted=len(closing))
        print("closed %d of %d" % (closed, len(closing)))
        if a.spawn_interview and cfg["interview"]:
            name = spawn_interview(classify(load_sessions(), decisions, cfg), cfg)
            if name:
                print("started %r" % name)


def cmd_decide(a):
    decisions = load_json(data_dir() / "decisions.json", {})
    sessions = {s["id"]: s for s in load_sessions()}
    for sid in a.ids:
        if sid not in sessions:
            print("unknown session %s" % sid)
            continue
        decisions[sid] = dict(decision=a.decision, at=time.time(), last_act=sessions[sid]["last_act"])
        log("decided", id=sid, decision=a.decision)
        print("%s -> %s" % (sid, a.decision))
    save_json(data_dir() / "decisions.json", decisions)


def cmd_restore(a):
    adir = archive_dir() / a.id
    m = load_json(adir / "manifest.json", None)
    if not m:
        sys.exit("no archive for %s" % a.id)
    kept = adir / "transcript.jsonl"
    if m.get("transcript") and not os.path.exists(m["transcript"]) and kept.exists():
        os.makedirs(os.path.dirname(m["transcript"]), exist_ok=True)
        os.link(kept, m["transcript"])
        print("transcript restored to %s" % m["transcript"])
    if (adir / "tmp").is_dir():
        print("job tmp/ files kept at %s" % (adir / "tmp"))
    for name in m.get("tmp_skipped") or []:
        print("  not archived: tmp/%s" % name)
    print("cd %s && claude --resume %s" % (m.get("cwd") or home(), m.get("sessionId")))


def cmd_schedule(a):
    if a.action == "install":
        print("installed the %s job: %s" % (schedule_install(), every(config()["interval_minutes"])))
    elif a.action == "uninstall":
        print("removed the %s job (it won't be reinstalled automatically)" % schedule_uninstall())
    elif a.action == "ensure":
        state = schedule_ensure()
        if state.startswith("newly-installed"):
            print("first run: installed the %s job; it runs `session-sweep run --apply --spawn-interview` %s. "
                  "Remove it with `session-sweep schedule uninstall`."
                  % (state.split(":")[1], every(config()["interval_minutes"])))
        elif state == "declined":
            print("the scheduled job was uninstalled earlier; `session-sweep schedule install` brings it back")
        else:
            print("the scheduled job is installed")
    else:
        print(json.dumps(load_json(data_dir() / "schedule.json", {"state": "never"}), indent=1))


def cmd_status(a):
    print("schedule: %s" % schedule_state())
    runs = []
    path = data_dir() / "log.jsonl"
    if path.exists():
        runs = [json.loads(l) for l in path.read_text().splitlines() if '"event": "run"' in l]
    print("last run: %s" % (runs[-1]["at"] + " (closed %d)" % runs[-1]["closed"] if runs else "never"))
    rows = classify(load_sessions(), load_json(data_dir() / "decisions.json", {}))
    print("now: %d to close, %d to ask about, %d to keep" % tuple(
        sum(1 for r in rows if r["verdict"] == v) for v in ("close", "ask", "keep")))


def main(argv=None):
    p = argparse.ArgumentParser(prog="session-sweep", description=__doc__.split("\n")[0])
    p.add_argument("--version", action="version", version="session-sweep " + __version__)
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("scan", help="show what would happen, change nothing")
    s.add_argument("--json", action="store_true")
    s.add_argument("--all", action="store_true", help="include KEEP rows")
    s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("run", help="close the CLOSE rows")
    s.add_argument("--apply", action="store_true", help="actually close (default is a dry run)")
    s.add_argument("--spawn-interview", action="store_true", help="start an interview session for new ASK rows")
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("decide", help="record your answer for ASK rows")
    s.add_argument("decision", choices=["close", "keep"])
    s.add_argument("ids", nargs="+")
    s.set_defaults(fn=cmd_decide)
    s = sub.add_parser("restore", help="bring back a closed session's transcript and print the resume command")
    s.add_argument("id")
    s.set_defaults(fn=cmd_restore)
    s = sub.add_parser("schedule", help="the scheduled job")
    s.add_argument("action", choices=["ensure", "install", "uninstall", "status"])
    s.set_defaults(fn=cmd_schedule)
    sub.add_parser("status", help="schedule, last run and counts").set_defaults(fn=cmd_status)
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return
    a.fn(a)


if __name__ == "__main__":
    main()
