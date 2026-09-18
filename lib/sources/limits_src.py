"""How much of each model's rate limit is used.

Two sources, both read-only and both free:

* Codex answers `account/rateLimits/read` on its local daemon.
* Claude Code answers `claude -p --output-format json "/usage"`. That runs no model: the call
  reports zero tokens and zero cost. Its text is written for a person, so every line is matched on
  its own and a line that does not match is dropped rather than guessed at.

The Claude call leaves a transcript file behind, so it runs in a folder of our own and the menu
asks for it rarely. Nothing here reads a credential: both tools use the owner's existing login.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import zoneinfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from codex_ws import CodexError, CodexWS  # noqa: E402

import private_log  # noqa: E402
from .base import SourceError, usage_folder  # noqa: E402

# "Current week (Fable): 16% used · resets Sep 24, 1pm (Europe/Berlin)"
USAGE_LINE = re.compile(r"^Current (session|week)(?: \(([^)]{1,40})\))?: (\d{1,3})% used(?: . resets (.{1,60}))?$")
RESET_ZONE = re.compile(r"^(.{1,40}?)\s*\(([A-Za-z_/+-]{1,40})\)\s*$")


def usage_cwd():
    """Where the Claude call runs, so its transcripts collect in one corner of their own."""
    return private_log.private_folder(usage_folder())


def short_delta(seconds):
    """Seconds until a reset, as something that fits a narrow column."""
    if seconds is None:
        return ""
    if seconds <= 0:
        return "now"
    for size, suffix in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{int(seconds // size)}{suffix}"
    return "now"


def window_name(minutes):
    if not isinstance(minutes, (int, float)) or minutes <= 0:
        return ""
    if minutes % 10080 == 0:
        return "week" if minutes == 10080 else f"{int(minutes // 10080)}wk"
    if minutes % 1440 == 0:
        return f"{int(minutes // 1440)}d"
    if minutes % 60 == 0:
        return f"{int(minutes // 60)}h"
    return f"{int(minutes)}m"


def parse_reset(text, now=None):
    """'Sep 24, 1pm (Europe/Berlin)' -> seconds from now, or None when it cannot be read."""
    found = RESET_ZONE.match(text or "")
    if not found:
        return None
    when, zone = found.groups()
    try:
        tz = zoneinfo.ZoneInfo(zone)
    except Exception:
        return None
    stamp = None
    for pattern in ("%b %d, %I:%M%p", "%b %d, %I%p", "%b %d, %H:%M"):
        try:
            stamp = datetime.datetime.strptime(when.strip().upper().replace("AM", "AM").replace("PM", "PM"), pattern)
            break
        except ValueError:
            continue
    if stamp is None:
        return None
    here = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(tz)
    guess = stamp.replace(year=here.year, tzinfo=tz)
    if guess < here - datetime.timedelta(days=180):        # the reset is in January and today is December
        guess = guess.replace(year=here.year + 1)
    return (guess - here).total_seconds()


def claude_limits(timeout=25, now=None):
    """[{source, window, model, percent, resets_in}] from Claude Code's own usage command."""
    binary = os.environ.get("CLAUDE_BIN") or "claude"
    try:
        # --safe-mode: no hooks, MCP servers, plugins or CLAUDE.md. --setting-sources user: no settings
        # from the folder it runs in. --no-session-persistence: no transcript left behind.
        done = subprocess.run([binary, "-p", "--output-format", "json", "--safe-mode", "--no-session-persistence",
                               "--setting-sources", "user", "/usage"],
                              cwd=usage_cwd(), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceError(f"claude: {type(exc).__name__}") from None
    if done.returncode != 0:
        raise SourceError(f"claude: exit {done.returncode}")
    try:
        text = json.loads(done.stdout or "{}").get("result")
    except ValueError:
        raise SourceError("claude: output is not JSON") from None
    if not isinstance(text, str):
        raise SourceError("claude: no usage text")
    out = []
    for line in text.splitlines():
        found = USAGE_LINE.match(line.strip())
        if not found:
            continue
        scope, label, percent, reset = found.groups()
        model = "" if not label or label.lower() in ("all models", "all") else label
        out.append({"source": "claude", "window": "session" if scope == "session" else "week",
                    "model": model, "percent": int(percent), "resets_in": parse_reset(reset, now)})
    if not out:
        raise SourceError("claude: no usage line recognised")   # the wording changed: say so, do not guess
    return out


def codex_limits(timeout=10, now=None):
    """[{source, window, model, percent, resets_in}] from the Codex daemon."""
    seconds_now = (now or datetime.datetime.now(datetime.timezone.utc)).timestamp()
    try:
        ws = CodexWS(timeout=timeout)
        try:
            answer = ws.rpc("account/rateLimits/read", {})
        finally:
            ws.close()
    except CodexError as exc:
        raise SourceError(f"codex: {exc}") from None
    by_id = answer.get("rateLimitsByLimitId")
    snapshots = by_id if isinstance(by_id, dict) and by_id else {"codex": answer.get("rateLimits")}
    out = []
    for limit_id, snapshot in sorted(snapshots.items()):
        if not isinstance(snapshot, dict):
            continue
        for slot in ("primary", "secondary"):
            window = snapshot.get(slot)
            if not isinstance(window, dict) or not isinstance(window.get("usedPercent"), (int, float)):
                continue
            resets = window.get("resetsAt")
            out.append({"source": str(limit_id)[:12], "window": window_name(window.get("windowDurationMins")),
                        "model": snapshot.get("normalModelSlug") or "",
                        "percent": int(round(window["usedPercent"])),
                        "resets_in": resets - seconds_now if isinstance(resets, (int, float)) else None})
    if not out:
        raise SourceError("codex: no rate limit window reported")
    return out


def read_all(now=None):
    """(limits, errors). One source failing never hides the other."""
    limits, errors = [], []
    for reader in (claude_limits, codex_limits):
        try:
            limits.extend(reader(now=now))
        except SourceError as exc:
            errors.append(str(exc))
        except Exception as exc:                      # a limits block must never end the menu
            errors.append(f"{reader.__name__.split('_')[0]}: {type(exc).__name__}")
    return limits, errors
