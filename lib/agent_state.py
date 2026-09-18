"""Build the agent tree for the menu. Reads the sources and merges them. Never draws, never acts."""
import dataclasses
import datetime
import re

import auth_readers
from sources import claude_src, codex_src, logs_src, tmux_src
from sources.base import SourceAbsent, SourceError, clean

RETIRED_VISIBLE = datetime.timedelta(minutes=10)
QUESTIONS = re.compile(r"\?\s+(\d+) questions?\b")
# An approval menu at the bottom of a pane, as both CLIs draw it: a first option "1. Yes…" and a later
# "N. No…", with an optional selection cursor in front.
APPROVAL_YES = re.compile(r"^(?:[❯›>]\s*)?1\.\s+Yes\b")
APPROVAL_NO = re.compile(r"^(?:[❯›>]\s*)?\d\.\s+No\b")
# Any other choice menu: a numbered option under the selection cursor, or the footer Claude Code draws
# under a question. Enter would pick the highlighted option there too.
CHOICE_CURSOR = re.compile(r"^[❯›]\s*\d+\.\s+\S")
CHOICE_FOOTER = re.compile(r"\bEnter to select\b")


@dataclasses.dataclass
class Agent:
    key: str                      # "<kind>:<session id>"
    kind: str                     # claude | codex
    name: str
    session_id: str
    short_id: str = ""
    cwd: str = ""
    state: str = "unknown"        # working | idle | stopped | needs_owner | unknown
    model: str = ""               # only where the CLI reports one: Codex does, Claude does not
    pid: int = 0                  # the session's own process, where the CLI reports one
    quit: bool = False            # stopped (Claude) or archived (Codex); Resume brings it back
    parked: bool = False          # quit and shown apart, under QUIT, rather than in the tree
    background: bool = False
    needs_owner: int = 0
    auth: str = ""                # auth label, e.g. "ssh key passphrase"
    open_messages: int = 0
    parent_key: str = ""
    spawned: bool = False         # created by spawn-peer
    access: str = ""              # ro | rw, spawned agents only
    pane_id: str = ""             # tmux %id, only when the match is certain
    maybe_in_pane: bool = False   # some pane could be showing it, certain or not: no Quit from the menu
    pane_pid: int = 0
    _title: str = dataclasses.field(default="", repr=False)   # the pane's title, read for Codex prompts
    retired: bool = False
    source_error: bool = False
    children: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Snapshot:
    roots: list
    by_key: dict
    errors: list                  # e.g. ["codex: unreachable"]
    quit_agents: list = dataclasses.field(default_factory=list)   # shown apart, below the tree


def title_names(kind, title):
    """Every name a pane title could be showing. Claude: the whole title. Codex: `<name> | <project>`,
    perhaps with a status segment in front, and a name may itself contain `|`, so every run of
    segments that ends before the project counts. Each reading is also tried without a leading status
    glyph, because the CLIs put a spinner there while busy. Returning all readings, not one guess,
    lets the two-claimants rule settle any doubt: a pane that fits two agents goes to neither."""
    parts = [part.strip() for part in title.split("|")]
    if kind == "codex" and len(parts) >= 2:
        segments = {" | ".join(parts[i:-1]) for i in range(len(parts) - 1)}
    else:
        segments = {title.strip()}
    names = set(segments) | {re.sub(r"^[^\w\s]{1,2}\s+", "", segment) for segment in segments}
    return {clean(name) for name in names if name}


def waits_on_owner(title, screen):
    """True when a pane shows a prompt that waits on the owner: Codex's "Action Required" title, or an
    approval menu at the bottom of the screen. Text typed into such a pane answers the prompt: Enter
    picks the highlighted option, and Codex takes single letters as answers too."""
    if "Action Required" in (title or ""):
        return True
    lines = auth_readers.tail(screen)
    if any(CHOICE_CURSOR.search(line) or CHOICE_FOOTER.search(line) for line in lines):
        return True
    first = next((i for i, line in enumerate(lines) if APPROVAL_YES.search(line)), None)
    return first is not None and any(APPROVAL_NO.search(line) for line in lines[first + 1:])


def pane_for_pid(pid, panes):
    """The pane a process runs in: walk up from it to the first tmux pane, or None."""
    by_pid = {pane["pid"]: pane for pane in panes}
    for walked, _ in tmux_src.ancestors(pid):
        if walked in by_pid:
            return by_pid[walked]
    return None


def sessions_between(pid, pane_pid, session_pids):
    """True when another session's process sits between this one and its pane. That happens two ways:
    the owner attached a background session from inside another session, which is fine and the pane
    title then names the attached one; or an agent's Bash tool started a second agent, where the
    title still names the parent. The title is what tells them apart."""
    for walked, _ in tmux_src.ancestors(pid):
        if walked == pane_pid:
            return False
        if walked != pid and walked in session_pids:
            return True
    return False


def _match_panes(agents, panes):
    """Give an agent a pane only when exactly one pane fits it AND no other agent fits that pane.
    A doubtful match means no pane: the menu then offers no typing at all."""
    session_pids = {a.pid for a in agents if a.pid}
    claims = {}
    for agent in agents:
        if agent.state == "stopped":
            continue  # a thread that is not running cannot be what a live pane shows
        if agent.kind == "claude":
            # Claude Code rewrites its pane title with live status ("2 awaiting input · claude
            # agents"), so a title is not an identifier. Only the process tells the truth: walk up
            # from the session to its pane. A background session is detached under init and runs in
            # no pane at all, so it gets none, and the popup offers Attach instead.
            owner = pane_for_pid(agent.pid, panes) if tmux_src.alive(agent.pid) else None
            fits = [owner] if owner and owner["command"] == "claude" else []
            if fits and sessions_between(agent.pid, owner["pid"], session_pids):
                fits = []                       # it runs inside another session, whose pane that is
        else:
            # Codex puts the thread name in its pane title and gives no process id to walk.
            fits = [pane for pane in panes if pane["command"] == agent.kind
                    and agent.name in title_names(agent.kind, pane["title"])]
        agent.maybe_in_pane = bool(fits)
        if len(fits) == 1:
            claims.setdefault(fits[0]["pane_id"], []).append((agent, fits[0]))
    for claimants in claims.values():
        if len(claimants) > 1:
            # An attached session shares its process chain with the session that attached it. The
            # pane shows one of them, and its title says which. Naming exactly one settles it.
            named = [(a, p) for a, p in claimants if a.name in title_names(a.kind, p["title"])]
            claimants = named if len(named) == 1 else []
        if len(claimants) == 1:
            agent, pane = claimants[0]
            agent.pane_id, agent.pane_pid, agent._title = pane["pane_id"], pane["pid"], pane["title"]


def _mark_spawned(agents, spawned):
    """Spawn flags only, for quit agents: they are not placed in the tree, but Quit and Resume
    still need to know which spawn record they belong to."""
    by_id = {a.session_id: a for a in agents}
    by_short = {a.short_id: a for a in agents if a.short_id}
    for record in spawned:
        agent = by_id.get(record["id"]) or by_short.get(record["id"])
        if agent:
            agent.spawned, agent.access = True, record["access"]
            agent.retired = record["retired_at"] is not None


def _link_parents(agents, spawned):
    by_id = {a.session_id: a for a in agents}
    by_short = {a.short_id: a for a in agents if a.short_id}
    for record in spawned:
        child = by_id.get(record["id"]) or by_short.get(record["id"])
        if not child:
            continue
        child.spawned, child.access = True, record["access"]
        # A running agent is not retired, whatever the record says: it was brought back, by Resume
        # whose record failed, or by hand with `claude attach` or `codex unarchive`.
        child.retired = record["retired_at"] is not None and child.quit
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


def collect(now=None, include_quit=False):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    errors, raw = [], []
    try:
        spawned = logs_src.spawned()
    except SourceError:
        spawned, _ = [], errors.append("spawn log: unreadable")
    # Spawned agents are looked up by their recorded ids too, because a finished one drops out of
    # the plain listings while it is still spawn-peer's to retire.
    recent = [s for s in spawned if s["retired_at"] is None or now - s["retired_at"] < RETIRED_VISIBLE]
    claude_ids = [s["id"] for s in recent if s["kind"] == "claude"]
    codex_ids = [s["id"] for s in recent if s["kind"] == "codex"]
    for label, reader in (("claude", lambda: claude_src.list_sessions(extra_ids=claude_ids, include_quit=include_quit,
                                                                    errors=errors)),
                          ("codex", lambda: codex_src.list_threads(extra_ids=codex_ids, include_quit=include_quit))):
        try:
            raw.extend(reader())
        except SourceAbsent:
            pass                                  # that CLI is not installed or running: nothing to show
        except SourceError as exc:
            errors.append(str(exc) if str(exc).startswith(label) else f"{label}: unreachable")
    try:
        panes = tmux_src.list_panes()
    except SourceError:
        panes, _ = [], errors.append("tmux: unreachable")

    agents = []
    for entry in raw:
        agent = Agent(key=f"{entry['kind']}:{entry['session_id']}", kind=entry["kind"], name=entry["name"],
                      session_id=entry["session_id"], short_id=entry.get("short_id") or "",
                      cwd=entry.get("cwd") or "", state=entry["state"], model=entry.get("model") or "", background=entry.get("background", False))
        agent.pid, agent._title = entry.get("pid") or 0, ""
        agent.quit = bool(entry.get("quit"))
        agents.append(agent)
    # A stopped agent that spawn-peer created is still part of its parent's work, so it stays in the
    # tree, where its unread messages show, until a while after it is retired: the same while any
    # retired child stays. Every other stopped agent is parked under QUIT. Either way it is `quit`,
    # so its popup offers Resume.
    retire_times = {s["id"]: s["retired_at"] for s in spawned}

    def overdue(agent):
        retired_at = retire_times.get(agent.session_id) or retire_times.get(agent.short_id)
        return bool(retired_at) and (now - retired_at >= RETIRED_VISIBLE or retired_at > now)  # a future time is a bad record

    stopped = [a for a in agents if a.quit]
    _mark_spawned(stopped, spawned)
    quit_agents = [a for a in stopped if not a.spawned or (a.retired and overdue(a))]
    for agent in quit_agents:
        agent.parked = True
    agents = [a for a in agents if not a.parked]
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
            try:
                screen = tmux_src.capture(agent.pane_id)
            except SourceError:
                screen = ""
            if waits_on_owner(agent._title, screen):
                agent.needs_owner = max(agent.needs_owner, 1)
            agent.auth = auth_readers.detect_screen(screen) or ""
            asked = QUESTIONS.search(screen)
            if asked and agent.needs_owner:
                agent.needs_owner = int(asked.group(1))

    # Retired children stay visible for a while, then drop off. Stopped non-spawned threads with
    # nothing open are history, not agents, so they are left out.
    visible = []
    for agent in agents:
        if agent.retired and overdue(agent) and not agent.open_messages:
            continue
        if agent.state == "stopped" and not agent.spawned and not agent.open_messages and not agent.pane_id:
            continue
        visible.append(agent)

    # The QUIT group is drawn only while the owner asked for it. Without `a`, a stopped agent reaches
    # this point only through its spawn record, and once it is parked it is not shown.
    quit_agents = sorted(quit_agents, key=lambda a: a.name.lower()) if include_quit else []
    for agent in quit_agents:
        agent.open_messages = open_counts.get(agent.session_id, 0)
    by_key = {a.key: a for a in visible + quit_agents}
    in_tree = {a.key: a for a in visible}
    roots = []
    for agent in visible:
        parent = in_tree.get(agent.parent_key)       # only a live agent can hold children
        (parent.children if parent else roots).append(agent)

    def urgency(a):
        return (not a.auth, not a.needs_owner, not a.open_messages, a.name.lower())
    for agent in visible:
        agent.children.sort(key=urgency)
    roots.sort(key=urgency)
    return Snapshot(roots=roots, by_key=by_key, errors=errors, quit_agents=quit_agents)


def safe_collect(include_quit=False):
    """collect(), or a snapshot that says the refresh failed. The menu must never keep showing the
    last good tree as if it were current, and one bad refresh must not end the sidebar."""
    try:
        return collect(include_quit=include_quit)
    except Exception as exc:
        return Snapshot(roots=[], by_key={}, errors=[f"refresh failed: {type(exc).__name__}"])
