"""
REST API for the React Native mobile app.

Completely separate from the session-based web routes. Authentication is
JWT-based (Bearer token), so the existing CSRF/session machinery does not
apply and the blueprint is exempt from CSRFProtect.

**No existing route, template, or web behaviour is changed by this file.**
It is a new blueprint registered alongside the existing eight, under the
``/api`` prefix.

⚠️ **Students only, no registration, once a day** (2026-10-22, the user's
decisions):

- No `/students/register`, `/colleges_programs` or `/enrol/*`. The first was
  public, so anyone with the link could create a student and put a face into
  `dataset/` and the model; the capture routes let a student replace their own
  images. Only an administrator, or an instructor for their own classes,
  registers a student and captures a face - on the website.
- `/login` issues tokens to students only and every route requires one.
  `/students`, `/subjects` and `/dashboard` served only the staff screens.
- Phone attendance is **once per class per day**: a student already Present or
  Late for the running subject today is refused before any face is judged.
- A phone's frames are judged by **the classroom camera's own pipeline**,
  liveness included (services/phone_verification.py), not by one photo.

`tests/test_api_students_only.py`, `tests/test_api_once_a_day.py`,
and `tests/test_api_attendance_frame.py` keep them.
"""

from __future__ import annotations

import datetime
import functools
import logging
import threading

import jwt
import mysql.connector
from flask import Blueprint, jsonify, request

from config.settings import settings as app_config
from infra.db import db_cursor
from infra.uploads import UploadRejected, decode_letterboxed
from repositories import attendance as attendance_repo
from repositories import students as students_repo
from repositories import subjects as subjects_repo
from security.access import public
from security.passwords import verify_password
from vision.pose import RECOGNITION_FRAME_HEIGHT, RECOGNITION_FRAME_WIDTH

logger = logging.getLogger(__name__)

api_bp = Blueprint("api", __name__, url_prefix="/api")

# JWT configuration — uses the same secret key as Flask sessions.
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 24


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token(payload: dict) -> str:
    """Create a signed JWT with an expiry."""
    payload["exp"] = datetime.datetime.now(tz=datetime.timezone.utc) + datetime.timedelta(
        hours=JWT_EXPIRY_HOURS
    )
    payload["iat"] = datetime.datetime.now(tz=datetime.timezone.utc)
    return jwt.encode(payload, app_config.secret_key, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> dict | None:
    """Decode and verify a JWT. Returns None on any failure."""
    try:
        return jwt.decode(token, app_config.secret_key, algorithms=[JWT_ALGORITHM])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def _get_current_user() -> dict | None:
    """Extract the user payload from the Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    return _decode_token(auth_header[7:])


def student_required(fn):
    """
    Decorator: require a valid JWT Bearer token issued to a student.

    The app is for students only (2026-10-21). /login no longer issues a token
    to an administrator or instructor, and this refuses one issued before that
    changed - those tokens live for JWT_EXPIRY_HOURS, so without the role check
    an old admin token would still reach every route below.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user = _get_current_user()
        if user is None:
            return jsonify({"success": False, "message": "Authentication required"}), 401
        if user.get("role") != "student":
            return jsonify({"success": False, "message": "Forbidden"}), 403
        request.jwt_user = user
        return fn(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------


@api_bp.route("/login", methods=["POST"])
@public
def api_login():
    """
    Authenticate a student and return a JWT token.

    Accepts JSON: {"role": "student", "user_id": "...", "password": "..."}

    Students only. Administrators and instructors sign in on the website,
    which is where everything they do lives; the app used to offer them a
    login too. `role` is still read so that an installed copy of the old app,
    which sends it, is told why rather than given a token.
    """
    data = request.get_json(silent=True) or {}
    role = data.get("role", "student")
    user_id = data.get("user_id", "")
    password = data.get("password", "")

    if role != "student":
        return jsonify({
            "success": False,
            "message": (
                "The app is for students. "
                "Administrators and instructors sign in on the website."
            ),
        }), 403

    if not user_id or not password:
        return jsonify({"success": False, "message": "Missing credentials"}), 400

    with db_cursor(dictionary=True) as cursor:
        cursor.execute(
            "SELECT student_id, name, password FROM students WHERE student_id = %s",
            (user_id,),
        )
        row = cursor.fetchone()

    if row is None or not row.get("password"):
        return jsonify({"success": False, "message": "Invalid credentials"}), 401

    if not verify_password(password, row["password"]):
        return jsonify({"success": False, "message": "Invalid credentials"}), 401

    token = _make_token({
        "role": "student",
        "user_id": row["student_id"],
        "display_name": row["name"],
    })

    return jsonify({
        "success": True,
        "token": token,
        "role": "student",
        "display_name": row["name"],
    })


# ---------------------------------------------------------------------------
# USER PROFILE
# ---------------------------------------------------------------------------


@api_bp.route("/me", methods=["GET"])
@public
@student_required
def api_me():
    """Return the signed-in student's profile."""
    user = request.jwt_user
    user_id = user.get("user_id")

    profile = {
        "role": "student",
        "user_id": user_id,
        "display_name": user.get("display_name", ""),
    }

    with db_cursor(dictionary=True) as cursor:
        student = students_repo.details(cursor, user_id)
        if student:
            profile.update({
                "college_department": student.get("college_department", ""),
                "program": student.get("program", ""),
                "year_level": student.get("year_level"),
                "section": student.get("section", ""),
            })

    return jsonify({"success": True, "profile": profile})


# ---------------------------------------------------------------------------
# ATTENDANCE
# ---------------------------------------------------------------------------


@api_bp.route("/attendance/history", methods=["GET"])
@public
@student_required
def api_attendance_history():
    """
    The signed-in student's own attendance records.

    Query params: ?date=YYYY-MM-DD&subject_id=N
    """
    user = request.jwt_user
    selected_date = request.args.get("date")
    subject_id = request.args.get("subject_id")

    try:
        subject_id = int(subject_id) if subject_id else None
    except (TypeError, ValueError):
        subject_id = None

    query = f"""
        SELECT a.id, a.attendance_date, a.student_id,
               s.name AS student_name, sub.subject_code,
               sub.subject_name,
               {subjects_repo.offering_section('sub')} AS section,
               a.time_in, a.status
        FROM attendance a
        JOIN students s ON s.student_id = a.student_id
        JOIN subjects sub ON sub.id = a.subject_id
        {subjects_repo.offering_join('sub')}
        WHERE a.student_id = %s
    """
    values = [user["user_id"]]

    if selected_date:
        query += " AND a.attendance_date = %s"
        values.append(selected_date)

    if subject_id is not None:
        query += " AND a.subject_id = %s"
        values.append(subject_id)

    query += " ORDER BY a.attendance_date DESC, a.time_in DESC"

    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(query, values)
            rows = cursor.fetchall()

        return jsonify({
            "success": True,
            "records": [
                {
                    "id": r["id"],
                    "attendance_date": str(r["attendance_date"]),
                    "student_id": r["student_id"],
                    "student_name": r["student_name"],
                    "subject_code": r["subject_code"],
                    "subject_name": r["subject_name"],
                    "section": r.get("section", ""),
                    "time_in": str(r["time_in"]) if r["time_in"] else None,
                    "status": r["status"],
                }
                for r in rows
            ],
        })

    except mysql.connector.Error:
        logger.exception("Attendance history query failed")
        return jsonify({"success": False, "message": "Could not load attendance records."}), 500


@api_bp.route("/attendance/live", methods=["GET"])
@public
@student_required
def api_attendance_live():
    """Live session status — who has been recognized so far."""
    # Import here to avoid the heavy recognize_face import at module scope
    from recognize_face import session as recognition_session

    active = recognition_session.subject

    if not recognition_session.is_running or active is None:
        # Every app on this screen polls here, so it is where phones left
        # from a session that has ended get their detectors released.
        _discard_phones()
        return jsonify({"success": True, "running": False, "students": []})

    student_id = request.jwt_user.get("user_id")
    student_ids = sorted(recognition_session.recognized_ids())

    try:
        with db_cursor(dictionary=True) as cursor:
            rows = attendance_repo.recognised_today(
                cursor, active.subject_id, student_ids
            )
    except mysql.connector.Error:
        logger.exception("Could not read live recognized list")
        return jsonify({
            "success": True,
            "running": True,
            "students": [],
            "recognised": len(student_ids),
        })

    try:
        your_status = _recorded_status_today(student_id, active.subject_id)
    except mysql.connector.Error:
        your_status = None  # the frame route checks again, and fails closed

    return jsonify({
        "success": True,
        "running": True,
        "subject_code": active.subject_code,
        "recognised": len(student_ids),
        # Present/Late if the signed-in student already has a mark for this
        # class today - the app shows that instead of a Verify button.
        "your_status": your_status,
        "students": [
            {
                "student_id": row["student_id"],
                "name": row["name"],
                "time_in": str(row["time_in"]) if row["time_in"] else None,
                "status": row["status"],
            }
            for row in rows
        ],
    })


@api_bp.route("/attendance/frame", methods=["POST"])
@public
@student_required
def api_attendance_frame():
    """
    One frame from a student's phone, judged by the classroom camera's rules.

    The phone streams frames while the student verifies; each answer is the
    status the classroom screen would draw under their face ("Verifying 12/20",
    "Turn LEFT", ...) plus `stage`, `step` and `progress` for the app to draw,
    until `done`. See services/phone_verification.py for what is shared with
    the classroom loop and what each phone gets of its own.

    ⚠️ This used to be one photo, one predict, at a 70.0 distance with no
    frontality check and no liveness: one good photo of a student recorded
    them, and 70.0 accepts the closest impostor measured (62.2) that 58.0
    refuses.

    Refused before any face is judged, in this order: no session; already
    recorded today; no classroom-code pass.
    """
    from recognize_face import session as recognition_session
    from services.phone_verification import PhonesBusy

    snapshot = recognition_session.snapshot()

    if (
        not snapshot.running
        or snapshot.subject is None
        or snapshot.recognizer is None
    ):
        _discard_phones()
        return jsonify({"success": False, "message": "No active attendance session."}), 409

    student_id = request.jwt_user.get("user_id")

    # ⚠️ **Once a day, per class, checked before any face is judged.** A
    # student who already has Present or Late for this subject today is
    # refused here, so repeated attempts on a recorded account - the shape a
    # spoofing attempt takes - get no recognition at all to probe. The
    # session's in-memory list is only a cache of the register; the register
    # is asked as well, so a restarted server or a failed seed cannot reopen it.
    recorded = snapshot.recognized.get(student_id)

    if student_id not in snapshot.recognized:
        try:
            recorded = _recorded_status_today(student_id, snapshot.subject.subject_id)
        except mysql.connector.Error:
            # Fail closed: if the register cannot be read, it cannot be written
            # either, and "try again" is the honest answer.
            logger.exception("Could not check today's register for %s", student_id)
            return jsonify({
                "success": False,
                "message": "Attendance could not be checked. Please try again.",
            }), 503

    if student_id in snapshot.recognized or recorded is not None:
        marked = f" as {recorded}" if recorded else ""
        return jsonify({
            "success": True,
            "done": True,
            "already": True,
            "message": (
                f"You are already recorded{marked} for this class today. "
                "Attendance can only be taken once."
            ),
        })

    # The resolution the recognition gates were tuned at, as the browser
    # camera route decodes (web/sessions.py) - not enrolment's 1920x1080.
    #
    # ⚠️ **Letterboxed, not centre-cropped.** The app sends a square, the shape
    # of its preview; cropping it to 16:9 would cut off the top and bottom of
    # what the student framed - L33's bug, the other way round. A 16:9 upload
    # fits exactly and is unchanged. The size rule is "never enlarged", not
    # the 960-wide minimum: see decode_letterboxed().
    #
    # ⚠️ **Several frames per request, judged in the order they were taken.**
    # One round trip through the tunnel costs 0.7-1.3 s whatever the picture's
    # size (measured 2026-10-24, 21-50 KB), so one frame per request held a
    # phone to about one frame a second - 25-35 s to verify, against the
    # classroom camera's 5-8 s. The app keeps photographing while a request is
    # in flight and sends what it took since in the next one; one request at a
    # time, so order is still kept.
    try:
        if 'frame' in request.files:
            uploads = [
                (file.read(), file.mimetype)
                for file in request.files.getlist('frame')[:MAX_FRAMES_PER_REQUEST]
            ]
        else:
            uploads = [(request.get_data(), request.content_type)]

        frames = [
            decode_letterboxed(
                body, content_type, (RECOGNITION_FRAME_WIDTH, RECOGNITION_FRAME_HEIGHT)
            )
            for body, content_type in uploads
        ]
    except UploadRejected as error:
        return jsonify({"success": False, "message": str(error)}), 400

    # A failed save (NotRecorded.ERROR) is not an outcome here: the pipeline
    # retries it on a later frame (`defer_attendance_retry`), and nothing is
    # marked until a row is written - so the once-a-day rule cannot lock out a
    # student who has no row.
    # The phone's own time per picture, for the timing log line only.
    try:
        capture_ms = min(max(float(request.headers.get("X-Capture-Ms", 0)), 0.0), 60_000.0)
    except ValueError:
        capture_ms = 0.0

    try:
        for index, frame in enumerate(frames):
            result = _phone_verifications().verify(
                student_id,
                snapshot,
                frame,
                recognition_session.mark_recognized,
                first_of_request=index == 0,
                capture_ms=capture_ms,
            )

            if result.done or result.refused:
                break
    except PhonesBusy:
        return jsonify({
            "success": False,
            "message": "Too many phones are verifying. Trying again in a moment...",
        }), 429
    except Exception:
        logger.exception("Phone verification failed for %s", student_id)
        return jsonify({"success": False, "message": "Face processing failed."}), 500

    if result.refused:
        return jsonify({"success": False, "message": result.message}), 403

    return jsonify({
        "success": True,
        "done": result.done,
        "message": result.message,
        "stage": result.stage,
        "step": result.step,
        "progress": round(result.progress, 3),
    })


def _recorded_status_today(student_id, subject_id):
    """Present/Late if already recorded for this subject today, else None."""
    with db_cursor(dictionary=True) as cursor:
        return attendance_repo.recorded_status_today(cursor, student_id, subject_id)


# Frames taken in one request; any more are ignored. Four at the measured
# 239 ms each is about a second of server time, so a request still answers in
# well under the phone track's 5 s timeout.
MAX_FRAMES_PER_REQUEST = 4


# The phones verifying now. Built on first use, because building it imports
# recognize_face, which this module deliberately avoids at import time.
_phones = None
_phones_lock = threading.Lock()


def _phone_verifications():
    global _phones

    with _phones_lock:
        if _phones is None:
            import recognize_face
            from services.phone_verification import (
                PhoneVerifications,
                phone_track_config,
            )
            from vision.tracking import FaceTracker

            phone_config = phone_track_config(recognize_face.TRACK_CONFIG)

            _phones = PhoneVerifications(
                make_detector=recognize_face.make_detector,
                make_tracker=lambda: FaceTracker(phone_config),
                recognise=recognize_face.recognise_frame,
            )

        return _phones


def _discard_phones():
    """No session is running: release every phone's detector, if any exist."""
    if _phones is not None:
        _phones.discard_all()
