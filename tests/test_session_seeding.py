"""
A session starts knowing who the register already holds (FS-18).

**Reported as "the verification repeats even after a person has already been
verified."** `RecognitionSession.start()` did `self._recognized = set()`
unconditionally and read no attendance rows, so the only thing that ever
populated that set was a write *this camera run* had made.

`TrackState.adopt_completed_identity()` exists precisely to stop a student who
stepped out of frame and back in from re-passing the liveness challenge, and it
reads that set - so it could never fire across a restart. Stop and start
attendance, or restart the process, and a student already Present today redid
the whole 20-frame confirmation window and a fresh randomised challenge, for a
write the UNIQUE index (RE-3) then discarded as a duplicate.

The register lives in the database and this package deliberately has no database
in it, so the read arrives as a `SessionHooks` callable, like the camera and the
model. Every test here uses fakes; nothing reaches MySQL (lessons.md L6).
"""

from __future__ import annotations

import pytest

import recognize_face
from repositories import attendance as attendance_repo
from tests.test_recognition_session import Recorder  # noqa: F401 - fixtures below
from vision.session import RecognitionSession
from vision.tracking import TrackConfig, TrackState

ALICE = "SEC-TEST-NOBODY-1"
BOB = "SEC-TEST-NOBODY-2"
SUBJECT = "TEST-101"


@pytest.fixture
def recorder():
    return Recorder()


def session_with(recorder, already_recorded):
    return RecognitionSession(
        hooks=recorder.hooks(already_recorded=already_recorded),
        config=TrackConfig(),
    )


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def test_a_session_starts_knowing_who_is_already_recorded(recorder):
    """⚠️ **The regression.** This set was empty on every start."""
    session = session_with(
        recorder, lambda _subject: {ALICE: "Present", BOB: "Late"}
    )

    assert session.start(SUBJECT)

    assert session.recognized_ids() == {ALICE, BOB}, (
        "A student the register already holds must not be asked to pass "
        "liveness again - that is the whole purpose of adopt_completed_identity()"
    )


def test_the_hook_is_asked_about_the_subject_being_started(recorder):
    """The register is per subject; seeding the wrong one would be worse than none."""
    asked = []

    def already_recorded(subject):
        asked.append(subject)
        return {}

    session = session_with(recorder, already_recorded)
    session.start(SUBJECT)

    assert asked == [SUBJECT]


def test_a_second_subject_does_not_inherit_the_first_subjects_register(recorder):
    """Each start re-seeds. A stale set would skip verification for the wrong class."""
    registers = {"CS401": {ALICE: "Present"}, "CS402": {BOB: "Late"}}
    session = session_with(recorder, lambda subject: dict(registers[subject]))

    session.start("CS401")
    assert session.recognized_ids() == {ALICE}

    session.stop()
    session.start("CS402")

    assert session.recognized_ids() == {BOB}


def test_no_hook_at_all_starts_empty(recorder):
    """The hook is optional, and its absence must not raise."""
    session = RecognitionSession(hooks=recorder.hooks(), config=TrackConfig())

    assert session.start(SUBJECT)
    assert session.recognized_ids() == set()


# ---------------------------------------------------------------------------
# The read is an optimisation, and must never cost a class
# ---------------------------------------------------------------------------


def test_a_failing_read_does_not_stop_the_session_starting(recorder):
    """
    ⚠️ **The failure mode this must not have.**

    Losing the seed costs a student one liveness challenge they did not need.
    Refusing to start costs the whole class their attendance. A database that
    is down at the moment a lesson begins must produce the first, never the
    second.
    """

    def explode(_subject):
        raise RuntimeError("the database is down")

    session = session_with(recorder, explode)

    assert session.start(SUBJECT), (
        "A failed register read must degrade to an empty set, not to a "
        "session that will not start"
    )
    assert session.recognized_ids() == set()


def test_the_seed_is_copied_not_aliased(recorder):
    """
    The frame loop mutates this mapping through the snapshot. If it were the
    caller's object, a repository cache or a test fixture would be written to.
    """
    register = {ALICE: "Present"}
    session = session_with(recorder, lambda _subject: register)
    session.start(SUBJECT)

    session.mark_recognized(BOB, "Late")

    assert register == {ALICE: "Present"}


# ---------------------------------------------------------------------------
# The status travels with the id (FS-7)
# ---------------------------------------------------------------------------


def test_the_recorded_status_survives_the_seed(recorder):
    """
    ⚠️ **A Late student must not be drawn "Present".**

    `recognized` was a set of ids, so `adopt_completed_identity()` had no
    status to inherit and `recorded_status` fell back to "Present". That was
    honest while the mark could only have been made by a track this process
    never saw; seeding from the register makes the real status available, and
    two screens disagreeing about one session is FS-7.
    """
    session = session_with(recorder, lambda _subject: {ALICE: "Late"})
    session.start(SUBJECT)

    assert session.recorded_status_of(ALICE) == "Late"

    state = TrackState(config=TrackConfig())
    state.adopt_completed_identity(ALICE, "Alice", session.recorded_status_of(ALICE))

    assert state.recorded_status == "Late"


def test_an_unknown_status_still_falls_back_to_present():
    """
    The within-session case is unchanged: a track adopting an identity this
    process marked, with no status to hand, reads "Present" as it always did.
    """
    state = TrackState(config=TrackConfig())
    state.adopt_completed_identity(ALICE, "Alice", None)

    assert state.recorded_status == "Present"


# ---------------------------------------------------------------------------
# The database half: the query, and the hook that carries it
#
# `vision/` gets a callable; this is what `recognize_face` puts in it. Both
# halves need pinning, because a hook that always returns {} and a hook that
# raises are indistinguishable from the session's side - it degrades quietly
# for either, by design - so a broken query would cost the fix silently.
# ---------------------------------------------------------------------------


class ScriptedCursor:
    """Records the SQL and answers `fetchall()` from a fixed list."""

    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows


def test_the_register_query_is_keyed_on_the_subject_and_today():
    """
    ⚠️ **This must agree with the UNIQUE index that suppresses duplicate
    writes** (RE-3), which is on (student_id, subject_id, attendance_date). If
    the two ever disagree about what "already recorded" means, a student is
    either asked to verify twice or skipped when they should not be.
    """
    cursor = ScriptedCursor(
        [
            {"student_id": ALICE, "status": "Present"},
            {"student_id": BOB, "status": "Late"},
        ]
    )

    result = attendance_repo.recorded_today(cursor, 7)

    assert result == {ALICE: "Present", BOB: "Late"}

    sql, params = cursor.queries[0]
    assert "FROM attendance" in sql
    assert "subject_id = %s" in sql
    assert "attendance_date = CURDATE()" in sql
    assert params == (7,)


def test_an_empty_register_is_an_empty_mapping():
    assert attendance_repo.recorded_today(ScriptedCursor([]), 7) == {}


def test_the_hook_swallows_a_database_error(monkeypatch):
    """
    A class must start even when the register cannot be read. The session
    catches too, so this is belt and braces on purpose: the message that names
    what was lost belongs here, where the subject code is in scope.
    """
    import contextlib

    import mysql.connector

    @contextlib.contextmanager
    def exploding_cursor(*_args, **_kwargs):
        raise mysql.connector.Error("the database is down")
        yield  # pragma: no cover - unreachable, keeps this a generator

    monkeypatch.setattr(recognize_face, "db_cursor", exploding_cursor)

    subject = recognize_face.ActiveSubject(
        subject_id=1, session_id=1, subject_code="TEST-101"
    )

    assert recognize_face.already_recorded_today(subject) == {}
