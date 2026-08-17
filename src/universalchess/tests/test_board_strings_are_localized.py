"""No board screen builds its text from an English literal.

Why this test exists
--------------------
The board's text comes from two localized sources -- the menu catalog
(``menu.json`` plus its overlays) and the string bundle (:mod:`universalchess.i18n`)
-- but nothing stopped a new row or splash from being written as a literal
instead. That is invisible in English and invisible in review, and it is how the
Lichess lobby, the shutdown countdown and half the startup splash ended up as
English screens inside a Spanish menu.

So this walks the modules that draw the board UI and flags any literal handed to
a row, a widget or a splash. A string that genuinely needs no translation is
listed in :data:`NOT_TRANSLATABLE` with the reason, which keeps the exceptions
countable and readable instead of scattered.

What it cannot see: text assembled elsewhere and passed in through a variable,
and text a caller supplies to a helper this list does not name. It catches the
shape every one of the known regressions took, not every conceivable one.
"""

import ast
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

# Modules that draw the board UI. Whole packages are walked; a file named
# directly is walked alone.
UI_SOURCES = (
    "board/board.py",
    "main.py",
    "menus",
    "epaper",
    "players/lichess",
    "managers/display.py",
    "managers/rfcomm_server.py",
    "utils/wifi.py",
    "services/engine_install_state.py",
    "services/centaur_import/import_state.py",
)

# Not board UI: a demo script, hardware panel drivers, and the standalone
# display-reset utility, none of which render menus or splashes to a user.
SKIPPED_PATHS = ("tests", "epaper_demo.py", "framework/waveshare", "reset_display.py")

# Calls whose text reaches the panel. Suffix match, so IconMenuEntry and
# _IconMenuEntry are one entry.
UI_CONSTRUCTORS = ("MenuEntry", "MenuRow", "SplashScreen", "TextWidget")

# Functions whose argument at any of the given positions is panel text. Several
# spellings take the board or a logger first, and ``show_splash`` exists as both
# a method on the display manager and a helper taking the board, so a name can
# carry its text at more than one position.
UI_TEXT_ARGUMENT = {
    "set_message": (0,),
    "set_text": (0,),
    "splash": (0,),
    "show_alert": (0,),
    "_show_shutdown_splash": (0,),
    "show_splash": (0, 1),
    "show_fullscreen_splash": (1,),
    "show_dismissible_splash": (1,),
    "_show_update_splash": (1,),
    "make_entry": (1,),
    "_show_splash": (2,),
}

# Keywords that carry panel text on a UI constructor.
UI_TEXT_KEYWORDS = ("label", "message", "text", "title", "help")

# Literals that stay as they are, with the reason. Keyed by the string itself so
# a move between files does not need an edit here.
NOT_TRANSLATABLE = {
    "UNIVERSAL": "half of the product's wordmark on the splash, set beside CHESS",
}


def _ui_modules():
    """Every board-UI module, as (relative path, parsed tree)."""
    paths = []
    for source in UI_SOURCES:
        target = PACKAGE_ROOT / source
        paths.extend(sorted(target.rglob("*.py")) if target.is_dir() else [target])
    modules = []
    for path in paths:
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        if any(skipped in relative for skipped in SKIPPED_PATHS):
            continue
        modules.append((relative, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))))
    return modules


def _callee_name(call: ast.Call) -> str:
    """The called name, whether it is a plain name or an attribute."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _literal_words(node) -> str:
    """The literal text in ``node``, or '' when it has none to translate.

    An f-string counts only for the parts written into the source: ``f"Shutdown
    in {n}"`` has words, ``f"{label}\\n{value}"`` has none.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if any(char.isalpha() for char in node.value) else ""
    if isinstance(node, ast.JoinedStr):
        literal = "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
        return literal if any(char.isalpha() for char in literal) else ""
    return ""


def _ui_literals(tree) -> list:
    """Return ``(line, text)`` for every panel string written as a literal."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node)
        candidates = []
        if any(name.endswith(suffix) for suffix in UI_CONSTRUCTORS):
            candidates.extend(
                keyword.value
                for keyword in node.keywords
                if keyword.arg in UI_TEXT_KEYWORDS
            )
        positions = UI_TEXT_ARGUMENT.get(name)
        if positions is not None:
            candidates.extend(
                node.args[index] for index in positions if len(node.args) > index
            )
            candidates.extend(
                keyword.value
                for keyword in node.keywords
                if keyword.arg in UI_TEXT_KEYWORDS
            )
        for candidate in candidates:
            words = _literal_words(candidate)
            if words:
                found.append((candidate.lineno, words))
    return found


def test_no_board_screen_is_built_from_an_english_literal():
    """Rows, widgets and splashes take their text from a localized source.

    Why: a literal renders correctly in English and silently untranslated
    everywhere else, with nothing raised or logged, so only reading catches it.
    How the regression manifests: the offending file, line and text are listed,
    to be moved into the string bundle (or the catalog, when the row already has
    a node there) -- or into NOT_TRANSLATABLE when it is a name or a glyph.
    """
    offenders = [
        f"{relative}:{line}: {text!r}"
        for relative, tree in _ui_modules()
        for line, text in _ui_literals(tree)
        if text not in NOT_TRANSLATABLE
    ]

    assert offenders == [], "board text written as English literals:\n" + "\n".join(offenders)


@pytest.mark.parametrize("literal", sorted(NOT_TRANSLATABLE))
def test_every_declared_exception_is_still_written_somewhere(literal):
    """Each exception names a literal the board still hands to a screen.

    Why: an exception for text that has been reworded, translated or deleted
    stops guarding anything, and the next string that happens to match it
    inherits the pass without anyone deciding that. How the regression
    manifests: the named literal reaches no row, widget or splash any more.
    """
    written = {
        text for _relative, tree in _ui_modules() for _line, text in _ui_literals(tree)
    }
    assert literal in written, f"{literal!r} no longer reaches a board screen"
