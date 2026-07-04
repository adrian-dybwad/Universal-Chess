"""Tests for CoachTextWidget pagination and the paging footer.

Why these tests exist
---------------------
A long coach statement or hint tip no longer overflows the board-area panel:
the wrapped lines are split into fixed-height pages and the OK (checkmark) button
advances one page at a time, wrapping back to the first page after the last. A
footer reads "Page N of X" (left) with a "Next" + checkmark glyph (right). These
tests pin:
  - a short statement is a single page and OK re-affirms page 1 (never a full
    refresh),
  - a long statement paginates into ceil(lines / lines_per_page) pages and OK
    cycles forward and wraps to page 1,
  - only the current page's lines are pushed to the rendered body,
  - the footer is drawn (black pixels in the reserved footer band) whenever a
    statement is present and omitted when the panel is empty.

A regression would drop the tail of a long statement (truncated instead of
paged), fail to wrap back to page 1, show the wrong page's lines, or render no
paging affordance.
"""

import math

from PIL import Image

from universalchess.epaper.coach_text import CoachTextWidget


def _widget():
    """A 128x128 coach-text widget with a no-op update callback."""
    return CoachTextWidget(0, 16, 128, 128, lambda full=False, immediate=False: None)


def _multiline_text(line_count: int) -> str:
    """Text with `line_count` explicit lines, each short enough not to re-wrap.

    Explicit newlines make pagination deterministic regardless of font metrics:
    _wrap_text keeps one output line per input line, so the page count depends
    only on lines_per_page.
    """
    return "\n".join(f"Line{i}" for i in range(line_count))


def test_empty_widget_has_no_pages_and_cannot_page():
    # A freshly constructed widget holds no statement: page_count is 0 and
    # next_page reports False so the OK handler falls back to a full refresh.
    # A regression returning True here would swallow the refresh on an empty panel.
    widget = _widget()
    assert widget.page_count == 0
    assert widget.current_page == 0
    assert widget.next_page() is False


def test_short_statement_is_single_page_and_ok_stays_on_page_one():
    # A statement that fits in one page must report exactly one page. next_page
    # returns True (OK paged instead of full-refreshing) and stays on page 1,
    # since wrapping (page + 1) % 1 == 0. A regression producing 0 pages or
    # advancing past the only page would misreport the footer / lose the text.
    widget = _widget()
    widget.set_text("Develops toward the center.")
    assert widget.page_count == 1
    assert widget.current_page == 1
    assert widget.next_page() is True
    assert widget.current_page == 1


def test_long_statement_paginates_and_wraps_forward():
    # 20 explicit lines exceed one page (8 lines) and must split into 3 pages.
    # OK advances 1 -> 2 -> 3 and then wraps back to 1. A regression that
    # truncated the statement would report 1 page; one that failed to wrap would
    # get stuck on the last page.
    widget = _widget()
    line_count = 20
    widget.set_text(_multiline_text(line_count))
    per_page = widget._lines_per_page()
    expected_pages = math.ceil(line_count / per_page)
    assert expected_pages >= 2  # guard: the fixture must actually overflow one page
    assert widget.page_count == expected_pages
    assert widget.current_page == 1

    seen = [widget.current_page]
    for _ in range(expected_pages - 1):
        assert widget.next_page() is True
        seen.append(widget.current_page)
    assert seen == list(range(1, expected_pages + 1))

    # One more press wraps back to the first page.
    assert widget.next_page() is True
    assert widget.current_page == 1


def test_only_current_page_lines_are_shown_in_body():
    # The rendered body must contain only the current page's lines, not the whole
    # statement. A regression rendering all lines would overflow the panel and the
    # first/second pages would be identical.
    widget = _widget()
    per_page = widget._lines_per_page()
    line_count = per_page * 2 + 1  # forces 3 pages, last one partial
    widget.set_text(_multiline_text(line_count))

    first_page_body = widget._body.text
    assert first_page_body == "\n".join(f"Line{i}" for i in range(per_page))

    widget.next_page()
    second_page_body = widget._body.text
    assert second_page_body == "\n".join(
        f"Line{i}" for i in range(per_page, per_page * 2)
    )
    assert first_page_body != second_page_body


def test_new_statement_resets_to_first_page():
    # Selecting a new statement must start at page 1 even if the previous one was
    # paged forward. A regression retaining the old page index would open a new
    # statement mid-way through.
    widget = _widget()
    widget.set_text(_multiline_text(20))
    widget.next_page()
    widget.next_page()
    assert widget.current_page == 3

    widget.set_text(_multiline_text(20).replace("Line", "Word"))
    assert widget.current_page == 1
    assert widget._body.text.startswith("Word0")


def _footer_has_ink(widget: CoachTextWidget) -> bool:
    """True if any black pixel is rendered in the widget's footer band."""
    sprite = Image.new("1", (widget.width, widget.height), 255)
    widget.render(sprite)
    pixels = sprite.load()
    footer_top = widget.height - widget.FOOTER_HEIGHT
    for y in range(footer_top, widget.height):
        for x in range(widget.width):
            if pixels[x, y] == 0:
                return True
    return False


def test_footer_drawn_when_statement_present():
    # The paging footer ("Page N of X" + Next checkmark) must render whenever a
    # statement is shown, so the reader sees the paging affordance. A regression
    # skipping the footer would leave the band blank.
    widget = _widget()
    widget.set_text("A concise coaching remark for this move.")
    assert _footer_has_ink(widget) is True


def test_footer_absent_when_empty():
    # With no statement there is nothing to page, so the footer must not draw.
    # A regression drawing a "Page 0 of 0" footer on an empty panel would show a
    # meaningless indicator.
    widget = _widget()
    assert _footer_has_ink(widget) is False
