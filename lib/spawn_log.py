"""The record of agents created by bin/spawn-peer. retire-peer acts only on these."""
import datetime
import fcntl
import json
import os

# What a spawn record's `access` and `approvals` mean to the Codex daemon. spawn-peer starts a thread
# with these, and send-to-codex wakes an unloaded one with the same, so a wake never widens a spawn.
CODEX_SANDBOX = {"read-only": "read-only", "write": "workspace-write"}
CODEX_APPROVALS = {
    "auto-review": {"approvalPolicy": "on-request", "approvalsReviewer": "auto_review"},
    "never": {"approvalPolicy": "never"},
}


def path():
    here = os.path.dirname(os.path.realpath(__file__))
    default = os.path.join(os.path.dirname(here), "log", "spawned.jsonl")
    return os.environ.get("AGENT_COMMS_SPAWN_LOG") or default


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_writable():
    """Raise OSError now if a later append would fail, before anything is created."""
    target = path()
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "a", encoding="utf-8"):
        pass


def append(record):
    target = path()
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def events_for(agent_id):
    try:
        # A line torn inside a multibyte character must not hide every other line.
        with open(path(), encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue  # a torn line must not block every later retire
        if isinstance(record, dict) and record.get("id") == agent_id:
            records.append(record)
    return records
