"""
Every `url_for()` in every template names an endpoint that exists.

⚠️ **Written because the blueprint split broke four of them and the test suite
did not notice.** MA-1 changed every endpoint name - `manage_students` became
`students.manage_students` - and the templates were rewritten with a regex that
only matched `url_for('name'` on one line. Four calls spanned two lines:

    href="{{ url_for(
        'edit_student', student_id=student.student_id
    ) }}"

They were found by loading `/manage_students` in a test client and reading the
500, which is lessons.md L5 exactly - "it imports" is not "it runs", and the
check that would have caught it was one layer away from where the failure
lived. A `BuildError` is raised at *render* time, so nothing short of rendering
the page, or this, sees it.

**Why this is not the same as rendering every page in a test.** Rendering needs
a database, a signed-in session and a plausible context object per template,
which is a lot of machinery to maintain for a check about names. This asserts
the one property that broke: the name resolves.
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import PROJECT_ROOT

TEMPLATES = PROJECT_ROOT / "templates"

# Tolerates whitespace and newlines between `url_for(` and the endpoint name,
# which is the whole reason this file exists. Flask's own `static` is included
# rather than skipped - it is an endpoint like any other and it does exist.
URL_FOR = re.compile(r"url_for\(\s*(['\"])([A-Za-z_][A-Za-z0-9_.]*)\1")


def _references():
    """`(template, line, endpoint)` for every url_for call in templates/."""
    found = []

    for path in sorted(TEMPLATES.glob("*.html")):
        text = path.read_text(encoding="utf-8")

        for match in URL_FOR.finditer(text):
            line = text[: match.start()].count("\n") + 1
            found.append((path.name, line, match.group(2)))

    return found


@pytest.fixture(scope="module")
def endpoints():
    import app as app_module

    return set(app_module.app.view_functions)


def test_the_templates_actually_reference_endpoints():
    """
    The scanner finds something.

    Without this, deleting every template or breaking the regex would leave
    the parametrised test below with nothing to check and a green tick - the
    apparatus-measuring-nothing failure of lessons.md L3.
    """
    references = _references()

    assert len(references) > 30, (
        f"Only {len(references)} url_for calls found across "
        f"{len(list(TEMPLATES.glob('*.html')))} templates. The scanner is "
        "probably broken rather than the templates being empty."
    )


@pytest.mark.parametrize(
    ("template", "line", "endpoint"),
    _references(),
    ids=lambda value: str(value),
)
def test_every_template_endpoint_resolves(endpoints, template, line, endpoint):
    assert endpoint in endpoints, (
        f"{template}:{line} builds a URL for {endpoint!r}, which no route "
        "provides. This raises BuildError when the page renders, so the "
        "operator gets a 500 on a page that looks fine in review."
    )


def test_no_template_uses_an_unqualified_endpoint(endpoints):
    """
    Everything except Flask's own `static` is blueprint-qualified.

    A bare name is not necessarily *broken* - it is broken today, because every
    route lives in a blueprint - but it is the shape the split had to remove,
    and catching it by name gives a better message than "endpoint not found".
    """
    unqualified = [
        f"{template}:{line} -> {endpoint}"
        for template, line, endpoint in _references()
        if "." not in endpoint and endpoint != "static"
    ]

    assert not unqualified, (
        "These url_for calls name a route without its blueprint:\n  "
        + "\n  ".join(unqualified)
    )
