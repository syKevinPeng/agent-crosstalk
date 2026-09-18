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

Current session: 18% used · resets Sep 18, 1:40am (Europe/Berlin)
Current week (all models): 11% used · resets Sep 24, 7pm (Europe/Berlin)
Current week (Fable): 16% used · resets Sep 24, 7pm (Europe/Berlin)

What's contributing to your limits usage?
Last 24h · 212 requests · 3 sessions
"""

ROOT = Path(__file__).resolve().parents[1]
BIN = Path(os.environ.get("AGENT_COMMS_BIN") or ROOT / "bin")
LIB = Path(os.environ.get("AGENT_COMMS_LIB") or ROOT / "lib")

TMUX_STUB = r"""#!/usr/bin/env bash
d="$STUB_DIR"; printf '%s\n' "$*" >> "$d/tmux-calls.txt"
[[ -n ${STUB_TMUX_FAIL:-} ]] && { echo "no server running" >&2; exit 1; }
target=""; args=("$@"); for ((i=0;i<${#args[@]};i++)); do [[ ${args[i]} == -t ]] && target=${args[i+1]}; done
# Options are kept as files, as tmux 3.4 scopes them: popt-<pane number>-<name> on a pane, wopt-<window
# number>-<name> on a window (window-N files say which window pane N is in, default @1), and gopt-<name>
# for the global value a window inherits. Tests read a pane's and a window's end state from them.
[[ -n $target && -f "$d/gone-${target#%}" ]] && { echo "can't find pane: $target" >&2; exit 1; }
win_of() { if [[ -f "$d/window-$1" ]]; then cat "$d/window-$1"; else echo "@1"; fi; }
scope=p; inherited=0; unset_=0; pos=()
for ((i=1;i<${#args[@]};i++)); do
  case ${args[i]} in -p) scope=p ;; -w) scope=w ;; -A) inherited=1 ;; -u) unset_=1 ;; -q|-v|-a) ;;
    -t|-F) i=$((i+1)) ;; *) pos+=("${args[i]}") ;; esac
done
if [[ $scope == w ]]; then file="$d/wopt-${target#@}-${pos[0]}"; else file="$d/popt-${target#%}-${pos[0]}"; fi
case $1 in
  list-panes) if [[ " $* " == *"@agent_menu_highlight"* ]]; then
                for f in "$d"/popt-*-@agent_menu_highlight; do
                  [[ -f $f ]] || continue
                  n=${f#"$d/popt-"}; n=${n%%-@agent_menu_highlight}
                  [[ -n $target && $(win_of "$n") != "$target" ]] && continue
                  printf '%%%s\t%s\n' "$n" "$(cat "$f")"
                done
              else cat "$d/panes.tsv"; fi ;;
  list-windows) for f in "$d"/wopt-*-@agent_menu_highlight_window; do
                  [[ -f $f ]] || continue
                  n=${f#"$d/wopt-"}; printf '@%s\t%s\n' "${n%%-@agent_menu_highlight_window}" "$(cat "$f")"
                done ;;
  capture-pane) cat "$d/screen-${target#%}.txt" 2>/dev/null ;;
  display-message) if [[ " $* " == *"window_id"* ]]; then win_of "${target#%}"
                   elif [[ -f "$d/pid-${target#%}.txt" ]]; then cat "$d/pid-${target#%}.txt"
                   else echo "can't find pane" >&2; exit 1; fi ;;
  show-options) if [[ -f $file ]]; then cat "$file"; echo
                elif ((inherited)); then if [[ -f "$d/gopt-${pos[0]}" ]]; then cat "$d/gopt-${pos[0]}"; echo; else echo default; fi
                fi ;;
  set-option) if ((unset_)); then rm -f "$file"; else printf '%s' "${pos[1]}" > "$file"; fi ;;
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
printf '%s\n' "$*" >> "$STUB_DIR/claude-calls.txt"
[[ -n ${STUB_CLAUDE_FAIL:-} ]] && exit 7
if [[ $1 == agents ]]; then
  if [[ " $* " == *" --all "* ]]; then cat "$STUB_DIR/claude-all.json"; else cat "$STUB_DIR/claude.json"; fi
  exit 0
fi
if [[ $1 == stop ]]; then
  [[ -n ${STUB_STOP_FAIL:-} ]] && { echo "no such session" >&2; exit 4; }
  exit 0
fi
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

    def quit_claude_session(self, session_id, name, cwd="/w"):
        """A stopped background session: listed by `claude agents --json --all` only, the way the CLI
        lists it (`state: done`, no process)."""
        self.claude_finished.append({"sessionId": session_id, "id": session_id[:8], "name": name, "pid": None,
                                 "status": None, "state": "done", "cwd": cwd, "kind": "background"})
        self.write()

    def claude_calls(self):
        path = self.dir / "claude-calls.txt"
        return path.read_text().splitlines() if path.exists() else []

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

    def pane_options(self, number):
        """The options a pane sets itself, as the stub tmux keeps them."""
        prefix = f"popt-{number}-"
        return {f.name[len(prefix):]: f.read_text() for f in self.dir.glob(prefix + "*")}

    def window_options(self, number=1):
        """The options a window sets itself (window @1 unless a test moved a pane elsewhere)."""
        prefix = f"wopt-{number}-"
        return {f.name[len(prefix):]: f.read_text() for f in self.dir.glob(prefix + "*")}

    def tmux_calls(self):
        path = self.dir / "tmux-calls.txt"
        return path.read_text().splitlines() if path.exists() else []

    def actions(self):
        path = self.dir / "menu-actions.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
