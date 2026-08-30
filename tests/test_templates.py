"""
Structural rules for the templates (US-1, US-5, US-6, US-8).

⚠️ **Everything here parses the HTML. Nothing greps it.**

That is lessons.md L12 applied where it bites hardest. This codebase comments
heavily by design, and the comments explain exactly the constructions these
tests ban - `templates/manage_students.html` contains the sentence *"This was
`onsubmit="return confirm(...)"`"*, describing the bug it no longer has. A text
search for `onsubmit=` matches that sentence and fails; a search for its
absence would pass on a page that still had the handler if the comment were
ever removed. Only a parser can tell an attribute from prose about one.

`html.parser` is in the standard library and is lenient about Jinja, which
appears as text and as bare `{{ ... }}` inside attribute values. The parser
never sees a `{% if %}` as markup, so a conditionally-rendered attribute is
read as present - which is the safe direction for a ban.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser

import pytest

from tests.conftest import PROJECT_ROOT

TEMPLATES = PROJECT_ROOT / "templates"

# Pages, as opposed to the layout and the two partials that are included in
# one. Only pages extend base.html.
PARTIALS = {"base.html", "sidebar.html", "training_status.html"}


def page_templates():
    return sorted(p for p in TEMPLATES.glob("*.html") if p.name not in PARTIALS)


def all_templates():
    return sorted(TEMPLATES.glob("*.html"))


class Collector(HTMLParser):
    """Records the tags, attributes and script bodies of a template."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.attributes = []   # (tag, name, value)
        self.tags = []
        self.scripts = []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)

        for name, value in attrs:
            self.attributes.append((tag, name, value))

        if tag == "script":
            self._in_script = True

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script and data.strip():
            self.scripts.append(data)


def parse(path):
    collector = Collector()
    collector.feed(path.read_text(encoding="utf-8"))
    return collector


# ---------------------------------------------------------------------------
# US-5 - the confirmation that silently did not happen
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", all_templates(), ids=lambda p: p.name)
def test_no_template_uses_an_inline_event_handler(path):
    """
    ⚠️ **This is US-5, and the bug it prevents is a missing confirmation
    rather than a broken one.**

    `onsubmit="return confirm('Delete {{ student.name }}?')"` renders, for a
    student named `O'Brien`, as `confirm('Delete O&#39;Brien?')` - which the
    browser decodes to a bare apostrophe *before* parsing it as JavaScript. The
    handler is then a syntax error, so it never compiles, so `onsubmit` does
    nothing, so **the delete form submits with no confirmation at all** - on
    exactly the records where a mis-click is least recoverable.

    `data-confirm` carries the same text as an attribute *value*, which is
    never parsed as code, and static/js/app.js reads it with a delegated
    listener.
    """
    handlers = [
        (tag, name)
        for tag, name, _value in parse(path).attributes
        if name.startswith("on")
    ]

    assert not handlers, (
        f"{path.name} carries inline event handler(s) {handlers}. Use a data- "
        "attribute and a listener in static/js/ - see US-5 in "
        "static/js/app.js for why an interpolated handler silently does "
        "nothing."
    )


def test_the_delete_forms_still_ask_for_confirmation():
    """
    Guards the fix rather than only the bug.

    Banning `onsubmit` without this would be satisfied by deleting the
    confirmation entirely, which is the same outcome the bug produced.
    """
    confirming = {
        path.name
        for path in all_templates()
        for _tag, name, _value in parse(path).attributes
        if name == "data-confirm"
    }

    expected = {
        "manage_students.html",
        "manage_instructors.html",
        "students.html",
        "subjects.html",
        "subject_enrolments.html",
    }

    missing = expected - confirming

    assert not missing, (
        f"{sorted(missing)} have destructive actions with no confirmation."
    )


# ---------------------------------------------------------------------------
# US-8 - no JavaScript in the templates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", all_templates(), ids=lambda p: p.name)
def test_no_template_contains_inline_script(path):
    """
    US-8: `static/js/script.js` was an empty committed file and every line of
    JavaScript lived in a template - 450 of them in enrol.html alone.

    Inline script is also what makes US-5 possible: it is the only place a
    server-rendered value ends up *inside* a JavaScript string literal. Values
    reach static/js/ through data- attributes now, where they are text.
    """
    bodies = parse(path).scripts

    assert not bodies, (
        f"{path.name} contains {len(bodies)} inline <script> block(s). Move "
        "the code to static/js/ and pass server values in as data- attributes."
    )


# ---------------------------------------------------------------------------
# US-1 - one layout, so a page cannot miss the flash region
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", page_templates(), ids=lambda p: p.name)
def test_every_page_extends_the_layout(path):
    """
    Twenty templates each repeated the doctype, the head, the stylesheet link
    and the sidebar include. That is what made US-1 possible to *half*-fix: a
    flash region added to nineteen of them is a page where confirmations
    silently do not appear.
    """
    first = path.read_text(encoding="utf-8").lstrip().splitlines()[0].strip()

    assert first == '{% extends "base.html" %}', (
        f"{path.name} does not extend base.html (starts with {first!r}), so it "
        "has its own <head>, its own sidebar and no flash region."
    )


@pytest.mark.parametrize("path", page_templates(), ids=lambda p: p.name)
def test_no_page_repeats_the_document_shell(path):
    """The layout owns <html>, <head> and <body>. Nothing else may."""
    tags = set(parse(path).tags)
    shell = tags & {"html", "head", "body"}

    assert not shell, (
        f"{path.name} defines {sorted(shell)}, which base.html already owns."
    )


def test_the_layout_renders_flashed_messages():
    """
    ⚠️ Without this, every `flash()` call in `web/` writes into a session that
    nothing ever reads. The messages would accumulate silently and appear on
    some later page in an unpredictable order, which is worse than no
    confirmation at all.
    """
    layout = (TEMPLATES / "base.html").read_text(encoding="utf-8")

    assert "get_flashed_messages" in layout, (
        "base.html does not render flashed messages, so every flash() call in "
        "the application is written and never read."
    )


# ---------------------------------------------------------------------------
# US-6 - accessibility affordances that are structural
# ---------------------------------------------------------------------------


def test_the_layout_provides_a_skip_link_and_a_main_landmark():
    collector = parse(TEMPLATES / "base.html")

    classes = {value for _tag, name, value in collector.attributes if name == "class"}
    ids = {value for _tag, name, value in collector.attributes if name == "id"}

    assert "skip-link" in classes, "base.html has no skip link (US-6)"
    assert "main" in ids, "base.html has no #main for the skip link to reach"


def test_the_navigation_is_a_landmark_with_a_name():
    """
    The sidebar was a bare <div>. A screen-reader user could not jump to the
    navigation, because as far as the browser was concerned there was none.
    """
    collector = parse(TEMPLATES / "sidebar.html")

    assert "nav" in collector.tags, "the sidebar is not a <nav> landmark"

    labels = [
        value
        for tag, name, value in collector.attributes
        if tag == "nav" and name == "aria-label"
    ]

    assert labels, "the <nav> has no accessible name"


@pytest.mark.parametrize("path", all_templates(), ids=lambda p: p.name)
def test_every_image_has_alt_text(path):
    """
    An <img> with no alt attribute is announced by its filename or its URL,
    which for the camera feed would be "video feed" read as a path.
    """
    missing = []

    for tag, attrs in _elements(path, "img"):
        if "alt" not in attrs:
            missing.append(dict(attrs).get("id") or dict(attrs).get("src") or tag)

    assert not missing, f"{path.name} has <img> without alt: {missing}"


def _elements(path, wanted):
    """`(tag, [(name, value), ...])` for every `wanted` element in a template."""

    class Finder(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.found = []

        def handle_starttag(self, tag, attrs):
            if tag == wanted:
                self.found.append((tag, [name for name, _value in attrs]))

    finder = Finder()
    finder.feed(path.read_text(encoding="utf-8"))

    return finder.found


def test_the_scanner_finds_the_templates():
    """
    Guards every parametrised ban above against silently having no cases -
    the apparatus-measuring-nothing failure of lessons.md L3.
    """
    assert len(all_templates()) >= 18
    assert len(page_templates()) >= 15


# ---------------------------------------------------------------------------
# A page whose classes have no rules
# ---------------------------------------------------------------------------


STYLESHEET = PROJECT_ROOT / "static" / "css" / "style.css"

# ⚠️ **Scoped to the two camera pages, and that is a real limit rather than a
# tidy one.** Six other templates carry nine classes with no rule between them
# - `back-btn`, `action-btn`, `export-btn`, `welcome-box`, `form-help`,
# `no-records`, and so on. They are cosmetic and pre-date this test; widening
# it would mean either styling all nine or carrying an allowlist that rots.
#
# These two are here because this is where the defect class actually cost
# something. See the test below.
STYLED_PAGES = ("enrol.html", "attendance.html")


def _stylesheet_without_comments():
    """
    style.css with every `/* ... */` block removed.

    ⚠️ **Not cosmetic - stripping these is what makes the test below mean
    anything**, and a mutation run is what proved it. The stylesheet documents
    itself heavily, and the comment introducing the capture page names
    `.capture-overlay`, `.capture-stage` and `#preview` in prose. A scan of the
    raw file therefore finds a "rule" for any class merely *discussed* there,
    so deleting a real rule left the test green.

    That is lessons.md L12 - a search matching the comment that explains the
    code - occurring inside the test written to catch a styling bug. The module
    docstring above says everything here parses rather than greps; CSS has no
    parser in the standard library, so removing comments first is the nearest
    honest equivalent.
    """
    stylesheet = STYLESHEET.read_text(encoding="utf-8")

    return re.sub(r"/\*.*?\*/", " ", stylesheet, flags=re.DOTALL)


def _selectors_defined():
    """Every class name that actually appears in a selector."""
    without_comments = _stylesheet_without_comments()

    # Declaration blocks go too: `content:"done"` and font stacks can carry a
    # dot, and only the text outside `{ ... }` is selector.
    selector_text = re.sub(r"\{[^{}]*\}", " ", without_comments)

    return set(re.findall(r"\.([A-Za-z0-9_-]+)", selector_text))


def _classes_used(path):
    """Every class named in a template, ignoring Jinja-computed ones."""

    class Finder(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.classes = set()

        def handle_starttag(self, tag, attrs):
            for name, value in attrs:
                # A value containing `{` is `class="{{ ... }}"` or similar -
                # the parser sees the Jinja as text and there is nothing to
                # check.
                if name == "class" and value and "{" not in value:
                    self.classes.update(value.split())

    finder = Finder()
    finder.feed(path.read_text(encoding="utf-8"))

    return finder.classes


@pytest.mark.parametrize("page", STYLED_PAGES)
def test_every_class_on_a_camera_page_has_a_rule(page):
    """
    ⚠️ **The whole enrolment capture page had no stylesheet, and the first UAT
    reported it as three unrelated-looking bugs.**

    Ten classes, zero rules. What that produced:

    * *"camera view is overflowing the screen"* - `#preview` is a `<video>`, so
      with no CSS it lays out at its intrinsic size. The page asks
      getUserMedia for 1920x1080, so a 1920px video was pushed into a ~1500px
      column and the rest of the page went off the bottom of the viewport.
    * *"capture direction is ambiguous"* - mostly because three indicators
      `enrol.js` was already driving did not render. It filled a progress bar
      with no height, marked the current stage on a list where all three
      `data-state` values looked identical, and drew the face box onto
      `.capture-overlay` - a `<canvas>` **sibling** of the video which, without
      `position:absolute`, stacked underneath as its own block. The box had
      never once appeared on the picture.
    * And the preview was never mirrored, though `drawBox()` says it mirrors
      coordinates "to match the preview, which is flipped". Turning left made
      the picture appear to turn right.

    **None of it was a JavaScript bug and none of it raised anything.** The
    behaviour was correct and invisible, which is why the suite was green
    through all three - it checks structure and endpoints, and a missing
    stylesheet is neither.

    A class named in the markup and absent from the stylesheet is the cheapest
    possible signal for that, and it is a parse rather than a grep because the
    comment blocks in these templates discuss the very classes involved
    (lessons.md L12).
    """
    defined = _selectors_defined()
    used = _classes_used(TEMPLATES / page)

    assert used, f"{page} names no classes at all - has the scanner broken?"

    missing = sorted(name for name in used if name not in defined)

    assert not missing, (
        f"{page} uses {missing} but static/css/style.css has no rule for "
        f"them. An unstyled class does not raise - it renders as an "
        f"unstyled element, which is how the capture page shipped with an "
        f"unbounded <video> and an overlay canvas that was never over "
        f"anything."
    )


def test_the_capture_overlay_is_positioned_over_the_video():
    """
    ⚠️ The one rule whose *absence* is silent and whose *presence* is
    load-bearing, pinned by value rather than by existence.

    `.capture-overlay` is a `<canvas>` that is a sibling of `#preview`, not a
    child. `drawBox()` sizes its backing store from `clientWidth`/
    `clientHeight` and draws the face rectangle in video coordinates. If it is
    not taken out of flow and stretched over `.capture-stage`, all of that
    still runs, still succeeds, and paints into a box sitting below the
    picture.

    The test above would pass on a `.capture-overlay{color:red}`. This one
    would not.
    """
    stylesheet = _stylesheet_without_comments()

    def block(selector):
        match = re.search(
            re.escape(selector) + r"\s*\{([^}]*)\}", stylesheet
        )
        assert match, f"{selector} has no rule at all"
        return match.group(1).replace(" ", "").replace("\n", "")

    stage = block(".capture-stage")
    overlay = block(".capture-overlay")
    preview = block("#preview")

    assert "position:relative" in stage, (
        ".capture-stage must establish a containing block, or the overlay "
        "positions itself against the viewport instead of the video"
    )
    assert "position:absolute" in overlay, (
        ".capture-overlay is in normal flow, so it renders as a block BELOW "
        "the video rather than on top of it - the face box is invisible"
    )
    assert "overflow:hidden" in stage, (
        "without this the video's corners escape the rounded stage"
    )

    # The overflow half of the same finding: the page must size the video,
    # never the other way round.
    assert "height:100%" in preview and "object-fit:cover" in preview, (
        "#preview is unbounded, so a 1920x1080 stream lays out at its "
        "intrinsic size and pushes the page off the screen"
    )


# ---------------------------------------------------------------------------
# A `tojson` value in a double-quoted attribute
# ---------------------------------------------------------------------------


# Every key and value carries a character that must survive the round trip: a
# double quote (the one that broke it), a single quote, and the three
# characters `tojson` does escape. If the attribute is quoted correctly, all of
# them come back; if it is not, parsing stops at the first `"`.
HOSTILE_RECORD = {
    'college"department': 'CCS "quoted"',
    "program": "BS'IT",
    "year_level": "<1 & 2>",
    "section": "A",
}


def _render(path, **context):
    """
    Render one template **through the application's own Jinja environment**.

    ⚠️ Not a hand-built `jinja2.Environment`, and that is lessons.md L3 rather
    than convenience. The whole finding is about what one filter escapes, and
    `tojson` is not one filter: jinja2 ships `htmlsafe_json_dumps` and Flask
    replaces it with its own JSON provider. They already differ observably -
    Flask's sorts keys - so a test driving jinja2's would be measuring a
    filter production does not use, on the exact question of what production's
    filter escapes.

    Not the Flask *test client* either: rendering a page there needs a
    database, a signed-in session and a plausible context object per template
    (test_template_endpoints.py's docstring says why that is not worth
    maintaining). A request context gives `url_for`, `csrf_token` and the real
    filters without any of that.
    """
    from flask import render_template

    import app as app_module

    with app_module.app.test_request_context("/"):
        return render_template(path.name, **context)


def _json_looking_attributes(markup):
    """`(tag, name, value)` for every data- attribute holding a JSON object."""

    class Finder(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.found = []

        def handle_starttag(self, tag, attrs):
            for name, value in attrs:
                if not name.startswith("data-") or not value:
                    continue
                if value.lstrip()[:1] in "{[":
                    self.found.append((tag, name, value))

    finder = Finder()
    finder.feed(markup)

    return finder.found


def test_a_json_data_attribute_survives_being_parsed_as_html():
    """
    ⚠️ **This is the defect that took the enrolment page down completely, and
    it is US-5 one context over.**

    `templates/enrol.html` carried `data-record="{{ record | tojson }}"`.
    `tojson` escapes `<`, `>`, `&` and `'` - it does **not** escape `"`,
    because it is built to be safe inside a script element or a
    *single*-quoted attribute. In a double-quoted one the first `"` of the
    JSON closes the attribute, so the browser handed
    `getAttribute("data-record")` the single character `{`, `JSON.parse` threw
    at enrol.js line 34, and the IIFE died before reaching `openCamera(null)`
    on its last line.

    The page then showed every placeholder the server had rendered -
    "Preparing the camera...", "Waiting for camera permission..." - and the
    camera never opened. Nothing was logged, because the failure was in the
    browser.

    It only bit *new* enrolments: recapture passes `record = {}`, which
    contains no quote, so the page worked. That is why it reached a UAT.

    **This renders and parses.** A text search for `tojson` would match the
    comment in enrol.html that explains all of the above (lessons.md L12), and
    a search of the *source* cannot see the quoting at all - `html.parser`
    discards the quote character, so only the round trip through a real value
    shows the difference.
    """
    markup = _render(
        TEMPLATES / "enrol.html",
        student_id="23-1-1-0559",
        student_name="Chrizol D. Evangelista",
        mode="new",
        record=HOSTILE_RECORD,
        plan=[],
        total=100,
        min_native_width=960,
    )

    attributes = _json_looking_attributes(markup)

    assert attributes, (
        "enrol.html no longer renders a JSON data- attribute. If data-record "
        "was removed this test should go with it; if it was renamed, rename "
        "it here."
    )

    for tag, name, value in attributes:
        try:
            parsed = json.loads(value)
        except ValueError as error:
            pytest.fail(
                f"<{tag} {name}> does not survive HTML parsing: {error}. "
                f"The parser recovered {value!r}. A `| tojson` value must sit "
                f"in a SINGLE-quoted attribute - tojson does not escape the "
                f"double quote."
            )

        assert parsed == HOSTILE_RECORD, (
            f"<{tag} {name}> parsed, but not back to what was rendered: "
            f"{parsed!r}"
        )
