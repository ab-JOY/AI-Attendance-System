"""
`/video_feed` refuses a second stream instead of serving one (RE-2, RE-10).

`tests/test_recognition_session.py` proves the session allows one viewer.
This file proves the *route* turns that into an HTTP answer, which is a
separate claim and the one that reaches the operator.

**Why the refusal has to happen in the route and not in the generator.** A
generator body does not run until it is first iterated, and Flask iterates it
only after the response headers are on the wire. By then the browser has been
told it is receiving `multipart/x-mixed-replace` and a 409 is no longer
expressible - the tab would sit on a stream that yields nothing, which is
indistinguishable from a camera that has stopped working. So the slot is
claimed in `open_video_stream()`, synchronously, before the `Response` exists.

No camera and no model needed: the session's refusal is simulated at the seam
`app.py` imports, because what is under test here is the mapping from exception
to status code.
"""

from __future__ import annotations

import pytest

from vision.session import SessionBusy, SessionNotRunning


@pytest.fixture(scope="module")
def flask_app():
    import app as app_module

    app_module.app.config["TESTING"] = True
    return app_module.app


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


def sign_in(client):
    """Fabricate a session without touching the database."""
    with client.session_transaction() as session:
        session["user"] = "test-admin"
        session["role"] = "admin"


def test_video_feed_still_requires_authentication(client):
    """SE-2 must not regress while RE-2 is being fixed."""
    response = client.get("/video_feed")

    assert response.status_code in (302, 401, 403)
    assert "multipart" not in response.headers.get("Content-Type", "")


def test_a_second_stream_is_refused_with_409(client, flask_app, monkeypatch):
    """
    The RE-2 symptom, at the layer the operator meets it.

    Two tabs used to mean two generators walking one tracker, double-counting
    identity votes toward a single attendance decision - and whichever
    generator exited first released the shared camera under the other (RE-10).
    """
    import web.sessions as sessions_module

    def already_open():
        raise SessionBusy("the camera stream is already open in another window")

    monkeypatch.setattr(sessions_module, "open_video_stream", already_open)

    sign_in(client)
    response = client.get("/video_feed")

    assert response.status_code == 409
    assert "multipart" not in response.headers.get("Content-Type", "")


def test_streaming_without_a_session_is_refused_with_409(client, monkeypatch):
    """
    Asking for a feed before pressing Start is an operator mistake, not a
    server error. It used to hand back an empty stream, which shows a broken
    image icon and explains nothing.
    """
    import web.sessions as sessions_module

    def not_running():
        raise SessionNotRunning("no attendance session is running")

    monkeypatch.setattr(sessions_module, "open_video_stream", not_running)

    sign_in(client)
    response = client.get("/video_feed")

    assert response.status_code == 409


def test_a_refusal_is_a_readable_page_not_a_raw_error(client, monkeypatch):
    """
    US-1 and SE-10: failures render the error template, and never leak the
    exception text. The message names the fix - close the other window.
    """
    import web.sessions as sessions_module

    monkeypatch.setattr(
        sessions_module,
        "open_video_stream",
        lambda: (_ for _ in ()).throw(SessionBusy("internal detail")),
    )

    sign_in(client)
    response = client.get("/video_feed")
    body = response.get_data(as_text=True)

    assert "internal detail" not in body, "the exception text reached the client"
    assert "another window" in body


def test_a_granted_stream_is_served_as_mjpeg(client, monkeypatch):
    """The success path still produces the multipart response an <img> needs."""
    import web.sessions as sessions_module

    def one_frame():
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8\xff\xd9\r\n"

    monkeypatch.setattr(sessions_module, "open_video_stream", one_frame)

    sign_in(client)
    response = client.get("/video_feed")

    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith(
        "multipart/x-mixed-replace"
    )
