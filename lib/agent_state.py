"""Build the agent tree for the menu. Reads the sources and merges them. Never draws, never acts."""
import dataclasses
import datetime
import re
import unicodedata

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


def clean(name):
    """A session name is chosen by the agent, so it is untrusted text. Control characters, escape
    sequences, newlines and tabs become `?` before the name reaches a screen, stdout or a log."""
    return "".join(ch if ch.isprintable() and unicodedata.category(ch) != "Cf" else "?" for ch in name or "")


def title_name(kind, title):
    """The session name a CLI puts in its pane title. Claude: `<status glyph> <name>`.
    Codex: `[<status> |] <glyph> <name> | <project>`, so the name is the segment before the last."""
    parts = [part.strip() for part in title.split("|")]
    segment = parts[-2] if kind == "codex" and len(parts) >= 2 else parts[0] if kind == "codex" else title.strip()
    return re.sub(r"^[^\w\s]{1,2}\s+", "", segment)   # drop a leading spinner or status glyph


def _match_panes(agents, panes):
    """Give an agent a pane only when exactly one pane fits it AND no other agent fits that pane.
    A doubtful match means no pane: the menu then offers no typing at all."""
    trees = {p["pane_id"]: tmux_src.descendants(p["pid"]) | {p["pid"]} for p in panes}
    claims = {}
    for agent in agents:
        if agent.state == "stopped":
            continue  # a thread that is not running cannot be what a live pane shows
        fits = []
        for pane in panes:
            if pane["command"] != agent.kind:
                continue
            by_pid = agent.kind == "claude" and agent._pid in trees[pane["pane_id"]]
            by_title = clean(title_name(agent.kind, pane["title"])) == agent.name
            if by_pid or by_title:
                fits.append(pane)
        if len(fits) == 1:
            claims.setdefault(fits[0]["pane_id"], []).append((agent, fits[0]))
    for claimants in claims.values():
        if len(claimants) == 1:
            agent, pane = claimants[0]
            agent.pane_id, agent.pane_pid, agent._title = pane["pane_id"], pane["pid"], pane["title"]


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
        if not parent and "/" in label:
            # A label is what the creator called itself. Without a short id it must at least name
            # its kind, and exactly one live agent of that kind may carry the name.
            kind, wanted = label.split("/", 1)[0].lower(), logs_src.normalise(label)
            named = [a for a in agents if a.kind == kind and logs_src.normalise(a.name) == wanted]
            parent = named[0] if len(named) == 1 else None
        if parent and parent is not child:
            child.parent_key = parent.key
    # A forged or mistaken record can make a loop (A made B, B made A). Cut it, so nobody vanishes.
    by_key = {a.key: a for a in agents}
    for agent in agents:
        seen, walker = {agent.key}, agent
        while walker.parent_key in by_key:
            if walker.parent_key in seen:
                walker.parent_key = ""
                break
            seen.add(walker.parent_key)
            walker = by_key[walker.parent_key]


def collect(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    errors, raw = [], []
    try:
        spawned = logs_src.spawned()
    except SourceError:
        spawned, _ = [], errors.append("spawn log: unreadable")
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
        agent = Agent(key=f"{entry['kind']}:{entry['session_id']}", kind=entry["kind"], name=clean(entry["name"]),
                      session_id=entry["session_id"], short_id=entry.get("short_id") or "",
                      cwd=entry.get("cwd") or "", state=entry["state"], background=entry.get("background", False))
        agent._pid, agent._title = entry.get("pid"), ""
        agents.append(agent)
    _match_panes(agents, panes)
    _link_parents(agents, spawned)

    try:
        open_counts = logs_src.open_messages()
    except SourceError:
        open_counts, _ = {}, errors.append("message log: unreadable")
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
        overdue = retired_at and (now - retired_at >= RETIRED_VISIBLE or retired_at > now)  # a future time is a bad record
        if agent.retired and overdue and not agent.open_messages:
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
