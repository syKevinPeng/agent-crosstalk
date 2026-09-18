"""How much of each model's rate limit is used.

Two sources, both read-only and both free:

* Codex answers `account/rateLimits/read` on its local daemon.
* Claude Code answers `claude -p --output-format json "/usage"`. That runs no model: the call
  reports zero tokens and zero cost. Its text is written for a person, so every line is matched on
  its own and a line that does not match is dropped rather than guessed at.

The Claude call runs with no hooks, MCP servers or folder settings and saves no transcript, in a
private folder of its own. Nothing here reads a credential: both tools use your existing login.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import zoneinfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from codex_ws import CodexError, CodexWS, sock_path  # noqa: E402

import private_log  # noqa: E402
from .base import SourceAbsent, SourceError, usage_folder  # noqa: E402

# "Current week (Fable): 16% used · resets Sep 24, 1pm (Europe/Berlin)"
USAGE_LINE = re.compile(r"^Current (session|week)(?: \(([^)]{1,40})\))?: (\d{1,3})% used(?: . resets (.{1,60}))?$")
RESET_ZONE = re.compile(r"^(.{1,40}?)\s*\(([A-Za-z_/+-]{1,40})\)\s*$")


def usage_cwd():
    """Where the Claude call runs: a private folder, which the listing leaves out as the menu's own call."""
    return private_log.private_folder(usage_folder())


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


MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                       "nov", "dec"), start=1)}
ENGLISH_TIME = re.compile(r"^([A-Za-z]{3})[a-z]* (\d{1,2}), (\d{1,2})(?::(\d{2}))?\s*([AaPp][Mm])?$")


def _english_time(text):
    """'Sep 24, 1pm' or 'Sep 24, 13:00' -> a naive datetime in 1900, or None. By hand, not with
    strptime, whose month and am/pm names follow the locale: under zh_CN every reset time was lost."""
    found = ENGLISH_TIME.match(text.strip())
    if not found or found.group(1).lower() not in MONTHS:
        return None
    month, day, hour, minute, half = found.groups()
    hour, minute = int(hour), int(minute or 0)
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half.lower() == "pm" else 0)
    try:
        return datetime.datetime(1900, MONTHS[month.lower()], int(day), hour, minute)
    except ValueError:
        return None


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
    stamp = _english_time(when)
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
    except FileNotFoundError:
        raise SourceAbsent("claude: not installed") from None
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
    if not os.path.exists(sock_path()):
        if not shutil.which(os.environ.get("CODEX_BIN") or "codex"):
            raise SourceAbsent("codex: not installed")
        raise SourceError("codex: daemon not running")
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
        except SourceAbsent:
            pass                                      # that CLI is not installed: nothing to report
        except SourceError as exc:
            errors.append(str(exc))
        except Exception as exc:                      # a limits block must never end the menu
            errors.append(f"{reader.__name__.split('_')[0]}: {type(exc).__name__}")
    return limits, errors
