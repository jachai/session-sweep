# Contributing

Issues and pull requests are welcome. session-sweep is one script,
`skills/close-stale-sessions/scripts/session_sweep.py`, using only the standard library and running on Python 3.9+.
Please keep it that way: no runtime dependencies and no new modules.

## Tests

The tests need [uv](https://docs.astral.sh/uv/), which fetches the Python versions for you. CI runs the oldest and
newest supported versions on Ubuntu and macOS:

```sh
uv run --no-project --python 3.9 python -m unittest discover -s tests
uv run --no-project --python 3.14 python -m unittest discover -s tests
uvx pyflakes skills scripts tests
claude plugin validate .
```

Every test builds its own Claude config dir in a temp folder, points `SESSION_SWEEP_CLAUDE` at a fake `claude`, and
sets `SESSION_SWEEP_SCHEDULER=none`, so nothing reads your real `~/.claude`, runs the real CLI or installs a job. New
tests should do the same.

## Changing how it decides

The rules are in `classify()` and `find_successors()`. A change to them needs a test showing a session that's now
judged differently, and an update to "How it decides" in the README and in `SKILL.md`. When in doubt, a rule should
produce an ASK, not a CLOSE: closing the wrong session costs more than one extra question.

## Privacy

Never commit real session data: no transcripts, `state.json` files, session names, prompts or paths from your own
machine. This applies to tests, issues and examples. Use `scripts/demo.py` or made-up fixtures instead.

## Releases

1. Move the `Unreleased` notes in `CHANGELOG.md` under a new version heading.
2. Bump `__version__` in `session_sweep.py` and `version` in `.claude-plugin/plugin.json`.
3. Push a `vX.Y.Z` tag. The release workflow runs the tests, checks the versions match and creates a GitHub release
   with that version's changelog notes. Plugin users get the update through `/plugin marketplace update`.
