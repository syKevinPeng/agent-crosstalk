"""Shared pieces for the agent-menu source readers."""
import json
import os


class SourceError(Exception):
    """A source could not be read. The menu shows `?` for its rows instead of stale marks."""


def clean(text):
    """Text chosen by another agent (a session name, a folder, a message line) is untrusted. Control
    characters, escape sequences, newlines, tabs and invisible format characters such as the
    right-to-left override become `?` before the text reaches a screen, stdout or a log."""
    if not isinstance(text, str):
        return ""
    # str.isprintable() is False for control, format (Cf, which holds the right-to-left override and
    # the zero-width joiner), separator and unassigned characters. A plain space stays.
    return "".join(ch if ch.isprintable() else "?" for ch in text)


def read_jsonl(path):
    """Every JSON object in a log file. Torn or foreign lines are skipped, never fatal."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise SourceError(f"{os.path.basename(path)}: {exc.strerror}") from None
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            # Any agent can append a line. Keep only plain values, so a well-formed line with a list
            # or a number where text belongs cannot crash every reader from then on.
            records.append({k: v for k, v in record.items() if isinstance(v, (str, bool)) or v is None})
    return records
