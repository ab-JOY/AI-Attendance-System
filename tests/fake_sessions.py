"""
A `SessionRegistry` stand-in holding fake sessions, for route tests.

Route tests used to swap `web.sessions.recognition_session` - the one session
the server had - for a stub. With one session per running class (todo.md §19
step 9) the routes ask a registry instead; this answers the same questions
from whatever stubs a test hands it, so each test keeps its own stub and
changes one line:

    monkeypatch.setattr(sessions_module, "recognition_sessions", FakeRegistry(fake))
"""

from __future__ import annotations

from vision.session import subject_key


class FakeRegistry:
    def __init__(self, *sessions):
        self.sessions = list(sessions)
        self.stopped = []

    def running(self):
        return [s for s in self.sessions if getattr(s, "is_running", False)]

    def get(self, subject):
        key = subject_key(subject)
        return next(
            (s for s in self.running() if subject_key(s.subject) == key), None
        )

    def find(self, predicate):
        return next((s for s in self.running() if predicate(s)), None)

    def server_camera_in_use(self):
        return any(
            not getattr(s, "uses_browser_camera", False) for s in self.running()
        )

    def stop(self, subject):
        found = self.get(subject)
        self.stopped.append(subject_key(subject))

        if found is None:
            return False

        stop = getattr(found, "stop", None)
        if callable(stop):
            stop()
        return True
