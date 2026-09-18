"""Stubs for the agent-menu tests: a fake `tmux`, a fake `claude`, a fake /proc, temp logs.
The Codex side reuses FakeCodexDaemon from test_peers."""
import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_peers import FakeCodexDaemon  # noqa: E402,F401

USAGE_TEXT = """You are currently using your subscription to power your Claude Code usage

Current session: 18% used · resets Sep 17, 7:40pm (America/New_York)
Current week (all models): 11% used · resets Sep 24, 1pm (America/New_York)
Current week (Fable): 16% used · resets Sep 24, 1pm (America/New_York)

What's contributing to your limits usage?
Last 24h · 1347 requests · 8 sessions
"""

ROOT = Path(__file__).resolve().parents[1]
BIN = Path(os.environ.get("AGENT_COMMS_BIN") or ROOT / "bin")
LIB = Path(os.environ.get("AGENT_COMMS_LIB") or ROOT / "lib")

TMUX_STUB = r"""#!/usr/bin/env bash
d="$STUB_DIR"; printf '%s\n' "$*" >> "$d/tmux-calls.txt"
[[ -n ${STUB_TMUX_FAIL:-} ]] && { echo "no server running" >&2; exit 1; }
target=""; args=("$@"); for ((i=0;i<${#args[@]};i++)); do [[ ${args[i]} == -t ]] && target=${args[i+1]}; done
case $1 in
  list-panes) cat "$d/panes.tsv" ;;
  capture-pane) cat "$d/screen-${target#%}.txt" 2>/dev/null ;;
  display-message) if [[ -f "$d/pid-${target#%}.txt" ]]; then cat "$d/pid-${target#%}.txt"; else echo "can't find pane" >&2; exit 1; fi ;;
  *) : ;;
esac
"""

# Every external program the menu can run has a stub here. A test that forgets to set one must hit
# a stub that fails, never the real binary: a real `codex queue` would message a live session.
CODEX_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_DIR/codex-calls.txt"
exit "${STUB_CODEX_EXIT:-1}"
"""

CLAUDE_STUB = r"""#!/usr/bin/env bash
[[ -n ${STUB_CLAUDE_FAIL:-} ]] && exit 7
[[ $1 == agents && " $* " == *" --all "* ]] && { cat "$STUB_DIR/claude-all.json"; exit 0; }
[[ $1 == agents ]] && { cat "$STUB_DIR/claude.json"; exit 0; }
if [[ $1 == -p ]]; then
  [[ -n ${STUB_USAGE_FAIL:-} ]] && exit 3
  [[ -n ${STUB_USAGE_GARBAGE:-} ]] && { echo 'not json'; exit 0; }
  python3 -c 'import json,os,sys; sys.stdout.write(json.dumps({"result": open(os.environ["STUB_DIR"]+"/usage.txt").read()}))'
fi
"""


def executable(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class Machine:
    """A pretend workstation in a temp folder."""

    def __init__(self, tmp):
        self.dir = Path(tmp)
        self.sock = str(self.dir / "d.sock")
        self.proc = self.dir / "proc"
        self.proc.mkdir()
        self.panes, self.claude, self.claude_finished = [], [], []
        self.usage(USAGE_TEXT)
        self.env = dict(os.environ, STUB_DIR=str(self.dir), CODEX_APP_SERVER_SOCK=self.sock,
                        TMUX_BIN=str(executable(self.dir / "tmux-stub", TMUX_STUB)),
                        CLAUDE_BIN=str(executable(self.dir / "claude-stub", CLAUDE_STUB)),
                        CODEX_BIN=str(executable(self.dir / "codex-stub", CODEX_STUB)),
                        AGENT_MENU_PROC_ROOT=str(self.proc),
                        AGENT_COMMS_LOG=str(self.dir / "messages.jsonl"),
                        AGENT_COMMS_SPAWN_LOG=str(self.dir / "spawned.jsonl"),
                        AGENT_MENU_ACTIONS_LOG=str(self.dir / "menu-actions.jsonl"))
        self.write()

    def process(self, pid, parent, pgrp=None, tpgid=None):
        """A /proc entry. With `pgrp` and `tpgid` it also says whether the process group is the
        foreground of its terminal (pgrp == tpgid), as the real stat line does."""
        folder = self.proc / str(pid)
        folder.mkdir(exist_ok=True)
        tail = f" {pgrp} 0 34816 {tpgid}" if pgrp is not None else " 0 0"
        (folder / "stat").write_text(f"{pid} (some proc) S {parent}{tail}")

    def pane(self, number, pid, command, title, screen=""):
        self.panes.append((number, pid, command, title))
        (self.dir / f"screen-{number}.txt").write_text(screen)
        (self.dir / f"pid-{number}.txt").write_text(f"{pid}\n")
        self.process(pid, 1)
        self.write()

    def claude_session(self, session_id, name, pid, status="idle", state=None, background=True, cwd="/w"):
        self.claude.append({"sessionId": session_id, "id": session_id[:8] if background else None, "name": name,
                            "pid": pid, "status": status if background else None, "state": state, "cwd": cwd,
                            "kind": "background" if background else "interactive"})
        self.write()

    def write(self):
        """A pane is (number, pid, command, title) or, with its flags, (…, in_mode, synchronized)."""
        rows = []
        for n, pid, cmd, title, *flags in self.panes:
            in_mode, synchronized = (list(flags) + [False, False])[:2]
            rows.append(f"%{n}\tmain:1.{n}\t{pid}\t{cmd}\t{int(in_mode)}\t{int(synchronized)}\t{title}")
        rows += rows  # a grouped session lists every pane twice
        (self.dir / "panes.tsv").write_text("\n".join(rows) + ("\n" if rows else ""))
        (self.dir / "claude.json").write_text(json.dumps(self.claude))
        # `claude agents --json --all` adds finished background sessions to the live ones.
        (self.dir / "claude-all.json").write_text(json.dumps(self.claude + self.claude_finished))

    def usage(self, text):
        (self.dir / "usage.txt").write_text(text)

    def log(self, name, *records):
        with open(self.dir / name, "a") as fh:
            for record in records:
                fh.write((record if isinstance(record, str) else json.dumps(record)) + "\n")

    def tmux_calls(self):
        path = self.dir / "tmux-calls.txt"
        return path.read_text().splitlines() if path.exists() else []

    def actions(self):
        path = self.dir / "menu-actions.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
