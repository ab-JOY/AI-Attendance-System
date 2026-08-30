"""
`/cameras` and `/cameras/select` - choosing the **server's** camera (UAT).

Reported from a UAT as "a webcam and a laptop cam are both available but the
app defaults to the laptop cam". It did, and correctly:
`open_best_camera()` scans upward from index 0 and takes the first device that
produces a picture, which is the built-in. What was missing was any way to see
that a second camera existed, or to say so - `selected_camera.txt` could only
ever be written by the scan itself.

⚠️ **This is not the enrolment camera picker.** Enrolment runs in the browser
on `getUserMedia`, where the *browser* enumerates devices; recognition opens
the classroom camera with OpenCV on the server, which the browser cannot see.
Two pages, two pickers, and no shared code between them - that is the
architecture, not an oversight.

**No camera is opened here.** `camera_utils` is replaced at the seam
`web/sessions.py` imports it under. What is under test is the routes'
behaviour: that a scan is refused while a session holds the device, and that a
chosen index is proved to work *before* it is saved.

That last one is the load-bearing case. `save_camera_index()`'s docstring
states an invariant the whole CAM-1 fix rests on - it is only ever called with
an index that has already passed `probe_camera()` - because the saved index is
tried **first** on the next run. A route that wrote whatever integer arrived
would put the dead camera back in the one place CAM-1 removed it from.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def flask_app():
    import app as app_module

    app_module.app.config["TESTING"] = True
    return app_module.app


@pytest.fixture
def client(flask_app):
    # Same pattern as test_subject_routes.py. CSRF is covered by
    # test_route_security.py; turning it off here keeps these cases about the
    # camera rather than about the token, and re-enabling it on the way out
    # stops one module weakening another's protection.
    flask_app.config["WTF_CSRF_ENABLED"] = False
    yield flask_app.test_client()
    flask_app.config["WTF_CSRF_ENABLED"] = True


def sign_in(client):
    """Fabricate a session without touching the database."""
    with client.session_transaction() as session:
        session["user"] = "test-admin"
        session["role"] = "admin"


class FakeSession:
    """Enough of `RecognitionSession` for these two routes."""

    def __init__(self, *, running):
        self.is_running = running


def camera(index, *, width=1280, height=720, frozen=False, saved=False):
    return {
        "index": index,
        "width": width,
        "height": height,
        "frozen": frozen,
        "is_saved_default": saved,
    }


@pytest.fixture
def bench(monkeypatch):
    """
    Stub the session and both camera_utils functions; record what was saved.

    `saved` starts empty and stays empty unless a route actually calls
    `save_camera_index()`, which is what most of these tests assert on.
    """
    import web.sessions as sessions_module

    state = {
        "cameras": [camera(0), camera(1)],
        "saved": [],
        "scan_error": None,
        "scans": 0,
    }

    def describe(max_tested=5):
        state["scans"] += 1

        if state["scan_error"] is not None:
            raise state["scan_error"]

        return list(state["cameras"])

    monkeypatch.setattr(sessions_module, "describe_available_cameras", describe)
    monkeypatch.setattr(
        sessions_module, "save_camera_index", lambda index: state["saved"].append(index)
    )
    monkeypatch.setattr(
        sessions_module, "recognition_session", FakeSession(running=False)
    )

    state["set_running"] = lambda running: monkeypatch.setattr(
        sessions_module, "recognition_session", FakeSession(running=running)
    )

    return state


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def test_listing_cameras_requires_authentication(client):
    response = client.get("/cameras")

    assert response.status_code == 401
    assert response.is_json, (
        "the picker calls this with fetch(); an HTML login page would be "
        "parsed as JSON and reported as a syntax error"
    )


def test_selecting_a_camera_requires_authentication(client, bench):
    """
    ⚠️ Asserts on the *effect*, not only the status.

    An earlier version of this accepted `status_code in (400, 401)`, and it
    passed against a CSRF rejection - so it would have gone green on a route
    with no `@authenticated` at all. Whether the camera was written is the
    question actually worth asking.
    """
    response = client.post("/cameras/select", data={"index": "1"})

    assert response.status_code == 401
    assert bench["saved"] == []
    assert bench["scans"] == 0


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_the_scan_reports_every_usable_camera(client, bench):
    sign_in(client)

    body = client.get("/cameras").get_json()

    assert body["success"]
    assert [c["index"] for c in body["cameras"]] == [0, 1]


def test_the_scan_is_refused_while_a_session_is_running(client, bench):
    """
    ⚠️ Probing opens each device in turn, so a scan during a session would
    collide with the camera that session is holding - at best a slow scan, at
    worst taking the picture away from a live register.
    """
    sign_in(client)
    bench["set_running"](True)

    body = client.get("/cameras").get_json()

    assert not body["success"]
    assert body["cameras"] == []
    assert "session" in body["message"].lower()
    assert bench["scans"] == 0, "a device was opened during a running session"


def test_a_scan_that_raises_does_not_break_the_page(client, bench):
    """DirectShow is not shy about raising, and the page must still load."""
    sign_in(client)
    bench["scan_error"] = RuntimeError("DirectShow exploded")

    body = client.get("/cameras").get_json()

    assert not body["success"]
    assert body["cameras"] == []


def test_the_saved_default_is_marked_so_the_page_can_preselect_it(client, bench):
    sign_in(client)
    bench["cameras"] = [camera(0), camera(1, saved=True)]

    body = client.get("/cameras").get_json()

    assert [c["is_saved_default"] for c in body["cameras"]] == [False, True]


# ---------------------------------------------------------------------------
# Selecting
# ---------------------------------------------------------------------------


def test_a_usable_camera_is_saved(client, bench):
    sign_in(client)

    body = client.post("/cameras/select", data={"index": "1"}).get_json()

    assert body["success"]
    assert bench["saved"] == [1]


@pytest.mark.parametrize("index", ["", "  ", "abc", "-1", "1.5", "0x1"])
def test_an_index_that_is_not_a_number_is_refused(client, bench, index):
    sign_in(client)

    body = client.post("/cameras/select", data={"index": index}).get_json()

    assert not body["success"]
    assert bench["saved"] == []


def test_an_index_that_is_not_producing_a_picture_is_refused(client, bench):
    """
    ⚠️ **The case the CAM-1 invariant depends on.**

    An integer proves nothing: CAM-1 *is* the finding that a camera can open,
    read successfully and return black. The saved index is tried first on the
    next run, so writing an unproven one would put the dead device back in the
    exact position CAM-1 removed it from.
    """
    sign_in(client)
    bench["cameras"] = [camera(0), camera(1)]

    body = client.post("/cameras/select", data={"index": "4"}).get_json()

    assert not body["success"]
    assert bench["saved"] == [], (
        "an index that no longer produces a picture was saved as the "
        "preference - see save_camera_index()'s docstring"
    )


def test_the_index_is_proved_by_a_scan_rather_than_by_parsing(client, bench):
    """The route rescans; it does not trust the list the page was rendered with."""
    sign_in(client)

    client.post("/cameras/select", data={"index": "1"})

    assert bench["scans"] == 1


def test_a_camera_cannot_be_changed_while_a_session_is_running(client, bench):
    sign_in(client)
    bench["set_running"](True)

    body = client.post("/cameras/select", data={"index": "1"}).get_json()

    assert not body["success"]
    assert bench["saved"] == []
    assert bench["scans"] == 0


def test_choosing_a_frozen_camera_says_so(client, bench):
    """
    `open_best_camera()` refuses to *save* a frozen device as the default. A
    picker that let somebody choose one without saying so would undo that
    silently - the operator would get a still image and no recognition, with
    nothing obviously wrong.
    """
    sign_in(client)
    bench["cameras"] = [camera(0), camera(2, frozen=True)]

    body = client.post("/cameras/select", data={"index": "2"}).get_json()

    assert body["success"], "an explicit choice is honoured"
    assert bench["saved"] == [2]
    assert "still image" in body["message"].lower()
