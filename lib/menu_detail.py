"""Text of the popup for one agent. Pure: takes data, returns lines and the buttons that apply."""
import os
import re
import textwrap

import menu_render
import menu_style

SHORT_ID = re.compile(r"[0-9a-f]{6,32}")
LABELS = {"auth": "needs login", "needs": "needs you", "unanswered": "unanswered",
          "working": "working", "idle": "idle", "stopped": "stopped"}


def short_path(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path and path.startswith(home) else (path or "")


def look_only(environ=None):
    """AGENT_MENU_LOOK_ONLY=1: show everything, but offer nothing that types into an agent or retires one."""
    return bool((os.environ if environ is None else environ).get("AGENT_MENU_LOOK_ONLY"))


def can_quit(agent):
    """Quit stops or archives an agent so it can be resumed. Nothing is deleted.
    A Claude session spawn-peer made that stopped without being retired is still offered Quit: it only
    records the retirement, which moves the agent from the tree to the QUIT group. An archived Codex
    thread cannot be told from a live one by `retire-peer`, so that one is resumed first."""
    if agent.quit:
        return agent.kind == "claude" and agent.spawned and not agent.retired
    if agent.retired or agent.pane_id or agent.maybe_in_pane:
        return False                              # one running in a pane is quit there, not from here
    if agent.kind == "codex":
        return True
    return agent.background and bool(SHORT_ID.fullmatch(agent.short_id or ""))


def quit_note(agent):
    """Why Quit is not offered for a live agent, or "" when it is."""
    if agent.quit or agent.retired or can_quit(agent):
        return ""
    if agent.pane_id:
        return "it runs in a pane, so quit it there"
    if agent.maybe_in_pane:
        return "a pane may be showing it, so quit it there"
    return "an interactive session ends only when its own terminal is closed"


def can_resume(agent):
    return agent.quit and (agent.kind == "codex" or bool(SHORT_ID.fullmatch(agent.short_id or "")))


def quit_effect(agent):
    """What Quit does to this agent, in the owner's words."""
    if agent.quit:
        return "record it as retired (it has already stopped)"
    if agent.kind == "codex":
        return "archive it"
    return "stop it (its conversation is kept)"


def confirm_text(agent):
    busy = " It is working right now." if agent.state == "working" else ""
    return f"Quit {agent.name}? This will {quit_effect(agent)}; Resume brings it back.{busy}"


def buttons(agent):
    """Only actions that can work for this agent are offered."""
    out = []
    if agent.quit:
        out = ["Resume"] if can_resume(agent) else []
        return out + (["Quit agent"] if can_quit(agent) and not look_only() else [])
    if agent.retired:
        return []
    if agent.pane_id:
        out.append("Open pane")
    elif agent.kind == "claude" and agent.background and agent.short_id:
        out.append("Attach")
    if can_instruct(agent):
        out.append("Send")
    if can_quit(agent) and not look_only():
        out.append("Quit agent")
    return out


def button_labels(offered, columns):
    """Labels with their key, or bare names when the row would not fit."""
    keys = {"Open pane": "o", "Attach": "t", "Quit agent": "x", "Resume": "r"}
    full = [f"[ {b} ]" if b == "Send" else f"[ {b} ({keys[b]}) ]" for b in offered]
    if sum(menu_render.width(label) + 2 for label in full) <= columns:
        return full
    return [f"[{b}]" for b in offered]


def can_instruct(agent):
    """An instruction typed into a pane lands on whatever the pane shows, so typing stops while it
    shows a credential prompt or waits on the owner: there Enter or a letter would answer the prompt.
    A Codex agent with no pane gets the instruction through `codex queue`, which types into nothing,
    so a login problem its latest answer reports leaves the instruction line on for the retry.
    A quit agent takes no instruction until it is resumed."""
    if agent.retired or agent.quit or look_only():
        return False
    if agent.pane_id:
        return not (agent.auth or agent.needs_owner)
    return agent.kind == "codex"


def title(agent, parent_name):
    origin = f"spawned by: {parent_name}" if parent_name else ("spawned" if agent.spawned else "started by you")
    if agent.quit:
        origin += " · quit"
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
    if agent.quit:
        out.append(("QUIT", "head"))
        how = ("It is archived. Resume unarchives it and opens it in a new window." if agent.kind == "codex"
               else "It is stopped and its conversation was kept. Resume attaches it in a new window.")
        if can_quit(agent):
            how += " Nobody retired it yet, so it keeps its place in the tree. Quit records it as retired."
        out += _wrapped(how, columns, "plain")
    elif agent.auth and agent.pane_id:
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
        if agent.quit:
            why = "it is quit: Resume brings it back"
        elif look_only():
            why = "look-only mode: no instruction line and no Quit"
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
    note = quit_note(agent)
    if note and not look_only():
        out += _wrapped(f"(no Quit here: {note})", columns, "dim")
    return out
