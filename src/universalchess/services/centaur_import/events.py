"""Event-log reporting for the Original Centaur SD-import pipeline.

Users who could not import their original Centaur had nothing to show for it.
The privileged mount/stage/umount and armhf helpers all ran with
``capture_output=True`` and their output was discarded once the exit code was
checked, so a missing sudoers grant, an unreadable app directory on the SD card
and a stale apt index all collapsed into one fixed sentence. Anything that was
not a helper -- a truncated upload, a full disk -- reached only the root
logger's ``~/debug.log``, which is truncated on every boot and so was usually
gone by the time the failure was reported.

This module is the one place the import records what it did and why it stopped,
into the persistent JSON-lines event log the Settings > System viewer reads.

Detail recorded here is deliberately richer than the ``CentaurImportError`` text
the same failure produces: that message is returned over HTTP and must stay
free of paths and exception text (CWE-209), while the event log is auth-gated
and is where argv, exit codes and helper output belong.
"""

import shlex
import shutil

from universalchess.services.event_log import log_event

# Category token stamped on every record the import writes. The Settings viewer
# maps it to a translated badge, so it is shared here rather than repeated at
# each call site where it could drift and lose its label.
EVENT_CATEGORY = "centaur_import"

# Upper bound on captured helper output carried into a single record. An apt run
# emits thousands of progress lines; the log rotates at ~1 MB and the viewer
# renders one row per record, so an unbounded message would both evict the rest
# of the board's history and break the layout.
_MAX_OUTPUT_CHARS = 800


def log_import_event(message: str, *, level: str = "info", duration_ms=None) -> None:
    """Append one import record. Best-effort: never raises into the pipeline."""
    log_event(EVENT_CATEGORY, message, level=level, duration_ms=duration_ms)


def _stream_text(value) -> str:
    """Decode one captured subprocess stream to text.

    Streams arrive as bytes because the helpers are run without ``text=True``,
    but an injected runner (and ``TimeoutExpired``) can hand back ``str`` or
    ``None``. Undecodable bytes are replaced rather than raising: losing a
    character is better than losing the whole diagnostic.
    """
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


def describe_output(stdout=None, stderr=None) -> str:
    """Render captured subprocess output as one bounded, single-line string.

    Both streams are kept and concatenated in emission order: which one carries
    the diagnosis varies by helper (apt reports progress on stdout and the
    reason on stderr; the mount helper uses stderr alone). Whitespace is
    collapsed because each event is one JSON-lines record shown on one row, and
    only the tail is retained because apt and the mount helper print the reason
    for a failure last, so truncating the head preserves it.

    Returns ``""`` when nothing was captured, so callers can say "no output"
    rather than emitting an empty detail that reads like a missing field.
    """
    parts = [_stream_text(stdout), _stream_text(stderr)]
    combined = " ".join(" ".join(part.split()) for part in parts if part.strip())
    if len(combined) <= _MAX_OUTPUT_CHARS:
        return combined
    return "..." + combined[-_MAX_OUTPUT_CHARS:]


def format_command(cmd) -> str:
    """Render an argv list as a copy-pasteable shell command.

    Quoting each token keeps a path containing spaces readable as one argument,
    so the recorded line can be re-run verbatim on the board to reproduce the
    failure.
    """
    return " ".join(shlex.quote(str(part)) for part in cmd)


def describe_free_space(directory) -> str:
    """Return a human-readable free-space note for ``directory``, or ``""``.

    Included with the failures of the two steps that write gigabytes (the image
    decompression and the copy into CENTAUR_HOME) because a full card is a
    common cause there and is otherwise indistinguishable from a permission or
    corruption fault. Returns ``""`` when the directory cannot be measured --
    an unmeasurable path is not worth failing a diagnostic write over.
    """
    if directory is None:
        return ""
    try:
        usage = shutil.disk_usage(str(directory))
    except OSError:
        return ""
    return f"free space: {format_bytes(usage.free)}"


def format_bytes(count: int) -> str:
    """Render a byte count as a short binary-unit size (e.g. ``203.4 MB``)."""
    if count < 1024:
        return f"{count} B"
    size = count / 1024
    for unit in ("KB", "MB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def log_command_failure(
    step: str,
    cmd,
    *,
    returncode=None,
    stdout=None,
    stderr=None,
    reason: str = "",
) -> None:
    """Record a failed import subprocess with everything needed to diagnose it.

    ``step`` names the pipeline phase ("Image mount", "32-bit armhf support
    install"); ``reason`` describes how it failed when there is no exit code --
    a timeout, or a helper that could not be started at all. Those two are kept
    distinct from a non-zero exit because they have different fixes: a missing
    sudoers grant is a deployment fault, a wedged apt is not.
    """
    outcome = reason if reason else f"exit code {returncode}"
    detail = describe_output(stdout, stderr) or "no output captured"
    log_import_event(
        f"{step} failed ({outcome}): {format_command(cmd)} -- {detail}",
        level="error",
    )


def log_step_failure(step: str, exc: BaseException, *, free_space_dir=None) -> None:
    """Record a failed non-subprocess import step with its exception detail.

    The exception type is named alongside its text because the type is what
    separates a corrupt upload (``BadGzipFile``) from a truncated one
    (``EOFError``) from a full disk (``OSError``), and the three are reported to
    the user with the same step-level message.
    """
    space = describe_free_space(free_space_dir)
    suffix = f" ({space})" if space else ""
    log_import_event(
        f"{step} failed: {type(exc).__name__}: {exc}{suffix}",
        level="error",
    )
