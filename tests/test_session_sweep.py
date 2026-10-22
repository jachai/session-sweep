"""session-sweep tests.

Every test builds its own Claude config dir under a temp HOME and points SESSION_SWEEP_CLAUDE at a
fake `claude` that serves `agents --json`, deletes job dirs on `rm` and records `--bg` calls, so
nothing reads your real ~/.claude or runs the real CLI.

    python -m unittest discover -s tests
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills/close-stale-sessions/scripts/session_sweep.py"
spec = importlib.util.spec_from_file_location("session_sweep", SCRIPT)
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)

H = 3600
NOW = time.time()

FAKE_CLAUDE = textwrap.dedent("""\
    #!%s
    import json, os, shutil, sys
    cfg = os.environ["CLAUDE_CONFIG_DIR"]
    args = sys.argv[1:]
    with open(os.path.join(cfg, "fake-calls.jsonl"), "a") as f:
        f.write(json.dumps(args) + "\\n")
    if args[:1] == ["agents"]:
        print(open(os.path.join(cfg, "fake-agents.json")).read())
    elif args[:1] == ["rm"]:
        if args[1] in os.environ.get("FAKE_REFUSE", "").split(","):
            print("refusing: worktree has unpushed commits"); sys.exit(1)
        shutil.rmtree(os.path.join(cfg, "jobs", args[1]))
    elif args[:1] == ["--bg"]:
        print("started abcd1234")
""") % sys.executable


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / ".claude"
        (self.cfg / "jobs").mkdir(parents=True)
        (self.cfg / "projects").mkdir()
        fake = self.tmp / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(0o755)
        self.agents = []
        env = {"HOME": str(self.tmp), "CLAUDE_CONFIG_DIR": str(self.cfg), "SESSION_SWEEP_CLAUDE": str(fake),
               "SESSION_SWEEP_SCHEDULER": "none", "CLAUDE_JOB_DIR": ""}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def session(self, sid, state="done", idle_h=48, created_h=None, intent="do a thing", texts=(), detail="",
                pid=None, kind="background", worktree=None, in_flight=None, name=None):
        sdir = self.cfg / "jobs" / sid
        sdir.mkdir()
        last = NOW - idle_h * H
        created = NOW - (created_h if created_h is not None else idle_h + 1) * H
        transcript = self.cfg / "projects" / (sid + ".jsonl")
        transcript.write_text("{}\n")
        os.utime(transcript, (last, last))
        st = {"state": state, "detail": detail, "name": name or "session " + sid, "intent": intent,
              "sessionId": sid + "-0000-uuid", "cwd": "/work", "createdAt": iso(created), "updatedAt": iso(last),
              "linkScanPath": str(transcript)}
        if worktree:
            st["worktreePath"] = worktree
        if in_flight:
            st["inFlight"] = in_flight
        (sdir / "state.json").write_text(json.dumps(st))
        (sdir / "timeline.jsonl").write_text("".join(json.dumps({"state": state, "text": t}) + "\n" for t in texts))
        entry = {"id": sid, "kind": kind, "state": state, "name": st["name"], "sessionId": st["sessionId"]}
        if pid:
            entry.update(pid=pid, status="idle")
        self.agents.append(entry)
        return sid

    def write_agents(self):
        (self.cfg / "fake-agents.json").write_text(json.dumps(self.agents))

    def rows(self, decisions=None):
        self.write_agents()
        return {r["id"]: r for r in cs.classify(cs.load_sessions(), decisions or {})}

    def run_cli(self, *argv):
        self.write_agents()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cs.main(list(argv))
        return out.getvalue()

    def calls(self):
        path = self.cfg / "fake-calls.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


HANDOFF = ("Resume prompt: continue the billing export rewrite on branch feat/billing-export. Wave two "
           "is done and pushed at 4f2a91c; next run the reconciliation script against the March ledger "
           "and fix the rounding drift in invoice totals before opening the pull request.")


class Successors(Env):
    def test_newer_session_started_from_the_handoff_prompt(self):
        self.session("old00001", state="blocked", idle_h=30, texts=["work done", HANDOFF])
        self.session("new00001", state="working", idle_h=1, created_h=29, pid=11, intent=HANDOFF + " Go.")
        r = self.rows()["old00001"]
        self.assertEqual(r["verdict"], "close")
        self.assertIn("new00001 started from its handoff prompt", r["reason"])

    def test_session_started_before_the_handoff_is_not_a_successor(self):
        self.session("old00002", state="blocked", idle_h=30, texts=[HANDOFF])
        self.session("new00002", state="done", idle_h=40, created_h=31, intent=HANDOFF)
        self.assertEqual(self.rows()["old00002"]["verdict"], "ask")

    def test_shared_boilerplate_does_not_link_sessions(self):
        boiler = "Read the handoff doc and follow the house rules for every change you make in this repository today"
        for i in range(5):
            self.session("bp%06d" % i, state="blocked", idle_h=50 - i * 5, texts=[boiler + " task %d" % i])
        self.session("newer000", state="working", idle_h=1, created_h=2, pid=5, intent=boiler)
        self.assertTrue(all(r["verdict"] == "ask" for k, r in self.rows().items() if k.startswith("bp")))

    def test_resumed_the_same_handoff_doc(self):
        self.session("old00003", state="blocked", idle_h=30, texts=["Wrote .agent-handoff/feat__export.md"])
        self.session("new00003", state="done", idle_h=3, created_h=20, intent="resume from .agent-handoff/feat__export.md")
        self.assertIn("resumed its handoff doc", self.rows()["old00003"]["reason"])

    def test_newer_session_names_the_old_id(self):
        self.session("old00004", state="blocked", idle_h=30)
        self.session("new00004", state="done", idle_h=3, created_h=20, intent="take over from session old00004")
        self.assertIn("names it", self.rows()["old00004"]["reason"])

    def test_link_survives_the_successor_being_closed_first(self):
        self.session("old00005", state="blocked", idle_h=60, texts=[HANDOFF])
        arch = self.cfg / "session-sweep/archive/gone0005"
        arch.mkdir(parents=True)
        (arch / "state.json").write_text(json.dumps({"name": "gone", "createdAt": iso(NOW - 50 * H), "intent": HANDOFF}))
        self.assertIn("gone0005", self.rows()["old00005"]["reason"])


class Verdicts(Env):
    def test_completed_and_idle_a_day_closes(self):
        self.session("done0001", idle_h=30)
        self.assertEqual(self.rows()["done0001"]["verdict"], "close")

    def test_recently_completed_is_kept(self):
        self.session("done0002", idle_h=5)
        self.assertEqual(self.rows()["done0002"]["verdict"], "keep")

    def test_active_in_the_last_two_hours_is_kept_even_if_superseded(self):
        self.session("old00006", state="blocked", idle_h=1, created_h=5, texts=[HANDOFF])
        self.session("new00006", state="working", idle_h=0.1, created_h=0.9, pid=3, intent=HANDOFF)
        self.assertEqual(self.rows()["old00006"]["verdict"], "keep")

    def test_waiting_on_input_asks_with_a_suggestion(self):
        self.session("wait0001", state="blocked", idle_h=10, detail="should I merge?")
        self.session("wait0002", state="blocked", idle_h=24 * 9, detail="should I merge?")
        self.session("wait0003", state="blocked", idle_h=10, texts=["start a fresh session with the prompt above"])
        rows = self.rows()
        self.assertEqual((rows["wait0001"]["verdict"], rows["wait0001"]["suggest"]), ("ask", "keep"))
        self.assertEqual(rows["wait0002"]["suggest"], "close")
        self.assertEqual(rows["wait0003"]["suggest"], "close")

    def test_completed_with_a_worktree_asks(self):
        self.session("wt000001", idle_h=30, worktree="/repo/.claude/worktrees/fix-x")
        self.assertEqual(self.rows()["wt000001"]["verdict"], "ask")

    def test_stopped_and_failed_ask(self):
        self.session("stop0001", state="stopped", idle_h=30)
        self.session("fail0001", state="failed", idle_h=30)
        rows = self.rows()
        self.assertEqual({rows["stop0001"]["suggest"], rows["fail0001"]["suggest"]}, {"close"})

    def test_working_with_a_live_process_is_kept(self):
        self.session("work0001", state="working", idle_h=30, pid=99)
        self.assertEqual(self.rows()["work0001"]["verdict"], "keep")

    def test_working_without_a_process_asks(self):
        self.session("work0002", state="working", idle_h=30)
        self.assertEqual(self.rows()["work0002"]["verdict"], "ask")

    def test_leftover_in_flight_on_an_exited_session_is_ignored(self):
        self.session("left0001", idle_h=30, in_flight={"tasks": 1, "kinds": ["local_bash"]})
        self.session("watch001", idle_h=30, pid=7, in_flight={"tasks": 1, "kinds": ["artifact_watch"]})
        self.session("busy0001", idle_h=30, pid=8, in_flight={"tasks": 1, "kinds": ["local_bash"]})
        rows = self.rows()
        self.assertEqual(rows["left0001"]["verdict"], "close")
        self.assertEqual(rows["watch001"]["verdict"], "close")
        self.assertEqual(rows["busy0001"]["verdict"], "keep")

    def test_interactive_and_own_sessions_are_kept(self):
        self.session("term0001", idle_h=30, kind="interactive")
        self.session("self0001", idle_h=30)
        with mock.patch.dict(os.environ, {"CLAUDE_JOB_DIR": str(self.cfg / "jobs/self0001")}):
            rows = self.rows()
        self.assertEqual((rows["term0001"]["verdict"], rows["self0001"]["verdict"]), ("keep", "keep"))

    def test_keep_decision_holds_until_new_activity(self):
        self.session("wait0004", state="blocked", idle_h=30)
        last = cs.load_sessions({})[0]["last_act"]
        self.assertEqual(self.rows({"wait0004": {"decision": "keep", "last_act": last}})["wait0004"]["verdict"], "keep")
        self.assertEqual(self.rows({"wait0004": {"decision": "keep", "last_act": last - 600}})["wait0004"]["verdict"], "ask")

    def test_config_file_changes_thresholds(self):
        self.session("done0003", idle_h=5)
        (self.cfg / "session-sweep").mkdir()
        (self.cfg / "session-sweep/config.json").write_text(json.dumps({"done_idle_hours": 4}))
        self.assertEqual(self.rows()["done0003"]["verdict"], "close")


class Closing(Env):
    def test_run_without_apply_changes_nothing(self):
        self.session("done0004", idle_h=30)
        self.assertIn("dry run", self.run_cli("run"))
        self.assertTrue((self.cfg / "jobs/done0004").exists())

    def test_run_apply_archives_then_removes(self):
        self.session("done0005", idle_h=30)
        transcript = self.cfg / "projects/done0005.jsonl"
        self.assertIn("closed 1 of 1", self.run_cli("run", "--apply"))
        self.assertFalse((self.cfg / "jobs/done0005").exists())
        arch = self.cfg / "session-sweep/archive/done0005"
        self.assertTrue((arch / "state.json").exists())
        self.assertEqual(os.stat(arch / "transcript.jsonl").st_ino, os.stat(transcript).st_ino)
        self.assertIn(["rm", "done0005"], self.calls())

    def test_refused_rm_becomes_a_question(self):
        self.session("wt000002", idle_h=30)
        with mock.patch.dict(os.environ, {"FAKE_REFUSE": "wt000002"}):
            self.run_cli("run", "--apply")
        r = self.rows(cs.load_json(self.cfg / "session-sweep/decisions.json", {}))["wt000002"]
        self.assertEqual(r["verdict"], "ask")
        self.assertIn("unpushed", r["reason"])

    def test_decide_close_then_run(self):
        self.session("wait0005", state="blocked", idle_h=30)
        self.run_cli("decide", "close", "wait0005")
        self.run_cli("run", "--apply")
        self.assertFalse((self.cfg / "jobs/wait0005").exists())

    def test_restore_relinks_a_deleted_transcript(self):
        self.session("done0006", idle_h=30)
        self.run_cli("run", "--apply")
        (self.cfg / "projects/done0006.jsonl").unlink()
        out = self.run_cli("restore", "done0006")
        self.assertTrue((self.cfg / "projects/done0006.jsonl").exists())
        self.assertIn("claude --resume done0006-0000-uuid", out)

    def test_interview_starts_once_for_new_questions(self):
        self.session("wait0006", state="blocked", idle_h=30)
        self.run_cli("run", "--apply", "--spawn-interview")
        self.run_cli("run", "--apply", "--spawn-interview")
        bg = [c for c in self.calls() if c[:1] == ["--bg"]]
        self.assertEqual(len(bg), 1)
        self.assertIn("session-sweep interview (1 to review)", bg[0])

    def test_interview_can_be_turned_off(self):
        self.session("wait0007", state="blocked", idle_h=30)
        (self.cfg / "session-sweep").mkdir()
        (self.cfg / "session-sweep/config.json").write_text(json.dumps({"interview": False}))
        self.run_cli("run", "--apply", "--spawn-interview")
        self.assertFalse([c for c in self.calls() if c[:1] == ["--bg"]])


class Schedule(Env):
    def test_first_ensure_installs_and_uninstall_is_remembered(self):
        self.assertIn("first run: installed", self.run_cli("schedule", "ensure"))
        self.assertTrue((self.cfg / "session-sweep/session_sweep.py").exists())
        self.assertIn("is installed", self.run_cli("schedule", "ensure"))
        self.run_cli("schedule", "uninstall")
        self.assertIn("uninstalled earlier", self.run_cli("schedule", "ensure"))

    def test_job_definitions_point_at_the_stable_copy(self):
        cfg = dict(cs.DEFAULTS, interval_minutes=60)
        plist = cs.launchd_plist(cfg)
        self.assertEqual(plist["StartInterval"], 3600)
        self.assertFalse(plist["RunAtLoad"])
        self.assertEqual(plist["ProgramArguments"][1:], [str(cs.stable_script()), "run", "--apply", "--spawn-interview"])
        self.assertEqual(plist["EnvironmentVariables"]["CLAUDE_CONFIG_DIR"], str(self.cfg))
        service, timer = cs.systemd_units(cfg)
        self.assertIn("OnUnitActiveSec=60min", timer)
        self.assertIn(str(cs.stable_script()), service)
        self.assertTrue(cs.cron_line(cfg).startswith("17 */1 * * * "))
        self.assertTrue(cs.cron_line(cs.DEFAULTS).endswith("# session-sweep"))

    def test_default_is_every_twelve_hours(self):
        self.assertEqual(cs.launchd_plist(cs.DEFAULTS)["StartInterval"], 12 * 3600)
        self.assertIn("OnUnitActiveSec=720min", cs.systemd_units(cs.DEFAULTS)[1])
        self.assertTrue(cs.cron_line(cs.DEFAULTS).startswith("17 */12 * * * "))
        self.assertEqual([cs.every(m) for m in (720, 60, 90)], ["every 12 hours", "every hour", "every 90 minutes"])


class Cli(Env):
    def test_scan_on_an_empty_config_dir(self):
        shutil.rmtree(self.cfg / "jobs")
        self.assertIn("0 to close, 0 to ask about, 0 to keep", self.run_cli("scan"))

    def test_scan_json(self):
        self.session("done0007", idle_h=30)
        rows = json.loads(self.run_cli("scan", "--json"))
        self.assertEqual([r["id"] for r in rows], ["done0007"])


if __name__ == "__main__":
    unittest.main()
