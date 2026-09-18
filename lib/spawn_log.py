"""The record of agents created by bin/spawn-peer. retire-peer acts only on these."""
import datetime
import grp
import json
import os
import pwd
import stat

import private_log

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
    private_log.private_folder(os.path.dirname(target))
    os.close(os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600))


def append(record):
    private_log.append_line(path(), json.dumps(record, ensure_ascii=False))


def _personal_group(gid):
    """A group only you are in: no listed members, and no other user's primary group. Most Linux systems
    give each user one, so a file your umask left group-writable is still yours alone."""
    try:
        if grp.getgrgid(gid).gr_mem:
            return False
        return all(p.pw_gid != gid or p.pw_uid == os.getuid() for p in pwd.getpwall())
    except (KeyError, OSError):
        return False


def trusted():
    """Can this record be trusted to say what spawn-peer created? Not if another user can write it."""
    try:
        st = os.stat(path())
    except OSError:
        return False
    bits = stat.S_IMODE(st.st_mode)
    if st.st_uid != os.getuid() or bits & 0o002:
        return False
    return not bits & 0o020 or _personal_group(st.st_gid)


def protected_roots():
    """Folders no spawned agent may work in: these tools' own folder and both agents' config."""
    home = os.path.expanduser("~")
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    roots = [root, os.path.join(home, ".claude"), os.path.join(home, ".codex"),
             os.environ.get("CLAUDE_CONFIG_DIR"), os.environ.get("CODEX_HOME")]
    return [os.path.realpath(p) for p in roots if p]


def is_protected(cwd):
    """True if cwd is inside a protected folder, or contains one (for example the home folder)."""
    cwd = os.path.realpath(cwd)

    def inside(a, b):
        return a == b or a.startswith(b.rstrip(os.sep) + os.sep)
    return any(inside(cwd, root) or inside(root, cwd) for root in protected_roots())


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
