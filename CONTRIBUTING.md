# Contributing

Issues and pull requests are welcome. A few rules keep the tools safe to run next to real agents.

## Tests never touch a live session

Every test uses a stub `claude`, a stub `codex`, a fake Codex daemon, a fake `/proc` and temp log files. Point the tools at them through the environment:

`CLAUDE_BIN`, `CODEX_BIN`, `TMUX_BIN`, `CODEX_APP_SERVER_SOCK`, `CLAUDE_SESSIONS_DIR`, `AGENT_COMMS_LOG`, `AGENT_COMMS_SPAWN_LOG`, `AGENT_MENU_ACTIONS_LOG`, `AGENT_MENU_PROC_ROOT` and `AGENT_MENU_USAGE_CWD`.

A test that needs a real tmux starts a private server with `tmux -S <socket in a temp folder> -f /dev/null` and kills it afterwards. Never use your own tmux server, the real daemon socket, or `~/.claude/sessions` in a test.

```bash
python3 -m unittest discover -s tests
python3 -m unittest discover -s tests -p 'test_hardening.py' -v
```

## Every safety check has a test that can fail

When you add a check, break it on purpose once and confirm a test fails. The existing checks were all verified that way. A test that passes with the check removed does not protect anything.

## Style

- Python 3.9 or newer, standard library only.
- bash tools use `set -euo pipefail` and build every command as an array, never a string.
- A log line is one JSON object, appended under `flock` and synced. Use `lib/private_log.py` or `lib/log_append.sh` rather than writing a log by hand.
- Text from another agent goes through `clean()` in `lib/sources/base.py` before it reaches a terminal or tmux.
- Comments say why, not what.

## Checking a new CLI version

The [compatibility table](docs/reference.md#compatibility) lists every interface these tools depend on and how to recheck it. After upgrading Claude Code or Codex CLI, walk through it and update the tested versions in the README.
