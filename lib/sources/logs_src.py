"""The local logs: who spawned whom, and which messages still wait for a receipt."""
import datetime
import os
import re

from .base import clean, read_jsonl

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


def messages_path():
    return os.environ.get("AGENT_COMMS_LOG") or os.path.join(ROOT, "log", "messages.jsonl")


def spawned_path():
    return os.environ.get("AGENT_COMMS_SPAWN_LOG") or os.path.join(ROOT, "log", "spawned.jsonl")


def normalise(label):
    """`claude/my-session [ab12cd34]` -> `my session`, so a sender label can be matched to a name."""
    label = re.sub(r"\[[^\]]*\]", "", label or "")
    label = re.sub(r"^(claude|codex)/", "", label.strip(), flags=re.I)
    return re.sub(r"[-_\s]+", " ", label).strip().lower()


def label_short_id(label):
    found = re.search(r"\[([0-9a-f]{6,})\]", label or "")
    return found.group(1) if found else None


def parse_time(text):
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def spawned():
    """One dict per agent that spawn-peer created, newest record per id, with its retire time."""
    agents = {}
    for record in read_jsonl(spawned_path()):
        agent_id = record.get("id")
        if not agent_id:
            continue
        if record.get("event") == "spawned":
            agents[agent_id] = {"id": agent_id, "kind": record.get("kind"), "name": clean(record.get("name")),
                                "cwd": clean(record.get("cwd")), "spawned_by": record.get("spawned_by") or "",
                                "access": "rw" if record.get("access") == "write" else "ro",
                                "retired_at": None}
        elif record.get("event") == "resumed" and agent_id in agents:
            agents[agent_id]["retired_at"] = None          # brought back from the agent menu: live again
        elif record.get("event") == "retired" and agent_id in agents:
            agents[agent_id]["retired_at"] = parse_time(record.get("time_utc"))
    return list(agents.values())


def _is_send(record):
    return record.get("channel") != "receipt" and record.get("msg_id") and record.get("result") != "error"


def open_messages():
    """{recipient id: count} of messages with no receipt yet. ACK lines never get one, so they are skipped."""
    records = read_jsonl(messages_path())
    received = {r.get("msg_id") for r in records if r.get("channel") == "receipt"}
    counts = {}
    for record in records:
        if not _is_send(record) or record["msg_id"] in received:
            continue
        if (record.get("first_line") or "").startswith("ACK "):
            continue
        target = record.get("thread_uuid") or record.get("thread")
        if target:
            counts[target] = counts.get(target, 0) + 1
    return counts


def recent(agent_id, agent_name, limit=4):
    """The last few messages to this agent (direction `out`) and from it (`in`), oldest first."""
    records = read_jsonl(messages_path())
    received = {r.get("msg_id") for r in records if r.get("channel") == "receipt"}
    wanted = normalise(agent_name)
    rows = []
    for record in records:
        if not _is_send(record):
            continue
        to_it = agent_id in (record.get("thread_uuid"), record.get("thread"))
        from_it = bool(wanted) and normalise(record.get("sender")) == wanted
        if to_it or from_it:
            sent = parse_time(record.get("time_utc"))          # a forged time is no text at all, only "?"
            rows.append({"direction": "out" if to_it else "in", "time": sent.astimezone().strftime("%H:%M") if sent else "?",
                         "text": clean(record.get("first_line")), "receipt": record["msg_id"] in received})
    return rows[-limit:]
