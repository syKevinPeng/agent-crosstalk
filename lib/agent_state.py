"""Build the agent tree for the menu. Reads the sources and merges them. Never draws, never acts."""
import dataclasses
import datetime
import re

import auth_readers
from sources import claude_src, codex_src, logs_src, tmux_src
from sources.base import SourceError

RETIRED_VISIBLE = datetime.timedelta(minutes=10)


@dataclasses.dataclass
class Agent:
    key: str                      # "<kind>:<session id>"
    kind: str                     # claude | codex
    name: str
    session_id: str
    short_id: str = ""
    cwd: str = ""
    state: str = "unknown"        # working | idle | stopped | needs_owner | unknown
    background: bool = False
    needs_owner: int = 0
    auth: str = ""                # auth label, e.g. "ssh key passphrase"
    open_messages: int = 0
    parent_key: str = ""
    spawned: bool = False         # created by spawn-peer
    access: str = ""              # ro | rw, spawned agents only
    pane_id: str = ""             # tmux %id, only when the match is certain
    pane_pid: int = 0
    retired: bool = False
    source_error: bool = False
    children: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Snapshot:
    roots: list
    by_key: dict
    errors: list                  # e.g. ["codex: unreachable"]


def _match_panes(agents, panes):
    """Give an agent a pane only when exactly one pane fits. A doubtful match means no pane."""
    trees = {p["pane_id"]: tmux_src.descendants(p["pid"]) | {p["pid"]} for p in panes}
    for agent in agents:
        fits = []
        for pane in panes:
            if pane["command"] != agent.kind:
                continue
            title_parts = [part.strip() for part in pane["title"].split("|")]
            by_pid = agent.kind == "claude" and agent._pid in trees[pane["pane_id"]]
            by_title = agent.name in title_parts
            if by_pid or by_title:
                fits.append(pane)
        if len(fits) == 1:
            agent.pane_id, agent.pane_pid = fits[0]["pane_id"], fits[0]["pid"]
            agent._title = fits[0]["title"]


def _link_parents(agents, spawned):
    by_id = {a.session_id: a for a in agents}
    by_short = {a.short_id: a for a in agents if a.short_id}
    for record in spawned:
        child = by_id.get(record["id"]) or by_short.get(record["id"])
        if not child:
            continue
        child.spawned, child.access = True, record["access"]
        child.retired = record["retired_at"] is not None
        label = record["spawned_by"]
        parent = by_short.get(logs_src.label_short_id(label) or "")
        if not parent:
            wanted = logs_src.normalise(label)
            kind = label.split("/", 1)[0].lower() if "/" in label else ""
            named = [a for a in agents if logs_src.normalise(a.name) == wanted and (not kind or a.kind == kind)]
            parent = named[0] if len(named) == 1 else None
        if parent and parent is not child:
            child.parent_key = parent.key


def collect(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    errors, raw = [], []
    spawned = logs_src.spawned()
    recent_ids = [s["id"] for s in spawned if s["kind"] == "codex"
                  and (s["retired_at"] is None or now - s["retired_at"] < RETIRED_VISIBLE)]
    for label, reader in (("claude", claude_src.list_sessions),
                          ("codex", lambda: codex_src.list_threads(extra_ids=recent_ids))):
        try:
            raw.extend(reader())
        except SourceError:
            errors.append(f"{label}: unreachable")
    try:
        panes = tmux_src.list_panes()
    except SourceError:
        panes, _ = [], errors.append("tmux: unreachable")

    agents = []
    for entry in raw:
        agent = Agent(key=f"{entry['kind']}:{entry['session_id']}", kind=entry["kind"], name=entry["name"],
                      session_id=entry["session_id"], short_id=entry.get("short_id") or "",
                      cwd=entry.get("cwd") or "", state=entry["state"], background=entry.get("background", False))
        agent._pid, agent._title = entry.get("pid"), ""
        agents.append(agent)
    _match_panes(agents, panes)
    _link_parents(agents, spawned)

    open_counts = logs_src.open_messages()
    for agent in agents:
        agent.open_messages = open_counts.get(agent.session_id, 0)
        if agent.state == "needs_owner":
            agent.needs_owner = 1
        if agent.pane_id:
            if "Action Required" in agent._title:
                agent.needs_owner = max(agent.needs_owner, 1)
            try:
                screen = tmux_src.capture(agent.pane_id)
            except SourceError:
                screen = ""
            agent.auth = auth_readers.detect_screen(screen) or ""
            asked = re.search(r"\?\s+(\d+) questions?\b", screen)
            if asked and agent.needs_owner:
                agent.needs_owner = int(asked.group(1))

    # Retired children stay visible for a while, then drop off. Stopped non-spawned threads with
    # nothing open are history, not agents, so they are left out.
    retire_times = {s["id"]: s["retired_at"] for s in spawned}
    visible = []
    for agent in agents:
        retired_at = retire_times.get(agent.session_id) or retire_times.get(agent.short_id)
        if agent.retired and retired_at and now - retired_at >= RETIRED_VISIBLE and not agent.open_messages:
            continue
        if agent.state == "stopped" and not agent.spawned and not agent.open_messages and not agent.pane_id:
            continue
        visible.append(agent)

    by_key = {a.key: a for a in visible}
    roots = []
    for agent in visible:
        parent = by_key.get(agent.parent_key)
        (parent.children if parent else roots).append(agent)

    def urgency(a):
        return (not a.auth, not a.needs_owner, not a.open_messages, a.name.lower())
    for agent in visible:
        agent.children.sort(key=urgency)
    roots.sort(key=urgency)
    return Snapshot(roots=roots, by_key=by_key, errors=errors)
