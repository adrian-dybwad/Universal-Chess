# Logging regression test for _prompt_game_text
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Guards a CodeQL "clear-text logging of sensitive information" fix.
# The regression is guarded structurally by parsing the source, because what is
# being asserted is a property of the code rather than of a result: the log calls in
# _prompt_game_text must not interpolate the settings `field` (which can be the
# namespaced coach_api_key key, a CodeQL sensitive source) and must log the
# constant UI `title` instead. The entered value is a separate variable
# (`result`) and is never logged.
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

import ast

from universalchess.tests.app_source import function_node

_LOG_METHODS = {"info", "debug", "warning", "error", "exception", "critical"}


def _prompt_game_text_node() -> ast.FunctionDef:
    """Return the parsed AST for the application's _prompt_game_text."""
    return function_node("_prompt_game_text")


def _log_calls(func: ast.FunctionDef):
    """Yield every log.<method>(...) call inside the given function."""
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if (
            isinstance(callee, ast.Attribute)
            and callee.attr in _LOG_METHODS
            and isinstance(callee.value, ast.Name)
            and callee.value.id == "log"
        ):
            yield node


def _references_name(call: ast.Call, name: str) -> bool:
    """True if any argument of the call references the local variable `name`."""
    return any(
        isinstance(sub, ast.Name) and sub.id == name
        for arg in call.args
        for sub in ast.walk(arg)
    )


def test_log_calls_do_not_leak_field_key():
    """No log call in _prompt_game_text may interpolate the `field` key.

    Why this test exists: `field` can be namespaced_key(API_KEY_BASE, ...), which
    CodeQL treats as a sensitive source (py/clear-text-logging). Logging it
    reopened alerts #190/#191/#192 even though the value is only a config key.
    How the regression manifests: if a log line interpolates `field` again, one of
    these calls references the Name `field` and this assertion fails.
    """
    calls = list(_log_calls(_prompt_game_text_node()))
    assert calls, "expected _prompt_game_text to contain log calls"
    offenders = [ast.dump(c) for c in calls if _references_name(c, "field")]
    assert not offenders, f"log calls must not reference `field`: {offenders}"


def test_logs_the_constant_title():
    """At least one log call logs the constant UI `title` (the safe descriptor).

    Why this test exists: the fix keeps useful logging by substituting the
    request-independent `title` literal for the tainted `field`. This pins that
    the informative log was not simply deleted.
    How the regression manifests: if `title` logging were removed, no log call
    references the Name `title` and this assertion fails.
    """
    calls = list(_log_calls(_prompt_game_text_node()))
    assert any(_references_name(c, "title") for c in calls), (
        "expected a log call to reference the constant `title`"
    )


def test_entered_value_is_never_logged():
    """The entered value (`result`, possibly a credential) is never logged.

    Why this test exists: the sensitive datum is the value the user types, not the
    field key. This guards that no log line ever interpolates it.
    How the regression manifests: logging `result` would reference the Name
    `result` in a log call and this assertion fails.
    """
    calls = list(_log_calls(_prompt_game_text_node()))
    offenders = [ast.dump(c) for c in calls if _references_name(c, "result")]
    assert not offenders, f"log calls must not reference `result`: {offenders}"
