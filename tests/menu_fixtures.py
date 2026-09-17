"""Stubs for the agent-menu tests: a fake `tmux`, a fake `claude`, a fake /proc, temp logs.
The Codex side reuses FakeCodexDaemon from test_peers."""
import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_peers import FakeCodexDaemon  # noqa: E402,F401

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

CLAUDE_STUB = r"""#!/usr/bin/env bash
[[ -n ${STUB_CLAUDE_FAIL:-} ]] && exit 7
[[ $1 == agents ]] && cat "$STUB_DIR/claude.json"
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
        self.panes, self.claude = [], []
        self.env = dict(os.environ, STUB_DIR=str(self.dir), CODEX_APP_SERVER_SOCK=self.sock,
                        TMUX_BIN=str(executable(self.dir / "tmux-stub", TMUX_STUB)),
                        CLAUDE_BIN=str(executable(self.dir / "claude-stub", CLAUDE_STUB)),
                        AGENT_MENU_PROC_ROOT=str(self.proc),
                        AGENT_COMMS_LOG=str(self.dir / "messages.jsonl"),
                        AGENT_COMMS_SPAWN_LOG=str(self.dir / "spawned.jsonl"),
                        AGENT_MENU_ACTIONS_LOG=str(self.dir / "menu-actions.jsonl"))
        self.write()

    def process(self, pid, parent):
        folder = self.proc / str(pid)
        folder.mkdir(exist_ok=True)
        (folder / "stat").write_text(f"{pid} (some proc) S {parent} 0 0")

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
        rows = [f"%{n}\tmain:1.{n}\t{pid}\t{cmd}\t{title}" for n, pid, cmd, title in self.panes]
        rows += rows  # a grouped session lists every pane twice
        (self.dir / "panes.tsv").write_text("\n".join(rows) + ("\n" if rows else ""))
        (self.dir / "claude.json").write_text(json.dumps(self.claude))

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
