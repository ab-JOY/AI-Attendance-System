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

`tests/test_api_students_only.py` and `tests/test_api_once_a_day.py` keep them.
"""

from __future__ import annotations

import datetime
import functools
import logging

import jwt
import mysql.connector
from flask import Blueprint, jsonify, request

from config.settings import settings as app_config
from infra.db import db_cursor
from infra.uploads import UploadRejected, decode_frame
from repositories import attendance as attendance_repo
from repositories import students as students_repo
from repositories import subjects as subjects_repo
from security.access import public
from security.passwords import verify_password

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
        return jsonify({"success": True, "running": False, "students": []})

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
        your_status = _recorded_status_today(
            request.jwt_user.get("user_id"), active.subject_id
        )
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
    """Verify a student's face for the active attendance session from the mobile app."""
    from recognize_face import session as recognition_session
    from recognize_face import align_face, preprocess_for_lbph, get_face_quality_issue, NotRecorded, save_attendance
    import cv2

    snapshot = recognition_session.snapshot()
    if not snapshot.running or snapshot.subject is None:
        return jsonify({"success": False, "message": "No active attendance session."}), 409

    user = request.jwt_user
    student_id = user.get("user_id")

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

    try:
        if 'frame' in request.files:
            file = request.files['frame']
            frame = decode_frame(file.read(), file.mimetype)
        else:
            frame = decode_frame(request.get_data(), request.content_type)
    except UploadRejected as error:
        return jsonify({"success": False, "message": str(error)}), 400

    # Detect face
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    results = snapshot.detector.process(rgb)

    if not results.multi_face_landmarks:
        return jsonify({"success": True, "done": False, "message": "No face detected."})

    # For verification, we just take the first face found
    landmarks = results.multi_face_landmarks[0]
    
    try:
        aligned_face = align_face(frame, landmarks)
        issue = get_face_quality_issue(aligned_face)
        if issue:
            return jsonify({"success": True, "done": False, "message": issue})

        processed_face = preprocess_for_lbph(aligned_face)
        label, confidence = snapshot.recognizer.predict(processed_face)
    except Exception:
        logger.exception("Mobile attendance recognition failed")
        return jsonify({"success": False, "message": "Face processing failed."}), 500

    if label == -1 or confidence > 70.0:  # Use a standard threshold
        return jsonify({"success": True, "done": False, "message": "Face not recognized clearly. Try again."})

    matched_student = snapshot.label_map.get(label)
    if matched_student is None or matched_student.get("student_id") != student_id:
        return jsonify({"success": True, "done": False, "message": "Face doesn't match your profile."})

    # Verified! Save attendance
    status = save_attendance(student_id, snapshot.subject)
    if status in (NotRecorded.NOT_IN_CLASS, NotRecorded.UNKNOWN_STUDENT):
        return jsonify({"success": False, "message": "You are not enrolled in this class."}), 403

    # NotRecorded.ERROR: the database could not be reached, so nothing was
    # written. It must not fall through to mark_recognized() below - with the
    # once-a-day rule above, that would lock the student out for the day with
    # no attendance row at all.
    if not status:
        return jsonify({
            "success": False,
            "message": "Your attendance could not be saved. Please try again.",
        }), 503

    # Mark as recognized in the in-memory session so the dashboard updates
    recognition_session.mark_recognized(student_id, status)

    return jsonify({
        "success": True,
        "done": True,
        "message": f"Verified! Marked as {status}."
    })


def _recorded_status_today(student_id, subject_id):
    """Present/Late if already recorded for this subject today, else None."""
    with db_cursor(dictionary=True) as cursor:
        return attendance_repo.recorded_status_today(cursor, student_id, subject_id)
