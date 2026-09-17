"""Spot a screen or an output that waits on a credential. Returns a short label, never the text:
a captured auth screen can hold a device code or a one-time sign-in URL."""
import re

# Only the last lines of a pane count: an old "Password:" further up is history, not a prompt.
# Fifteen, because Claude Code and Codex draw four to six lines of their own under the prompt.
TAIL_LINES = 15
# What a CLI agent draws in front of a command's output: box edges, tree marks, quote marks.
CHROME = r"^[\s│|⎿└├>•·]*"

SCREEN_PATTERNS = [
    ("ssh key passphrase", r"Enter passphrase for key"),
    ("sudo password", r"\[sudo\] password for \S+:\s*$"),
    ("password", r"(?i)^(\S+@\S+'s |\(current\) unix |vault |new )?password( for .{1,80})?:\s*$"),
    ("username", r"(?i)^username for .{1,80}:\s*$"),
    ("credential", r"(?i)^(enter )?(your |the )?(passphrase|passcode|pin|otp|token|mfa code|security code)"
                   r"( for [^:]{1,40})?:?\s*$"),
    ("second factor", r"(?i)(duo two-factor|passcode or option|verification code:\s*$|one-time (code|password):\s*$)"),
    ("device sign-in", r"(?i)(enter the code|one-time code|device code)\b.*\b[A-Z0-9]{4}-[A-Z0-9]{4}\b"),
    ("browser sign-in", r"(?i)(open|visit) (this|the following) (url|link).*(sign in|log ?in|authenti)"),
    # Anchored to the start of the line: an agent that merely mentions a login command is not stuck on one.
    ("claude login", r"(?i)^(please run /login|run /login to|not logged in\b.*/login)"),
    ("codex login", r"(?i)^(not signed in\b.*codex login|please run `?codex login`?|sign in with chatgpt to continue)"),
    ("github login", r"(?i)^(to get started.*|please run:?\s*)gh auth login"),
    ("gcloud login", r"(?i)^(please run:?\s*)\$?\s*gcloud auth login"),
]

OUTPUT_PATTERNS = [
    ("ssh key rejected", r"(?i)permission denied \(publickey"),
    ("not logged in", r"(?i)\bnot logged in\b"),
    ("authentication required", r"(?i)authentication (is )?required"),
    ("http 401", r"(?i)\b401\b.*unauthori[sz]ed|unauthori[sz]ed.*\b401\b"),
    ("token expired", r"(?i)(token|credentials?) (has |have )?expired"),
]


def detect_screen(text):
    """Label of the auth prompt at the bottom of a pane, or None."""
    tail = [re.sub(CHROME, "", line).rstrip() for line in (text or "").splitlines() if line.strip()][-TAIL_LINES:]
    for label, pattern in SCREEN_PATTERNS:
        if any(re.search(pattern, line) for line in tail):
            return label
    return None


def detect_output(text):
    """Label of an auth failure in an agent's answer, or None. For agents with no pane."""
    for label, pattern in OUTPUT_PATTERNS:
        if re.search(pattern, text or ""):
            return label
    return None
