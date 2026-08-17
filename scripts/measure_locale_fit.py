"""Report board strings a translation makes too wide for the e-paper panel.

Every string in ``i18n/locale/<code>.json`` is run through the widget that draws
it, in the splash's geometry (the narrowest column the board uses: 120px inside
a 128px panel), and compared against the English source it replaces. Only
strings that clip or wrap in the translation while the English fits are
reported, so the output is the cost of translating rather than a list of every
long line.

Two failures are worth acting on:

- **clipped**: a single word is wider than the column. Wrapping cannot break
  inside a word, so the text is cut off on hardware. Split it with an explicit
  ``\\n``, or choose a shorter word.
- **taller**: the wrapped text needs more lines than the band has, so the last
  line falls off the bottom.

Run as ``python scripts/measure_locale_fit.py nl`` from the repository root.
Written for the Dutch pass; the German one used ad-hoc versions of this.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from universalchess.epaper.text import Justify, TextWidget  # noqa: E402

# The splash message band: the full panel less a 4px margin each side, over the
# height left below the "UNIVERSAL" logo, drawn at 18pt with wrapping on.
COLUMN_WIDTH = 120
BAND_HEIGHT = 110
FONT_SIZE = 18

# Placeholders stand in for runtime values whose length no translator controls,
# so they are measured as a short sample rather than as their brace text.
PLACEHOLDER_SAMPLE = "8888"
LOCALE_DIR = ROOT / "src" / "universalchess" / "i18n" / "locale"


def _sample(text: str) -> str:
    """Return ``text`` with every ``{placeholder}`` replaced by a short value."""
    out = []
    depth = 0
    for char in text:
        if char == "{":
            depth += 1
            if depth == 1:
                out.append(PLACEHOLDER_SAMPLE)
        elif char == "}":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(char)
    return "".join(out)


def _widget(text: str) -> TextWidget:
    return TextWidget(
        0, 0, COLUMN_WIDTH, BAND_HEIGHT, lambda *args, **kwargs: None,
        text=text, font_size=FONT_SIZE, justify=Justify.CENTER, wrapText=True,
    )


def _verdict(text: str) -> str:
    """Return "" when the text fits, else the way it fails."""
    widget = _widget(_sample(text))
    lines = widget._wrap_text(widget.text, COLUMN_WIDTH, widget._font)
    if any(widget._text_len(line, widget._font) > COLUMN_WIDTH for line in lines if line):
        return "clipped"
    if len(lines) * (FONT_SIZE + 2) > BAND_HEIGHT:
        return "taller than the band"
    return ""


def main(code: str) -> int:
    translated = json.loads((LOCALE_DIR / f"{code}.json").read_text("utf-8"))
    english = json.loads((LOCALE_DIR / "en.json").read_text("utf-8"))

    regressions = []
    for key, text in sorted(translated.items()):
        if key == "_comment":
            continue
        theirs = _verdict(text)
        if theirs and not _verdict(english.get(key, "")):
            regressions.append((key, theirs, text, english.get(key, "")))

    for key, how, text, source in regressions:
        print(f"{how:>21}  {key}")
        print(f"                       {code}: {text!r}")
        print(f"                       en: {source!r}")
    print(f"\n{len(regressions)} of {len(translated) - 1} strings fit in English but not in {code!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "nl"))
