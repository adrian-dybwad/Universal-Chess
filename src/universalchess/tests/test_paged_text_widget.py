"""Tests for the shared paged text view.

Long text on a 128px-wide e-paper panel does not fit, and a TextWidget draws as
many wrapped lines as its height allows and silently drops the rest. Two places
need the same answer to that -- the coach statement panel and the menu help
dialog -- so the wrap, the split into pages, the page cursor and the "Page N of
X" footer live here once.

The invariant that matters is that paging loses nothing: every wrapped line
appears on exactly one page, in order. A pagination bug does not raise, it just
drops the tail of the text, which is indistinguishable on screen from text that
was written short.

Line height is TextWidget's own (``font_size + 2``), because the pages are
measured for the widget that renders them; deriving it any other way would leave
the last line of a page half-drawn or a page short of a line.
"""

import math

import pytest
from PIL import Image

from universalchess.epaper.paged_text import NavigationHint, PagedTextWidget

WIDTH = 128
HEIGHT = 128


def _widget(**kwargs) -> PagedTextWidget:
    """A paged view the size of the board area, with a no-op update callback."""
    return PagedTextWidget(
        0, 0, WIDTH, HEIGHT, lambda *_a, **_k: None, **kwargs
    )


def _numbered_lines(count: int) -> str:
    """``count`` explicit lines, each short enough that none of them re-wraps.

    Explicit newlines make the page count depend only on lines_per_page rather
    than on font metrics, so these tests do not move when a font is swapped.
    """
    return "\n".join(f"Line{index}" for index in range(count))


def _rendered(widget: PagedTextWidget) -> Image.Image:
    """The widget rendered onto its own sprite."""
    sprite = Image.new("L", (widget.width, widget.height), 255)
    widget.render(sprite)
    return sprite


def _footer_ink(widget: PagedTextWidget) -> tuple[bool, bool]:
    """Whether the footer band has ink in its (left, right) half."""
    sprite = _rendered(widget).convert("1")
    pixels = sprite.load()
    footer_top = widget.height - widget.FOOTER_HEIGHT
    left = right = False
    for y in range(footer_top, widget.height):
        for x in range(widget.width):
            if pixels[x, y] == 0:
                if x < widget.width // 2:
                    left = True
                else:
                    right = True
    return left, right


def test_empty_text_has_no_pages_and_cannot_be_paged():
    """With no text there is nothing to page and no footer to draw.

    Why: both owners construct the view before they have anything to show. A
    "Page 1 of 0" footer on an empty panel, or a next_page that claims to have
    moved, would make the empty state look like a rendering fault.

    Failure: page_count/current_page report a page that does not exist, or the
    footer band has ink.
    """
    widget = _widget()

    assert widget.page_count == 0
    assert widget.current_page == 0
    assert widget.page_text == ""
    assert widget.footer_label == ""
    assert widget.next_page() is False
    assert widget.previous_page() is False
    assert _footer_ink(widget) == (False, False)


def test_text_that_fits_is_one_page_holding_all_of_it():
    """Text within one page's height must be a single, complete page.

    Why: the common case must not acquire paging. If a short statement reported
    two pages the reader would be sent to a blank second page.

    Failure: page_count is not 1, or the page text is not the whole text.
    """
    widget = _widget(text="Develops toward the centre.")

    assert widget.page_count == 1
    assert widget.current_page == 1
    # The page holds the wrapped form, so the comparison is against the text with
    # its line breaks put back to spaces.
    assert widget.page_text.replace("\n", " ") == "Develops toward the centre."


def test_paging_covers_every_wrapped_line_exactly_once_in_order():
    """The pages must reconstruct the wrapped text, with nothing lost or doubled.

    Why this test exists: this is the whole point of the view. TextWidget draws
    ``height // line_height`` lines and discards the rest without complaint, so
    a pagination bug shows up as text that simply ends early -- which looks
    exactly like text that was written that way. Walking the pages and comparing
    against the wrap catches a dropped line, a duplicated boundary line (an
    off-by-one in the page window) and reordering, which a page-count assertion
    alone would not.

    Failure: the concatenation differs from the wrapped lines, or the page count
    is not ceil(lines / lines_per_page).
    """
    widget = _widget()
    per_page = widget.lines_per_page
    line_count = per_page * 2 + 1  # two full pages and a partial one
    widget.set_text(_numbered_lines(line_count))

    assert widget.page_count == math.ceil(line_count / per_page)

    seen: list[str] = []
    for expected_page in range(1, widget.page_count + 1):
        assert widget.current_page == expected_page
        page_lines = widget.page_text.split("\n")
        assert len(page_lines) <= per_page, f"page {expected_page} overflows the panel"
        seen.extend(page_lines)
        moved = widget.next_page()
        assert moved is (expected_page < widget.page_count)

    assert seen == [f"Line{index}" for index in range(line_count)]


def test_the_last_page_holds_the_remainder_rather_than_being_padded():
    """A partial final page must contain exactly the lines that are left.

    Why: the obvious slicing mistake is a fixed-size window that runs past the
    end, which either repeats earlier lines to fill the page or raises. Both are
    only visible on the last page of a long tip.

    Failure: the final page repeats lines from the page before it, or is empty.
    """
    widget = _widget()
    per_page = widget.lines_per_page
    widget.set_text(_numbered_lines(per_page + 2))

    widget.next_page()

    assert widget.current_page == 2
    assert widget.page_text.split("\n") == [f"Line{per_page}", f"Line{per_page + 1}"]


def test_the_cursor_stops_at_the_ends_unless_asked_to_wrap():
    """next/previous must refuse to move past the ends unless wrap is asked for.

    Why: the two owners want different endings. The help dialog treats "no next
    page" as "the reader has finished, close" and must be told when it happens;
    the coach panel cycles so OK always does something. One cursor serves both
    only if it reports whether it moved and takes wrapping as an argument.

    Failure: a bare next_page wraps silently (the help dialog then never closes)
    or wrap=True stops (the coach panel's OK becomes a no-op on the last page).
    """
    widget = _widget()
    widget.set_text(_numbered_lines(widget.lines_per_page * 3))

    assert widget.previous_page() is False, "already on the first page"
    assert widget.previous_page(wrap=True) is True
    assert widget.current_page == widget.page_count

    assert widget.next_page() is False, "already on the last page"
    assert widget.next_page(wrap=True) is True
    assert widget.current_page == 1


def test_new_text_returns_to_the_first_page():
    """Replacing the text must reset the cursor.

    Why: the panel is reused for the next statement or tip. Keeping the old
    index would open new text half way through, and on a shorter text it would
    point past the end.

    Failure: current_page stays where it was, or the view shows a page the new
    text does not have.
    """
    widget = _widget()
    widget.set_text(_numbered_lines(widget.lines_per_page * 3))
    widget.next_page()
    widget.next_page()
    assert widget.current_page == 3

    widget.set_text("Short.")

    assert widget.current_page == 1
    assert widget.page_count == 1
    assert widget.page_text == "Short."


def test_the_footer_says_which_page_of_how_many():
    """The footer label must name the current page and the total.

    Why: without it a reader cannot tell a paged tip from a truncated one, and
    has no idea how much is left. Asserted as a string rather than by reading
    pixels, since what matters is the two numbers being right and in the right
    order.

    Failure: the label is missing, off by one (a zero-based index leaking out),
    or reports the total as the current page.
    """
    widget = _widget()
    widget.set_text(_numbered_lines(widget.lines_per_page * 2))

    assert widget.footer_label == f"Page 1 of {widget.page_count}"
    widget.next_page()
    assert widget.footer_label == f"Page 2 of {widget.page_count}"


def test_a_single_page_can_be_asked_not_to_show_a_footer():
    """The footer must be suppressible for text that is one page.

    Why: the coach panel shows "Page 1 of 1" so the checkmark affordance is
    always explained, while the help dialog has its own dismiss line and would
    only be adding noise under a tip that has no second page.

    Failure: the flag is ignored, so one of the two owners shows a paging
    affordance for something that cannot be paged.
    """
    quiet = _widget(text="Short.", footer_on_single_page=False)
    loud = _widget(text="Short.", footer_on_single_page=True)

    assert _footer_ink(quiet) == (False, False)
    assert _footer_ink(loud)[0] is True


def test_a_multi_page_view_always_shows_the_footer():
    """Once there is a second page the footer must appear regardless of the flag.

    Why: the flag is about single-page noise, not about hiding the fact that
    there is more text. Suppressing it here is how a tip silently looks
    truncated again.

    Failure: footer_on_single_page=False also hides the footer on page 1 of 3.
    """
    widget = _widget(footer_on_single_page=False)
    widget.set_text(_numbered_lines(widget.lines_per_page * 2))

    assert _footer_ink(widget)[0] is True


@pytest.mark.parametrize(
    ("hint", "expect_hint_ink"),
    [(NavigationHint.NONE, False), (NavigationHint.OK_NEXT, True), (NavigationHint.UP_DOWN, True)],
)
def test_the_navigation_hint_is_drawn_on_the_right_of_the_footer(hint, expect_hint_ink):
    """The footer must show how to turn the page, and only when asked.

    Why: the two owners page on different buttons -- OK on the coach panel,
    UP/DOWN in the help dialog -- and a page indicator with no affordance leaves
    the reader pressing the wrong button and closing the text. NONE exists for a
    caller that draws its own.

    Failure: the hint is missing (nothing tells the reader which button pages)
    or drawn for NONE (a hint for a button that does nothing here).
    """
    widget = _widget(hint=hint)
    widget.set_text(_numbered_lines(widget.lines_per_page * 2))

    _left, right = _footer_ink(widget)
    assert right is expect_hint_ink


def test_a_full_page_of_body_text_stays_clear_of_the_footer_band():
    """A page filled to its last line must leave the footer band untouched.

    Why this test exists: this is the defect the view exists to prevent. If pages
    were measured against the whole panel instead of the panel minus the footer,
    the last line of a full page would be drawn over the page indicator and, on a
    taller overrun, off the bottom of the screen where PIL clips it silently.

    The text is exactly ``lines_per_page`` lines: the worst case for the body, and
    a single page, so ``footer_on_single_page=False`` suppresses the footer and
    any ink left in the band can only have come from the body.

    Failure: ink in the band, meaning the page was measured against the full
    height.
    """
    widget = _widget(hint=NavigationHint.NONE, footer_on_single_page=False)
    widget.set_text(_numbered_lines(widget.lines_per_page))
    assert widget.page_count == 1, "the fixture must fill exactly one page"

    pixels = _rendered(widget).convert("1").load()
    footer_top = widget.height - widget.FOOTER_HEIGHT
    ink = [
        (x, y)
        for y in range(footer_top, widget.height)
        for x in range(widget.width)
        if pixels[x, y] == 0
    ]

    assert ink == [], f"a full page draws into the footer band at {ink[:5]}"


def test_the_body_area_leaves_room_for_the_footer():
    """Lines per page must be measured against the panel minus the footer.

    Why this test exists: the pixel check above can only see the body's last
    line. This states the geometry the pagination depends on, so a change to
    FOOTER_HEIGHT or the line height that stops leaving room fails here with the
    numbers in the message rather than as a smudge on a panel.

    Failure: a full page's height exceeds the body area.
    """
    widget = _widget()
    line_height = widget.font_size + 2
    body_height = widget.height - widget.FOOTER_HEIGHT

    assert widget.lines_per_page * line_height <= body_height, (
        f"{widget.lines_per_page} lines of {line_height}px do not fit in "
        f"{body_height}px of body area"
    )


def test_a_larger_font_fits_fewer_lines_and_so_more_pages():
    """Lines per page must follow the font size.

    Why: the board's Text Size setting scales the coach body. A view that scaled
    the font without re-measuring would keep the same lines per page and overflow
    the panel -- the setting would appear to work and quietly cut text off.

    Failure: lines_per_page is equal for two different font sizes, or the larger
    font does not produce more pages for the same text.
    """
    small = _widget(font_size=10)
    large = _widget(font_size=20)

    assert large.lines_per_page < small.lines_per_page

    text = _numbered_lines(30)
    small.set_text(text)
    large.set_text(text)
    assert large.page_count > small.page_count
