# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-22

### Added

- The `close-stale-sessions` skill, packaged as the `session-sweep` Claude Code plugin and marketplace, plus `install.sh` for a plain skill install.
- `session_sweep.py` (stdlib only, Python 3.9+): `scan`, `run`, `decide`, `restore`, `status` and `schedule`.
- Handoff detection: a newer session's opening prompt reusing the old session's final message (rare 8-word runs, so shared templates don't count), naming the same handoff doc, or naming the old session's id.
- CLOSE, ASK (with a suggestion) and KEEP verdicts, remembered answers, and a refused `claude rm` turned into a question.
- Archive before every close (state, timeline, transcript hard link), kept 30 days, with `restore`.
- The hourly job (launchd, systemd user timer or cron) installs itself on the skill's first run, and an uninstall is remembered. It starts one interview session when there are new questions.
- `config.json` settings, a run lock, a unittest suite on temp config dirs, CI on Ubuntu and macOS with Python 3.9 and 3.14, `scripts/demo.py` and issue templates.
