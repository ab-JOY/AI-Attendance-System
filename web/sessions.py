"""
Attendance sessions: starting one, watching it, ending it, correcting it.

This blueprint is where the web layer meets the recognition engine. It owns no
recognition logic - that is `recognize_face.py` and `vision/` - and no SQL -
that is `repositories/attendance.py`. What it owns is the order things happen
in and what the operator is told.
"""

from __future__ import annotations

import logging
from datetime import date

import mysql.connector
from flask import (
    Blueprint,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from camera_utils import describe_available_cameras, save_camera_index
from config.settings import settings as app_config
from infra.camera import BrowserCamera
from infra.db import db_cursor
from infra.uploads import UploadRejected, decode_frame
from recognize_face import (
    ActiveSubject,
    ServerCameraBusy,
    SessionBusy,
    SessionLimitReached,
    SessionNotRunning,
    latest_upload_overlays,
    open_video_stream,
    start_camera,
    start_upload_recognition,
    stop_camera,
)

# One recognition session per running class (todo.md §19 step 9). Named away
# from `session`, which is Flask's request-scoped session in this module - the
# import-shadowing trap lessons.md L5 records.
from recognize_face import sessions as recognition_sessions
from repositories import attendance as attendance_repo
from repositories import enrolments as enrolments_repo
from repositories import subjects as subjects_repo
from security.access import authenticated, instructor_scope, json_api
from services import attendance as attendance_service
from services import schedule
from vision.pose import (
    MIN_NATIVE_FRAME_WIDTH,
    RECOGNITION_FRAME_HEIGHT,
    RECOGNITION_FRAME_WIDTH,
)
from web.errors import error_page

logger = logging.getLogger(__name__)

sessions_bp = Blueprint("sessions", __name__)

# Where a session's frames come from. "server" is the classroom camera, opened
# by OpenCV on this machine - the only source there was until the system was
# put on the internet. "browser" is the webcam of whichever device started the
# session, uploaded frame by frame. Recognition is the same code either way.
CAMERA_SOURCES = ("server", "browser")

# Flask-session key holding the session_id of the browser-camera session this
# login started. The upload route compares against it instead of re-running
# `_may_use_subject()` - a database query - for every one of ~8 frames a
# second, and it means only the browser that started a session can feed it.
BROWSER_CAMERA_KEY = "browser_camera_session_id"

# Flask-session key holding the subject_id of the session this login started
# (todo.md §19 step 9). With several classes running, "the running session"
# no longer means anything on its own; for this page it means this one.
OPERATING_SUBJECT_KEY = "operating_subject_id"


# The three statuses this system produces. Deliberately not free text: the
# register is aggregated by exact string match in /reports and in the export,
# so a typed "present" would be a fourth category nothing counts (this is FS-7
# one layer down).
CORRECTABLE_STATUSES = ("Present", "Late", "Absent")

# `attendance_audit.reason` is VARCHAR(255). Checked here rather than left to
# the column, because this server has no STRICT_TRANS_TABLES: an over-long
# reason is silently truncated and the correction reports success with a
# mangled record of itself. See tasks/lessons.md L11 - the same non-strict
# behaviour that turned a NULL into a 0 and cost two migration drafts.
MAX_CORRECTION_REASON = 255


def _all_subjects():
    """
    The subject pickers, each carrying how many students are on its class list.

    The count is what lets the dropdown mark an offering on which a session
    would record nobody (FS-3/B3) *before* one is chosen, rather than after a
    student has been refused in front of the camera.
    """
    with db_cursor(dictionary=True) as cursor:
        # DM-5: an instructor is offered their own offerings. None for an
        # administrator, which is every offering.
        return subjects_repo.for_selection_with_class_list_size(
            cursor, instructor_id=instructor_scope()
        )


# What an instructor is told when a request names an offering that is not
# theirs (DM-5). The dropdowns never offer one, so reaching this means the
# offering was reassigned after the page loaded, or the request was made by
# hand.
NOT_YOUR_SUBJECT = (
    "That subject is not assigned to you. Ask an administrator to assign it "
    "under Subjects if it should be."
)


def _may_use_subject(subject_id):
    """
    Whether the signed-in user may run or end a session on this offering.

    Always True for an administrator. For an instructor, only an offering
    assigned to their account.

    ⚠️ **This is the guard; the filtered dropdown is the convenience.** A
    control that does not offer another instructor's class is absent from a
    curl and a replayed POST - the same relationship the End control's lock
    has with the 409 behind it.

    ⚠️ **Refuses on a database error, unlike `_class_list_is_empty()`.** That
    gate protects the operator's time and must not close on a blip. This one
    decides whose register gets written, and "could not check" is not "yes".
    A real outage stops the session one line later either way.
    """
    scope = instructor_scope()

    if scope is None:
        return True

    try:
        with db_cursor(dictionary=True) as cursor:
            return subjects_repo.is_taught_by(cursor, subject_id, scope)

    except mysql.connector.Error:
        logger.exception(
            "Could not check who teaches subject_id %s; refusing", subject_id
        )
        return False


# Why a session on a subject with an empty class list is refused (B3). Module
# scope so the route and its test cannot drift about the wording, the same way
# SKIPPED_MARKER is shared in train_model.
#
# ⚠️ **This is a refusal, not a warning, and that was the user's call on
# 2026-08-30.** It was built as a warning first, on the reasoning that an empty
# class list is a roster nobody has filled in yet and blocking would obstruct
# whoever is setting the class up. The user overruled it, and the reasoning
# holds: FS-3 means *every* face is refused with "Not in this class" and
# `/end-attendance` writes a register of nobody, so the session cannot produce
# a single useful row. Letting it start spends the operator's time, the
# students' time and a camera slot on an outcome that is knowable in one query
# before any of it is committed.
EMPTY_CLASS_LIST_REFUSAL = (
    "Nobody is on this subject's class list, so this session could not record "
    "anyone - every face would be refused with \"Not in this class\"."
)

# The remedy, which is a different sentence depending on who is reading it.
#
# Administrators and instructors can both open the Class List screen - an
# instructor only for their own subjects, which is the only kind they can
# start a session on (`_may_use_subject` runs first). They reach it from
# different pages, so the directions differ. Any other role is told who can
# fix it and gets no button: a button that leads to a 403 reads as the system
# being broken.
EMPTY_CLASS_LIST_REMEDY_ADMIN = (
    " Add students under Subjects → Class List, then start the session again."
)

EMPTY_CLASS_LIST_REMEDY_INSTRUCTOR = (
    " Add students under My Classes → Class List, then start the session "
    "again."
)

EMPTY_CLASS_LIST_REMEDY_OTHER = (
    " Ask an administrator to add students to this class list, then start the "
    "session again."
)

EMPTY_CLASS_LIST_REMEDIES = {
    'admin': EMPTY_CLASS_LIST_REMEDY_ADMIN,
    'instructor': EMPTY_CLASS_LIST_REMEDY_INSTRUCTOR,
}


def _class_list_is_empty(subject_id):
    """
    True when nobody is enrolled on this offering, as far as we can tell.

    ⚠️ **False on a database error, deliberately** — see the caller. This is a
    gate, and a gate that closes when it cannot see is worse than the thing it
    guards against: a momentary blip would become a class unable to take
    attendance at all.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            return enrolments_repo.count_for_subject(cursor, subject_id) == 0

    except mysql.connector.Error:
        logger.exception(
            "Could not check the class list for subject_id %s; allowing the "
            "start rather than refusing on an answer we do not have.",
            subject_id,
        )
        return False


def _today():
    """Today's date on the server's clock. A function so tests can set it."""
    return date.today()


def _wrong_day_refusal(subject_id):
    """
    The refusal message when the subject does not meet today, else None.

    A Monday-and-Wednesday subject started on a Friday used to run, and
    `/end-attendance` then wrote its whole class list Absent for a class that
    never met (user-reported, 2026-10-17).

    ⚠️ **None on a database error**, for the same reason as
    `_class_list_is_empty()`: a gate that closes when it cannot see turns a
    blip into a class that cannot take attendance. A subject whose days cannot
    be read is not refused either - see `schedule.meets_on()`.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            stored = subjects_repo.meeting_day(cursor, subject_id)

    except mysql.connector.Error:
        logger.exception(
            "Could not read the meeting days of subject_id %s; allowing the "
            "start rather than refusing on an answer we do not have.",
            subject_id,
        )
        return None

    today = _today()

    if schedule.meets_on(stored, today):
        return None

    return (
        f"This subject meets on {stored}, and today is "
        f"{today.strftime('%A')}. Start it on one of its days. For a make-up "
        "class, an administrator can add today under Subjects → Edit."
    )


def _class_list_action(subject_id):
    """
    The "Open Class List" button for the empty-class-list dialog, or None.

    None for any role that `subjects.subject_enrolments` would refuse: a
    button that leads to a 403 turns a fixable roster problem into what looks
    like a broken system.
    """
    if session.get('role') not in EMPTY_CLASS_LIST_REMEDIES:
        return None

    return {
        "label": "Open Class List",
        "url": url_for('subjects.subject_enrolments', subject_id=subject_id),
    }


def _running_subject_id():
    """
    The subject of the session this login is running, or None.

    What the End form's dropdown is locked to (R3/FS-16). With several classes
    running (todo.md §19 step 9) that is *this login's* class: the one it
    started; failing that - the cookie lost by signing in again, or an
    administrator who started nothing - the only running class this login may
    use. With one class running that is exactly the old behaviour; with
    several and no way to tell, None, and the page asks rather than guesses.
    """
    usable = [
        running.subject.subject_id
        for running in recognition_sessions.running()
        if running.subject is not None
        and _may_use_subject(running.subject.subject_id)
    ]

    operating = session.get(OPERATING_SUBJECT_KEY)

    if operating in usable:
        return operating

    if len(usable) == 1:
        return usable[0]

    return None


def _session_for_request():
    """
    The running session a /attendance/live or /video_feed request means.

    `?subject_id=` names it - checked against `_may_use_subject()`, so an
    instructor cannot watch another instructor's class. Without it, this
    login's own running session, which is what the attendance page wants.
    """
    requested = request.args.get('subject_id', '').strip()

    if requested.isdigit():
        subject_id = int(requested)

        if not _may_use_subject(subject_id):
            return None
    else:
        subject_id = _running_subject_id()

    if subject_id is None:
        return None

    return recognition_sessions.get(subject_id)


@sessions_bp.route('/attendance')
@authenticated
def attendance():
    # FS-7/US-4. The subject was typed by hand, twice - once to start a session
    # and again to end it - and nothing checked either against the subjects
    # table, so a typo silently created a session no report could find. It is a
    # dropdown now, which is also what makes subject_id available: a typed code
    # cannot identify an offering, because two sections share one code.
    return _render_attendance_page(
        subjects=_all_subjects(),
        running_subject_id=_running_subject_id(),
    )


def _render_attendance_page(**context):
    """
    attendance.html, always with the browser camera's settings.

    ⚠️ **Both routes that render this page go through here, and must.** The
    page reads these three from data attributes. /end-attendance used to call
    render_template() itself without them, so after End the page asked the
    webcam for an ideal width of 0 - which the browser answers with the
    camera's *smallest* mode (640x480 on the user's laptop, 96x96 for a test
    camera) - and its minimum-width check compared against 0 and passed. Every
    frame of the next session was then refused as too narrow. Reported as
    "it works, but after End and Start again it says the camera is only 640
    pixels wide".
    """
    return render_template(
        'attendance.html',
        # The browser camera sends frames at this size, so the upload is no
        # larger than recognition will use. From here rather than restated in
        # JavaScript, where a duplicated constant drifts.
        recognition_width=RECOGNITION_FRAME_WIDTH,
        recognition_height=RECOGNITION_FRAME_HEIGHT,
        # What /attendance/frame refuses below, so the page can refuse a
        # too-small webcam at Start rather than after the session has begun.
        min_frame_width=MIN_NATIVE_FRAME_WIDTH,
        **context,
    )


@sessions_bp.route('/start-attendance', methods=['POST'])
@authenticated
@json_api
def start_attendance():
    """Open an attendance session: a database row, then the camera."""
    subject_id = request.form.get("subject_id", "").strip()

    # Absent means the classroom camera, which is what every caller sent
    # before there was a choice.
    camera_source = request.form.get("camera", "server").strip()

    if camera_source not in CAMERA_SOURCES:
        return jsonify({
            "success": False,
            "message": "Choose which camera the session should use."
        })

    if not subject_id.isdigit():
        return jsonify({
            "success": False,
            "message": "Choose a subject from the list."
        })

    subject_id = int(subject_id)

    if not _may_use_subject(subject_id):
        logger.warning(
            "Refused to start attendance: %r is not the instructor of "
            "subject_id %s",
            session.get('user'), subject_id,
        )

        return jsonify({"success": False, "message": NOT_YOUR_SUBJECT})

    # Before a session row exists, like B3 below: a refusal leaves nothing to
    # undo, and no Absent row is ever written for a class that did not meet.
    wrong_day = _wrong_day_refusal(subject_id)

    if wrong_day is not None:
        logger.warning(
            "Refused to start attendance for subject_id %s: %s",
            subject_id, wrong_day,
        )

        return jsonify({"success": False, "message": wrong_day})

    # B3. Checked **first**, before a session row exists and before the camera
    # is touched, so a refusal costs nothing and leaves nothing behind. The
    # other two refusal paths below have to undo an `attendance_sessions` row
    # they already opened; this one never opens it.
    #
    # ⚠️ A failed check does **not** refuse. The gate is only as good as the
    # answer, and "the database did not respond" is not an answer - refusing on
    # it would turn a momentary blip into a class that cannot take attendance,
    # which is the failure this gate exists to be cheaper than. `open_session()`
    # is the next line and needs the same database, so a genuine outage still
    # stops the session, with its own message.
    if _class_list_is_empty(subject_id):
        logger.warning(
            "Refused to start attendance for subject_id %s: its class list is "
            "empty, so no face could have been recorded.",
            subject_id,
        )

        action = _class_list_action(subject_id)

        return jsonify({
            "success": False,
            "message": EMPTY_CLASS_LIST_REFUSAL + EMPTY_CLASS_LIST_REMEDIES.get(
                session.get('role'), EMPTY_CLASS_LIST_REMEDY_OTHER
            ),
            # The way out of the refusal, built here rather than in the
            # browser. US-8: a URL assembled in JavaScript is a route reference
            # that does not move with the route, and this one is
            # parameterised - `/subject_enrolments/<id>` - so the alternative
            # is a template string with an id spliced into it.
            #
            # ⚠️ **None for a role the Class List screen refuses**, because a
            # button that leads to 403 is worse than no button. An instructor
            # gets it: this subject passed `_may_use_subject`, so it is theirs.
            "action": action,
        })

    subject_row, session_row_id = attendance_service.open_session(
        subject_id, session.get('user')
    )

    if session_row_id is None:
        return jsonify({
            "success": False,
            "message": (
                "That subject no longer exists."
                if subject_row is None
                else "The attendance session could not be started."
            )
        })

    active = ActiveSubject(
        subject_id=subject_id,
        session_id=session_row_id,
        subject_code=subject_row['subject_code'],
    )

    logger.info(
        "Start attendance requested for %s (subject_id %s, session %s)",
        active.subject_code, subject_id, session_row_id,
    )

    try:
        if camera_source == "browser":
            started = start_camera(active, camera=BrowserCamera())
        else:
            started = start_camera(active)

    except (SessionLimitReached, ServerCameraBusy) as refusal:
        # Several classes run at once now (todo.md §19 step 9); these are the
        # two things that still refuse a Start. Not errors: said as they are.
        attendance_service.close_session(session_row_id)
        logger.warning(
            "Refused to start attendance for %s: %s", active.subject_code, refusal
        )

        return jsonify({
            "success": False,
            "message": (
                f"{refusal} End one first, or try again when a class finishes."
                if isinstance(refusal, SessionLimitReached)
                else f"{refusal} Choose \"This device's camera\" to use the "
                "camera of the computer in front of you."
            ),
        })

    except Exception:
        logger.exception("Camera error while starting attendance")
        attendance_service.close_session(session_row_id)

        # SE-10: the exception text used to go straight to the browser.
        return jsonify({
            "success": False,
            "message": "The camera could not be started."
        })

    if not started:
        # Refused because *this subject's* session is already running (other
        # classes may run beside it). The row just opened would otherwise sit
        # forever with no end.
        attendance_service.close_session(session_row_id)

        return jsonify({
            "success": False,
            "message": "This subject's attendance session is already running."
        })

    running = recognition_sessions.get(subject_id)

    if running is None:
        # Started and already gone - its camera failed at once. Nothing to
        # keep the row open for.
        attendance_service.close_session(session_row_id)

        return jsonify({
            "success": False,
            "message": "The camera could not be started."
        })

    # `start()` answers True without starting anything when this subject is
    # already running - with whichever camera it was started on. Asking for
    # the other one must not look like success: a browser told "running"
    # would upload into a session that refuses its frames.
    if (camera_source == "browser") != running.uses_browser_camera:
        attendance_service.close_session(session_row_id)

        return jsonify({
            "success": False,
            "message": (
                "This subject's session is already running with the "
                + ("classroom camera" if camera_source == "browser"
                   else "camera of another device")
                + ". End it first to switch cameras."
            )
        })

    session[OPERATING_SUBJECT_KEY] = subject_id

    if camera_source == "browser":
        session[BROWSER_CAMERA_KEY] = session_row_id
        # Recognition for uploads runs on the server's own thread; the page
        # does not open /video_feed in this mode (see recognize_face.py).
        start_upload_recognition(running)
    else:
        session.pop(BROWSER_CAMERA_KEY, None)

    return jsonify({
        "success": True,
        "session_id": session_row_id,
        "subject_id": subject_id,
        "camera": camera_source,
    })


@sessions_bp.route('/attendance/frame', methods=['POST'])
@authenticated
@json_api
def attendance_frame():
    """
    One JPEG from the browser camera of the running session.

    Answers as soon as the frame is handed over - recognition runs on its own
    thread - with the **latest results** (`faces`): boxes in the 1280x720
    recognition frame, names, statuses and colours, which the page draws over
    its own camera picture. They are for the previous frame recognition
    finished, not this one; at a few frames a second that is a fraction of a
    second behind. `faces` is null until the first frame has been judged.

    409 when there is nothing to feed: no session, a classroom-camera session,
    or a session this login did not start. The page stops uploading on a 409.
    """
    # The session this browser started (todo.md §19 step 9: there can be
    # several). Matched on the attendance_sessions row, which is unique per
    # Start - so only the browser that started a class can feed it.
    feeding = session.get(BROWSER_CAMERA_KEY)
    running = (
        None if feeding is None else recognition_sessions.find(
            lambda candidate: candidate.uses_browser_camera
            and getattr(candidate.subject, "session_id", None) == feeding
        )
    )

    if running is None:
        return jsonify({
            "success": False,
            "message": "No session is waiting for this device's camera.",
        }), 409

    try:
        frame = decode_frame(
            request.get_data(),
            request.content_type,
            size=(RECOGNITION_FRAME_WIDTH, RECOGNITION_FRAME_HEIGHT),
        )
    except UploadRejected as error:
        # Safe to relay: infra/uploads.py's messages describe the picture.
        return jsonify({"success": False, "message": str(error)}), 400

    if not running.submit_frame(frame):
        # The session ended between the check above and now.
        return jsonify({
            "success": False,
            "message": "The session has ended.",
        }), 409

    return jsonify({"success": True, "faces": latest_upload_overlays(running)})


def _may_correct(record):
    """
    Whether the signed-in user may see and correct this register row (DM-5).

    An instructor corrects their own classes. `record` carries the offering's
    `instructor_id` from the join in `attendance_repo.record()`.
    """
    scope = instructor_scope()

    return scope is None or record.get('instructor_id') == scope


@sessions_bp.route('/stop_camera', methods=['POST'])
@authenticated
@json_api
def stop_camera_route():
    """
    Release the camera without ending the session in the database.

    ⚠️ **The attendance page deliberately no longer calls this before ending a
    session, and must not be wired to again (FS-16).** `end_attendance()` reads
    the subject to check the form against from the recognition session, and
    `RecognitionSession.stop()` sets that subject to None - so a pre-flight
    stop guaranteed the guard saw no running session and trusted the dropdown
    instead. The register was then written for the wrong subject with
    `session_id` NULL, and the real session's row was never closed.
    `/end-attendance` stops the camera itself, after the guard has run.

    What remains is an operator escape hatch: free the device without writing
    an absent register.

    ⚠️ **This login's class only** (todo.md §19 step 9). With one session it
    freed "the" camera; with several, stopping them all would end other
    instructors' classes from one button.
    """
    subject_id = _running_subject_id()

    if subject_id is not None:
        stop_camera(subject_id)

    return jsonify({"success": True})


@sessions_bp.route('/end-attendance', methods=['POST'])
@authenticated
def end_attendance():
    """
    End the session and write down who was absent (FS-3, FS-4).

    Two defects meet here.

    FS-4: "Absent" was computed in memory and rendered, never stored, so
    /reports counted Absent rows from a table that could not contain any and
    reported 0 every time. Two screens, contradictory numbers, same session.

    FS-3: the absentees were *every student in the database*, because there was
    no student-to-subject relation to scope them by. With CS401 selected, an
    unrelated test student was reported absent for it.

    Both are one query now: the students enrolled in this offering who have no
    attendance row for today.

    ⚠️ **The running session decides the subject, not the form (R3).** The End
    form carries its own dropdown, defaulting to "Choose a subject…", and the
    route used to trust it. Pick a different offering than the one running and
    the absent register was written for *that* offering, each row stamped with
    the *running* session's `session_id` - which belongs to a different
    subject. Nothing errored: the foreign key to `attendance_sessions`
    resolves, so the result is a referentially valid register that is factually
    wrong. Students marked absent from a class that was never held, attributed
    to another subject's session. Then the running session was closed anyway.

    The template comment said a dropdown meant "the two can no longer disagree
    by a typo". True, and they could still disagree by a mis-selection, which
    nothing checked. FS-7 was about the value being unverifiable; this is the
    same finding one level up - two independent sources of one fact, never
    reconciled.

    ⚠️ **FS-16: that guard was dead in the browser for two sprints.** It reads
    the subject from the *in-memory* recognition session, and
    `static/js/attendance.js` POSTed `/stop_camera` before submitting the form.
    `RecognitionSession.stop()` sets the subject to None, so `active` was
    always None here, the comparison was skipped, the form was trusted, and the
    mis-selection went through exactly as before - HTTP 200, the absent
    register written for the class that was not taught, `session_id` NULL, and
    the real session's row left open so the dashboard counted it active all
    day. Every test in `tests/test_end_attendance_subject.py` passed throughout,
    because none of them used the browser's order.

    **So there are two checks now, and the second is the load-bearing one.**
    The in-memory check stays because it can refuse *before* the camera is
    stopped, which keeps a mis-selection free. But the authority is
    `attendance_sessions`: a row written before the camera opened, which
    survives a stop, a restart and a different operator's browser. A client
    cannot talk the route out of it by choosing what to POST first.

    **Several classes at once (todo.md §19 step 9).** The session ended is the
    one *for the submitted subject*, so ending one class never touches
    another. The R3 refusal is now about this login: while its own class is
    running, a form naming a subject with no running session is the
    mis-selection R3 is, and is refused exactly as before.
    """
    submitted = request.form.get('subject_id', '').strip()

    if not submitted.isdigit():
        return error_page(400, "Choose a subject from the list.")

    submitted_id = int(submitted)

    # DM-5. Before anything else, and before stop_camera(): an instructor may
    # end only their own class, and a refusal must leave whatever is running
    # running - the same reasoning as the mismatch refusal just below.
    if not _may_use_subject(submitted_id):
        logger.warning(
            "Refused to end attendance: %r is not the instructor of "
            "subject_id %s",
            session.get('user'), submitted_id,
        )

        return error_page(403, NOT_YOUR_SUBJECT)

    target = recognition_sessions.get(submitted_id)
    active = target.subject if target is not None else None

    operating = _running_subject_id()
    own = (
        recognition_sessions.get(operating)
        if active is None and operating is not None
        and operating != submitted_id
        else None
    )

    if own is not None:
        # ⚠️ Refused *before* stop_camera() and before the transaction. Nothing
        # is written and the session is still running, so the operator can
        # correct the dropdown and end the right class. Closing the session on
        # a refusal would make the mistake unrecoverable - the register the
        # session was for could then never be written at all.
        own_code = own.subject.subject_code

        logger.warning(
            "Refused to end attendance: %s is running, but subject_id %s was "
            "submitted",
            own_code, submitted_id,
        )

        return error_page(
            409,
            f"The running session is for {own_code}. The form "
            "asked to end a different subject, so nothing was recorded and "
            "the session is still running. Choose "
            f"{own_code} and try again.",
        )

    subject_id = active.subject_id if active is not None else submitted_id
    session_row_id = active.session_id if active is not None else None

    stop_camera(subject_id)

    try:
        # ⚠️ One block, so the absent register and the session's end time
        # commit together. db_cursor() is one transaction per `with`.
        with db_cursor(dictionary=True, commit=True) as cursor:
            if active is None:
                # The server has forgotten which subject this was - a restart,
                # a second End on the same class, or the browser having stopped
                # the camera before submitting (FS-16). The form is *not* the
                # only source left: `attendance_sessions` still holds the row
                # written before the camera opened.
                # Rows of classes running *now* belong to other operators'
                # live sessions (todo.md §19 step 9) and say nothing about
                # this form. Only a forgotten one - open in the database, gone
                # from memory - is evidence of what this End was meant for.
                running_ids = {
                    running.subject.subject_id
                    for running in recognition_sessions.running()
                }
                open_rows = [
                    row for row in attendance_repo.open_sessions_today(cursor)
                    if row['subject_id'] not in running_ids
                ]

                mine = next(
                    (
                        row for row in open_rows
                        if row['subject_id'] == submitted_id
                    ),
                    None,
                )

                if mine is not None:
                    # ⚠️ The register is stamped with the real session id
                    # rather than NULL, and *that* session is the one closed.
                    # Before this, ending after a restart wrote orphan rows and
                    # left the row open, so the dashboard counted a class that
                    # had finished as active for the rest of the day.
                    subject_id = submitted_id
                    session_row_id = mine['id']

                elif open_rows and any(
                    _may_use_subject(row['subject_id']) for row in open_rows
                ):
                    open_rows = [
                        row for row in open_rows
                        if _may_use_subject(row['subject_id'])
                    ]

                    # Something is open and it is not what the form named.
                    # Refusing is the whole point of FS-16: nothing is written,
                    # and the operator is told which class is actually waiting
                    # to be ended.
                    other = subjects_repo.code_of(
                        cursor, open_rows[0]['subject_id']
                    )

                    open_code = (
                        other['subject_code'] if other else
                        f"subject {open_rows[0]['subject_id']}"
                    )

                    logger.warning(
                        "Refused to end attendance: session %s for %s is open, "
                        "but subject_id %s was submitted",
                        open_rows[0]['id'], open_code, submitted_id,
                    )

                    return error_page(
                        409,
                        f"A session for {open_code} is still open and the form "
                        "asked to end a different subject, so nothing was "
                        f"recorded. Choose {open_code} and try again.",
                    )

                # No open session at all: nothing to contradict the form, and
                # the register still has to be writable. The rows carry no
                # session_id, which is honest rather than a guess.

            subject_row = subjects_repo.code_of(cursor, subject_id)

            if subject_row is None:
                return error_page(404, "That subject no longer exists.")

            register = attendance_repo.register_for(cursor, subject_id)

            absentees = [row for row in register if row['status'] is None]

            if absentees:
                attendance_repo.insert_absences(
                    cursor,
                    [
                        (row['student_id'], subject_id, session_row_id)
                        for row in absentees
                    ],
                )

                logger.info(
                    "Recorded %d absence(s) for %s",
                    len(absentees), subject_row['subject_code'],
                )

            if session_row_id is not None:
                attendance_repo.close_session(cursor, session_row_id)

        session_results = [
            {
                "student_id": row['student_id'],
                "name": row['name'],
                "status": row['status'] or "Absent",
                "time_in": row['time_in'],
            }
            for row in register
        ]

        total_present = sum(
            1 for row in session_results if row['status'] != "Absent"
        )

        return _render_attendance_page(
            subjects=_all_subjects(),
            # Always None here in practice - the session has just been stopped -
            # but computed rather than hardcoded, so the control unlocks because
            # nothing is running rather than because this branch says so.
            running_subject_id=_running_subject_id(),
            session_results=session_results,
            total_students=len(session_results),
            total_present=total_present,
            total_absent=len(session_results) - total_present,
            selected_subject=subject_row['subject_code'],
            selected_subject_id=subject_id,
        )

    except mysql.connector.Error:
        logger.exception("End attendance failed: database error")
        return error_page(500)


# The number of device indices a scan checks. Matches camera_utils' own
# default; named here because both routes below quote it to the operator.
CAMERA_SCAN_DEPTH = 5


@sessions_bp.route('/cameras')
@authenticated
@json_api
def list_cameras():
    """
    Which cameras this machine has, so the operator can pick one.

    ⚠️ **This is the *server's* camera, not the browser's.** Enrolment runs on
    `getUserMedia` and the browser enumerates its own devices; recognition
    opens the classroom camera with OpenCV on the server, which the browser
    cannot see at all. So the two pages need two different pickers, and this
    is the server one.

    Reported from a UAT as "a webcam and a laptop cam are both available but
    the app defaults to the laptop cam". It did: `open_best_camera()` scans
    upward from index 0 and takes the first device that produces a picture,
    which is the built-in. That is the right default and the wrong *only*
    option - there was no way to see that a second camera existed, and the
    saved preference `selected_camera.txt` could only be written by the scan
    itself.

    ⚠️ **Refused while a session is running.** Probing opens each device in
    turn, and index by index it would collide with the camera the running
    session is holding - at best a slow scan, at worst taking the picture away
    from a live attendance session. Refusing with a reason beats a scan that
    quietly returns the wrong answer.
    """
    # Only a session on the server's own camera holds it; classes running on
    # their browsers' cameras do not (todo.md §19 step 9).
    if recognition_sessions.server_camera_in_use():
        return jsonify({
            "success": False,
            "cameras": [],
            "message": (
                "Cameras cannot be scanned while a session is running - the "
                "scan would take the picture away from it. End the session "
                "first."
            ),
        })

    try:
        cameras = describe_available_cameras(max_tested=CAMERA_SCAN_DEPTH)

    except Exception:
        # A camera scan touches hardware and DirectShow is not shy about
        # raising. The page must still load.
        logger.exception("Camera scan failed")
        return jsonify({
            "success": False,
            "cameras": [],
            "message": "The cameras could not be listed.",
        })

    return jsonify({
        "success": True,
        "cameras": cameras,
        "checked": CAMERA_SCAN_DEPTH,
    })


@sessions_bp.route('/cameras/select', methods=['POST'])
@authenticated
@json_api
def select_camera():
    """
    Remember which camera the next session should open.

    Writes through `save_camera_index()`, which is the same file
    `open_best_camera()` already consults second, after an explicit argument
    and before the scan. So nothing needs plumbing into `session.start()`:
    the preference is picked up on the next start by machinery that has always
    been there.

    ⚠️ **The index is validated by opening it, not by parsing it.** An index
    that is merely an integer proves nothing - CAM-1 is the finding that a
    camera can open, read, and return black - and `save_camera_index()`'s
    docstring states the invariant this route has to keep: it is only ever
    called with an index that has passed `probe_camera()`. Rescanning is the
    only honest way to know that, and it also catches the device having been
    unplugged since the page was loaded.
    """
    raw_index = request.form.get("index", "").strip()

    if not raw_index.isdigit():
        return jsonify({
            "success": False,
            "message": "Choose a camera from the list.",
        })

    index = int(raw_index)

    # Only a session on the server's own camera holds it; classes running on
    # their browsers' cameras do not (todo.md §19 step 9).
    if recognition_sessions.server_camera_in_use():
        return jsonify({
            "success": False,
            "message": (
                "The camera cannot be changed while a session is running. "
                "End the session first."
            ),
        })

    try:
        usable = describe_available_cameras(max_tested=CAMERA_SCAN_DEPTH)

    except Exception:
        logger.exception("Camera scan failed while selecting camera %s", index)
        return jsonify({
            "success": False,
            "message": "The camera could not be checked.",
        })

    chosen = next((c for c in usable if c["index"] == index), None)

    if chosen is None:
        logger.warning(
            "Refused camera %s: it is not producing a picture now", index
        )
        return jsonify({
            "success": False,
            "message": (
                f"Camera {index} is not producing a picture. Check that "
                "nothing else is using it."
            ),
            "cameras": usable,
        })

    save_camera_index(index)

    logger.info("Camera %s chosen for the next session", index)

    # CAM-2: a connected webcam is opened ahead of the saved choice, so saying
    # "will be used" about anything else would be untrue while one is in.
    webcam = next(
        (c for c in usable if c.get("external") and c["index"] != index), None
    )

    if webcam is not None and not chosen.get("external"):
        return jsonify({
            "success": True,
            "message": (
                f"Saved camera {index}. While the webcam "
                f"{webcam.get('name') or webcam['index']} is connected it is "
                f"used first; camera {index} will be used when it is unplugged."
            ),
            "cameras": usable,
        })

    return jsonify({
        "success": True,
        "message": (
            f"Camera {index} will be used for the next session."
            + (
                " ⚠️ It looks like a still image rather than a live feed."
                if chosen["frozen"]
                else ""
            )
        ),
        "cameras": usable,
    })


@sessions_bp.route('/attendance/live')
@authenticated
@json_api
def attendance_live():
    """
    Who has been recorded so far in the running session (US-3).

    The operator had to *end the session* to find out whether anybody was
    recognised, which meant a session that was silently recording nothing -
    FS-14, where every frame agreed on the right student and nothing was ever
    written - looked exactly like a session where nobody had walked past yet.

    ⚠️ **@json_api as well as @authenticated.** Without the JSON marker an
    expired session hands this poller an HTML login page to `JSON.parse`, and
    the operator sees a syntax error instead of "please sign in again". The
    same pairing as /train_status, for the same reason.

    The confirmed identities come from the recognition session, not from the
    register: the question being asked is "is the camera doing anything?", and
    a row written by an earlier session today would answer it wrongly.
    """
    # This login's class, or the one `?subject_id=` names (todo.md §19 step 9).
    running = _session_for_request()
    active = running.subject if running is not None else None

    if running is None or active is None:
        return jsonify({"running": False, "students": []})

    # ⚠️ `recognized_ids` is a method, not a property, and it is called here
    # deliberately: `sorted(running.recognized_ids)` reads as
    # working code, raises `TypeError: 'method' object is not iterable`, and is
    # unreachable until a session is actually running - which is the state no
    # test constructed. It answered 500 on every poll, in every session, from
    # the day this route was written. tests/test_attendance_live.py now stubs a
    # running session so the state is covered.
    student_ids = sorted(running.recognized_ids())

    # "browser" only for the login that started it: that is the page which
    # should resume uploading after a reload. Anyone else is watching.
    camera = (
        "browser"
        if running.uses_browser_camera
        and session.get(BROWSER_CAMERA_KEY) == active.session_id
        else "server"
    )

    try:
        with db_cursor(dictionary=True) as cursor:
            rows = attendance_repo.recognised_today(
                cursor, active.subject_id, student_ids
            )

    except mysql.connector.Error:
        # A failed lookup must not stop the poll. The count is still true and
        # the page keeps showing that the session is alive.
        logger.exception("Could not read the live recognised list")
        return jsonify({
            "running": True,
            "camera": camera,
            "students": [],
            "recognised": len(student_ids),
        })

    return jsonify({
        "running": True,
        "camera": camera,
        "subject_code": active.subject_code,
        "recognised": len(student_ids),
        "students": [
            {
                "student_id": row['student_id'],
                "name": row['name'],
                "time_in": str(row['time_in']) if row['time_in'] else None,
                "status": row['status'],
            }
            for row in rows
        ],
    })


# SE-2. This was reachable by anyone who could reach the host: the live camera
# feed of a classroom, unauthenticated. It is loaded as an <img src>, so the
# browser sends the session cookie with it and the hook can enforce
# authentication exactly as it does for any other route.
#
# RE-2. Authentication does not *serialise* access, and this route needs both.
# A second tab used to get a second generator walking the same tracker, so two
# streams double-counted identity votes toward one attendance decision - and
# whichever generator exited first released the camera under the other (RE-10).
# The session now allows one viewer, and a second request is refused here
# rather than served, because once the multipart response headers are on the
# wire there is no way left to say no.
@sessions_bp.route('/video_feed')
@authenticated
def video_feed():
    # This login's class, or the one `?subject_id=` names (todo.md §19 step 9).
    running = _session_for_request()

    try:
        if running is None:
            raise SessionNotRunning()

        stream = open_video_stream(running)

    except SessionBusy:
        logger.warning("Refused a second video stream: one is already open")
        return error_page(
            409,
            "The camera is already being viewed in another window. Close it "
            "and try again."
        )

    except SessionNotRunning:
        logger.warning("Video stream requested with no attendance session")
        return error_page(
            409,
            "No attendance session is running. Start one first."
        )

    return Response(
        stream,
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )


# ==============================
# ATTENDANCE CORRECTION (FS-10)
#
# A false negative - the system failing to recognise a student who was in the
# room - could not be corrected. The register was wrong and stayed wrong, and
# the only remedies were editing the database by hand or telling the student
# their attendance does not count.
#
# ⚠️ **The audit trail is the feature; the screen is the interface to it.**
# Migration 006 created `attendance_audit` before any of this existed, and said
# why: an attendance system where a human can silently change a biometric
# determination is worse than one where they cannot change it at all. Every
# correction therefore names who made it, when, what it was before, and why -
# and the write that changes the status and the write that records it are one
# transaction, so there is no path that produces one without the other.
# ==============================


@sessions_bp.route(
    '/attendance/<int:attendance_id>/correct', methods=['GET', 'POST']
)
@authenticated
def correct_attendance(attendance_id):
    """
    Change one attendance record, and record that it was changed (FS-10).

    **@authenticated, not admin-only.** FS-10's premise is that the *instructor*
    cannot correct a false negative, and they are the person who watched it
    happen. This matches /start-attendance and /end-attendance, which are the
    other two routes an instructor needs. The audit trail is what makes it safe
    to open up: nothing here is anonymous.

    ⚠️ **`time_in` is deliberately left alone** - see
    `repositories.attendance.set_status`.
    """
    if request.method == 'GET':
        with db_cursor(dictionary=True) as cursor:
            record = attendance_repo.record(cursor, attendance_id)

            if record is None:
                return error_page(404, "That attendance record no longer exists.")

            if not _may_correct(record):
                # 404, not 403: to an instructor, another instructor's record
                # is not theirs to know exists. The log says what happened.
                logger.warning(
                    "Refused correction screen for record %s to %r: not their "
                    "class",
                    attendance_id, session.get('user'),
                )
                return error_page(404, "That attendance record no longer exists.")

            history = attendance_repo.correction_history(cursor, attendance_id)

        return render_template(
            'correct_attendance.html',
            record=record,
            history=history,
            statuses=CORRECTABLE_STATUSES,
        )

    new_status = request.form.get('status', '').strip()
    reason = request.form.get('reason', '').strip()

    if new_status not in CORRECTABLE_STATUSES:
        return error_page(400, "Choose a status from the list.")

    if not reason:
        return error_page(
            400,
            "Give a reason for the correction. It is kept with the record.",
        )

    if len(reason) > MAX_CORRECTION_REASON:
        return error_page(
            400,
            f"Keep the reason to {MAX_CORRECTION_REASON} characters or fewer.",
        )

    try:
        # ⚠️ One db_cursor block is one transaction (the pool's connections are
        # not in autocommit), so the status change and its audit row commit
        # together or not at all. A correction that is not recorded must not be
        # possible, and this is what makes that structural rather than a thing
        # the next person has to remember. Splitting these two calls into two
        # blocks leaves both working and silently removes the property;
        # tests/test_attendance_correction.py fails if they are separated.
        with db_cursor(dictionary=True, commit=True) as cursor:
            existing = attendance_repo.lock_for_correction(cursor, attendance_id)

            if existing is None:
                return error_page(404, "That attendance record no longer exists.")

            # DM-5. The GET above hides another instructor's record; this is
            # what refuses the POST that names it anyway. Nothing has been
            # written yet, so returning here changes nothing.
            if instructor_scope() is not None and not _may_correct(
                attendance_repo.record(cursor, attendance_id) or {}
            ):
                logger.warning(
                    "Refused correction of record %s by %r: not their class",
                    attendance_id, session.get('user'),
                )
                return error_page(404, "That attendance record no longer exists.")

            old_status = existing['status']

            if old_status == new_status:
                # Nothing to record. An audit row reading "changed Present to
                # Present" is noise in the one register that has to stay
                # readable, and the operator gets told rather than silently
                # given a success.
                return error_page(
                    400,
                    f"That record is already marked {new_status}. "
                    "Nothing was changed.",
                )

            attendance_repo.set_status(cursor, attendance_id, new_status)

            attendance_repo.record_correction(
                cursor,
                attendance_id,
                session.get('user'),
                old_status,
                new_status,
                reason,
            )

        logger.info(
            "Attendance %s corrected from %s to %s by %s",
            attendance_id, old_status, new_status, session.get('user'),
        )

    except mysql.connector.Error:
        logger.exception("Attendance correction failed for record %s", attendance_id)
        return error_page(500)

    # Back to the same screen, which now shows the new status and the
    # correction in the history below it. Post/redirect/get, so a refresh does
    # not offer to apply the correction a second time.
    return redirect(
        url_for('sessions.correct_attendance', attendance_id=attendance_id)
    )
