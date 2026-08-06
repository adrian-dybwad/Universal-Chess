"""Talking to the person running the tool.

Output and prompts live here rather than in either entry point because both
need them, and the two copies that existed before this module had already
drifted into being maintained separately. A single definition also means the
prompt wording and the default answer cannot diverge between the card half of
the tool and the host half.

Kept deliberately small: anything that decides *what* to ask belongs with the
logic doing the asking, not here.
"""

from __future__ import annotations


def emit(message: str = "") -> None:
    """Write a line of user-facing output."""
    print(message)  # noqa: T201 -- stdout is this tool's interface, not logging


def confirm(prompt: str) -> bool:
    """Ask the user to approve an action, defaulting to no.

    The negative default is for prompts that write to a card or restart a
    daemon: a stray newline, or a run with no terminal behind it, must not be
    read as consent to modify something.
    """
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def confirm_default_yes(prompt: str) -> bool:
    """Ask the user to approve an action, defaulting to yes.

    For prompts where continuing is harmless and is what almost everyone wants,
    such as carrying on into a read-only check.

    EOF still answers no. Reaching EOF means nobody is there to answer, and the
    callers that default to yes are the ones that would otherwise go on to wait
    on a person or a piece of hardware that is not coming.
    """
    try:
        answer = input(f"{prompt} [Y/n] ").strip().lower()
    except EOFError:
        return False
    return answer in {"", "y", "yes"}
