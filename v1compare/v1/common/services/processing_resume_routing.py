from __future__ import annotations

import re


FRESH_PROCESSING_MARKERS = (
    "brand new run",
    "brand-new run",
    "new device",
    "new project",
    "new experiment",
    "start fresh",
    "fresh start",
    "fresh run",
    "do not resume",
    "don't resume",
    "no resume",
    "nothing to do with run",
    "ignore the previous experiment",
    "ignore previous experiment",
    "ignore the prior experiment",
    "ignore prior experiment",
)

PROCESSING_RESUME_MARKERS = (
    "resume_processing_checkpoint",
    "resume the previous",
    "resume previous",
    "resume the interrupted",
    "continue the previous",
    "continue previous",
    "re-arm the checkpoint",
    "rearm the checkpoint",
)


def is_explicit_fresh_processing_request(text: str) -> bool:
    normalized = " ".join(str(text or "").casefold().split())
    return any(marker in normalized for marker in FRESH_PROCESSING_MARKERS)


def is_explicit_processing_resume_request(text: str) -> bool:
    normalized = " ".join(str(text or "").casefold().split())
    return any(marker in normalized for marker in PROCESSING_RESUME_MARKERS)


def last_user_message_from_transcript(transcript: str) -> str:
    latest: list[str] = []
    current: list[str] | None = None
    for line in str(transcript or "").splitlines():
        if line.startswith("User: "):
            current = [line.removeprefix("User: ")]
            latest = current
        elif line.startswith("Assistant: "):
            current = None
        elif current is not None:
            current.append(line)
    return "\n".join(latest).strip()


def fresh_processing_run_requested(prompt: str, history_transcript: str) -> bool:
    """Return true when checkpoint matching must yield to a fresh-run request."""
    # An explicit current resume request always wins over an earlier fresh-run note.
    if is_explicit_processing_resume_request(prompt):
        return False
    if is_explicit_fresh_processing_request(prompt):
        return True
    return is_explicit_fresh_processing_request(
        last_user_message_from_transcript(history_transcript)
    )


def implicit_processing_resume_allowed(
    current_session_key: str | None,
    checkpoint_session_key: str | None,
) -> bool:
    """Return whether prompt matching may implicitly resume a checkpoint.

    A repeated request is only a continuation signal inside the same UI
    session.  Identical wording in a new chat is not sufficient evidence that
    the user wants to adopt an older physical run.  Cross-session recovery must
    use an explicit checkpoint path, which keeps the intended run unambiguous.
    """
    current = str(current_session_key or "").strip()
    checkpoint = str(checkpoint_session_key or "").strip()
    return bool(current and checkpoint and current == checkpoint)


def requested_processing_checkpoint_path(text: str) -> str:
    """Return an explicitly named processing checkpoint path, or an empty string.

    Physical workflows are never selected from prompt similarity alone.  The
    caller must supply the durable checkpoint filename so a stale run cannot
    hijack a new request that happens to repeat earlier wording.
    """
    raw = str(text or "")
    quoted = re.search(
        r"['\"](?P<path>[^'\"\r\n]*processing_resume_checkpoint\.json)['\"]",
        raw,
        flags=re.IGNORECASE,
    )
    if quoted:
        return quoted.group("path").strip()
    unquoted = re.search(
        r"(?P<path>(?:[A-Za-z]:[\\/]|/|\.{0,2}[\\/])[^\s'\"]*processing_resume_checkpoint\.json)",
        raw,
        flags=re.IGNORECASE,
    )
    return unquoted.group("path").strip() if unquoted else ""
