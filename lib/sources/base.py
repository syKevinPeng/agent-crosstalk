"""Shared pieces for the agent-menu source readers."""
import json
import os


class SourceError(Exception):
    """A source could not be read. The menu shows `?` for its rows instead of stale marks."""


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
            records.append(record)
    return records
