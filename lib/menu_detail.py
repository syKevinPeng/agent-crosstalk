"""Text of the popup for one agent. Pure: takes data, returns lines and the buttons that apply."""
import os
import textwrap

import menu_render
import menu_style

LABELS = {"auth": "needs login", "needs": "needs you", "unanswered": "unanswered",
          "working": "working", "idle": "idle", "stopped": "stopped"}


def short_path(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path and path.startswith(home) else (path or "")


def look_only(environ=None):
    """AGENT_MENU_LOOK_ONLY=1: show everything, but offer nothing that types into an agent or retires one."""
    return bool((os.environ if environ is None else environ).get("AGENT_MENU_LOOK_ONLY"))


def buttons(agent):
    """Only actions that can work for this agent are offered."""
    out = []
    if agent.retired:
        return []
    if agent.pane_id:
        out.append("Open pane")
    elif agent.kind == "claude" and agent.background and agent.short_id:
        out.append("Attach")
    if can_instruct(agent):
        out.append("Send")
    if agent.spawned and not look_only():
        out.append("Retire")
    return out


def button_labels(offered, columns):
    """Labels with their key, or bare names when the row would not fit."""
    keys = {"Open pane": "o", "Attach": "t", "Retire": "r"}
    full = [f"[ {b} ]" if b == "Send" else f"[ {b} ({keys[b]}) ]" for b in offered]
    if sum(menu_render.width(label) + 2 for label in full) <= columns:
        return full
    return [f"[{b}]" for b in offered]


def can_instruct(agent):
    """An instruction typed into a pane lands on whatever the pane shows, so typing stops while it
    shows a credential prompt or waits on the owner: there Enter or a letter would answer the prompt.
    A Codex agent with no pane gets the instruction through `codex queue`, which types into nothing,
    so a login problem its latest answer reports leaves the instruction line on for the retry."""
    if agent.retired or look_only():
        return False
    if agent.pane_id:
        return not (agent.auth or agent.needs_owner)
    return agent.kind == "codex"


def title(agent, parent_name):
    origin = f"spawned by: {parent_name}" if parent_name else ("spawned" if agent.spawned else "started by you")
    access = f" · {agent.access}" if agent.access else ""
    return f"{agent.name} · {agent.kind}{access} · {short_path(agent.cwd)} · {origin}"


def _wrapped(text, columns, style, indent="  "):
    """A sentence, wrapped at word boundaries. The terminal must never break it mid-word."""
    return [(indent + line, style) for line in textwrap.wrap(text, max(columns - len(indent), 10))]


def body(agent, recent, reply, columns, glyphs=None):
    """[(text, style)] where style is plain | warn | dim | head | blank. Long text is wrapped in
    full, never cut: the owner must be able to read all of it."""
    glyphs = glyphs or menu_style.UNICODE
    out = []
    if agent.auth and agent.pane_id:
        out.append(("NEEDS LOGIN", "head"))
        out += _wrapped(f"This agent is waiting for a credential: {agent.auth}.", columns, "warn")
        out += _wrapped("Open its pane and type it there. The menu never carries or records a secret.", columns, "plain")
    elif agent.auth:
        out.append(("NEEDS LOGIN", "head"))
        out += _wrapped(f"Its latest answer reports a login problem: {agent.auth}.", columns, "warn")
        out += _wrapped("Log in from your own terminal, then send it an instruction to try again. The menu never "
                        "carries or records a secret.", columns, "plain")
    elif agent.needs_owner:
        out.append((f"NEEDS YOU ({agent.needs_owner})", "head"))
        if agent.pane_id:
            where = "Open its pane to read and answer it."
        elif agent.kind == "codex":
            where = f"It has no pane: open it in a terminal with `codex resume {agent.session_id}` to read and answer it."
        else:
            where = "It runs in no pane: attach it to read and answer it."
        out += _wrapped(f"This agent is waiting for an answer from you. {where}", columns, "warn")
    else:
        out.append((f"STATE      {LABELS[menu_render.status(agent)]}", "head"))
    if agent.children:
        out.append(("", "blank"))
        for index, child in enumerate(agent.children):
            lead = "CHILDREN   " if index == 0 else "           "
            state = "retired" if child.retired else LABELS[menu_render.status(child)]
            out.append((menu_render.fit(f"{lead}{child.name}  {child.access}  {menu_render.mark(child, glyphs)} {state}",
                                        columns, glyphs["ellipsis"]), "plain"))
    if recent:
        out.append(("", "blank"))
        for index, row in enumerate(recent):
            receipt = f"  receipt {glyphs['idle']}" if row["receipt"] else ("  no receipt yet" if row["direction"] == "→" else "")
            lead = "RECENT     " if index == 0 else "           "
            text = f"{lead}{row['direction']} {row['time']} \"{row['text']}\""
            out.append((menu_render.fit(text, columns - menu_render.width(receipt), glyphs["ellipsis"]) + receipt, "plain"))
    if reply:
        out += [("", "blank"), ("LATEST ANSWER", "head")]
        for paragraph in reply.splitlines():
            for line in textwrap.wrap(paragraph, max(columns - 2, 10)) or [""]:
                out.append(("  " + line, "plain"))
    if not can_instruct(agent):
        if look_only():
            why = "look-only mode: no instruction line and no Retire"
        elif agent.retired:
            why = "this agent is retired"
        elif agent.auth:
            why = "instruction line is off while a credential prompt is on screen"
        elif agent.needs_owner and agent.pane_id:
            why = "instruction line is off while it waits for your answer: typed text would answer its prompt"
        else:
            why = "a Claude session with no pane takes input only in its own terminal: use Attach"
        out.append(("", "blank"))
        out += _wrapped(f"({why})", columns, "dim")
    return out
