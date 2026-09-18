"""The local logs hold message summaries, session names and your own instructions, so they are private
to their user: files 0600 in folders 0700. A log file left readable by others is tightened when it is
next written. A folder is tightened only when it is this repository's own log/ folder or this module
created it: a log you point somewhere else, such as your home folder, never changes that folder."""
import fcntl
import os
import stat

REPO_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "log")


def _tighten(path, wanted):
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return
    if st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) & ~wanted:
        os.chmod(path, stat.S_IMODE(st.st_mode) & wanted)


def private_folder(folder):
    """Create `folder` 0700 if it is missing. The repository's own log folders are tightened if open."""
    folder = folder or "."
    if not os.path.isdir(folder):
        os.makedirs(folder, mode=0o700, exist_ok=True)
        _tighten(folder, 0o700)
    elif os.path.realpath(folder).startswith(REPO_LOG + os.sep) or os.path.realpath(folder) == REPO_LOG:
        _tighten(folder, 0o700)
    return folder


def append_line(path, line):
    """Append one line to a private log under an exclusive lock, then fsync. Raises OSError."""
    private_folder(os.path.dirname(path))
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    # backslashreplace: a lone surrogate, as a JavaScript writer leaves when it cuts an emoji in half,
    # becomes a \udXXX escape, which inside a JSON string is still valid JSON.
    with os.fdopen(fd, "a", encoding="utf-8", errors="backslashreplace") as fh:
        _tighten(path, 0o600)
        fcntl.flock(fh, fcntl.LOCK_EX)
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            fh.write("\n")                  # a writer died mid-line: do not join its line
        fh.write(line if line.endswith("\n") else line + "\n")
        fh.flush()
        os.fsync(fh.fileno())
