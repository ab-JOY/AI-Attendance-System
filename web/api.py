"""
REST API for the React Native mobile app.

Completely separate from the session-based web routes. Authentication is
JWT-based (Bearer token), so the existing CSRF/session machinery does not
apply and the blueprint is exempt from CSRFProtect.

**No existing route, template, or web behaviour is changed by this file.**
It is a new blueprint registered alongside the existing eight, under the
``/api`` prefix.
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
from repositories import attendance as attendance_repo
from repositories import credentials as credentials_repo
from repositories import enrolments as enrolments_repo
from repositories import students as students_repo
from repositories import subjects as subjects_repo
from security.access import public
from security.passwords import hash_password, verify_password

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


def jwt_required(fn):
    """Decorator: require a valid JWT Bearer token."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user = _get_current_user()
        if user is None:
            return jsonify({"success": False, "message": "Authentication required"}), 401
        request.jwt_user = user
        return fn(*args, **kwargs)

    return wrapper


def role_required_api(*roles):
    """Decorator: require JWT + one of the given roles."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            user = _get_current_user()
            if user is None:
                return jsonify({"success": False, "message": "Authentication required"}), 401
            if user.get("role") not in roles:
                return jsonify({"success": False, "message": "Forbidden"}), 403
            request.jwt_user = user
            return fn(*args, **kwargs)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------


@api_bp.route("/login", methods=["POST"])
@public
def api_login():
    """
    Authenticate and return a JWT token.

    Accepts JSON: {"role": "admin"|"instructor"|"student", "user_id": "...", "password": "..."}
    """
    data = request.get_json(silent=True) or {}
    role = data.get("role", "")
    user_id = data.get("user_id", "")
    password = data.get("password", "")

    if not role or not user_id or not password:
        return jsonify({"success": False, "message": "Missing credentials"}), 400

    account = None

    if role == "admin":
        with db_cursor(dictionary=True) as cursor:
            row = credentials_repo.admin_by_username(cursor, user_id)
            if row is not None and row["username"] == user_id:
                account = row

        if account is None or not verify_password(password, account.get("password")):
            return jsonify({"success": False, "message": "Invalid credentials"}), 401

        token = _make_token({
            "role": "admin",
            "user_id": account["username"],
            "admin_id": account["id"],
            "display_name": account["username"],
        })

    elif role == "instructor":
        with db_cursor(dictionary=True) as cursor:
            row = credentials_repo.instructor_by_id(cursor, user_id)
            if row is not None and row["instructor_id"] == user_id:
                account = row

        if account is None or not verify_password(password, account.get("password")):
            return jsonify({"success": False, "message": "Invalid credentials"}), 401

        token = _make_token({
            "role": "instructor",
            "user_id": account["instructor_id"],
            "display_name": account["fullname"],
        })

    elif role == "student":
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

        account = row
        token = _make_token({
            "role": "student",
            "user_id": account["student_id"],
            "display_name": account["name"],
        })

    else:
        return jsonify({"success": False, "message": "Invalid role"}), 400

    return jsonify({
        "success": True,
        "token": token,
        "role": role,
        "display_name": account.get("display_name") or account.get("name")
            or account.get("fullname") or account.get("username", ""),
    })


# ---------------------------------------------------------------------------
# USER PROFILE
# ---------------------------------------------------------------------------


@api_bp.route("/me", methods=["GET"])
@public
@jwt_required
def api_me():
    """Return the current user's profile."""
    user = request.jwt_user
    role = user.get("role")
    user_id = user.get("user_id")

    profile = {
        "role": role,
        "user_id": user_id,
        "display_name": user.get("display_name", ""),
    }

    if role == "student":
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
# SUBJECTS
# ---------------------------------------------------------------------------


@api_bp.route("/subjects", methods=["GET"])
@public
@jwt_required
def api_subjects():
    """List subjects. Students see only their enrolled subjects."""
    user = request.jwt_user

    with db_cursor(dictionary=True) as cursor:
        if user.get("role") == "student":
            cursor.execute(
                """
                SELECT s.id, s.subject_code, s.subject_name, s.instructor,
                       s.day, s.course, s.section, s.time_in, s.time_out
                FROM subjects s
                JOIN enrolments e ON e.subject_id = s.id
                WHERE e.student_id = %s
                ORDER BY s.subject_code
                """,
                (user["user_id"],),
            )
            subjects = cursor.fetchall()
        else:
            subjects = subjects_repo.for_selection(cursor)

    return jsonify({"success": True, "subjects": subjects})


# ---------------------------------------------------------------------------
# STUDENTS
# ---------------------------------------------------------------------------


@api_bp.route("/students", methods=["GET"])
@public
@role_required_api("admin")
def api_students():
    """List all students (admin only)."""
    with db_cursor(dictionary=True) as cursor:
        records = students_repo.all_students(cursor)

    # Convert to plain dicts for JSON serialization
    students = []
    for row in records:
        students.append({
            "student_id": row["student_id"],
            "name": row["name"],
            "college_department": row.get("college_department", ""),
            "program": row.get("program", ""),
            "year_level": row.get("year_level"),
            "section": row.get("section", ""),
            "classes": row.get("classes", 0),
        })

    return jsonify({"success": True, "students": students})


@api_bp.route("/students/register", methods=["POST"])
@public
def api_register_student():
    """
    Register a new student from the mobile app.

    This is a public endpoint — new students register themselves.
    The student row and password are created here; face capture follows
    separately through the enrolment endpoints.
    """
    data = request.get_json(silent=True) or {}

    student_id = data.get("student_id", "").strip()
    first_name = data.get("first_name", "").strip()
    middle_name = data.get("middle_name", "").strip()
    last_name = data.get("last_name", "").strip()
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()
    college_department = data.get("college_department", "").strip()
    program = data.get("program", "").strip()
    year_level = data.get("year_level", "").strip()
    section = data.get("section", "").strip()

    if not student_id or not first_name or not last_name or not password:
        return jsonify({
            "success": False,
            "message": "Student number, first name, last name, and password are required.",
        }), 400

    if len(password) < 8:
        return jsonify({
            "success": False,
            "message": "Password must be at least 8 characters.",
        }), 400

    # Build display name
    name_parts = [first_name]
    if middle_name:
        name_parts.append(middle_name)
    name_parts.append(last_name)
    name = " ".join(name_parts)

    try:
        password_hash = hash_password(password)
    except Exception:
        return jsonify({
            "success": False,
            "message": "Password could not be processed.",
        }), 400

    try:
        with db_cursor(dictionary=True, commit=True) as cursor:
            if students_repo.exists(cursor, student_id):
                return jsonify({
                    "success": False,
                    "message": "A student with that ID is already registered.",
                }), 409

            record = {
                "college_department": college_department,
                "program": program,
                "year_level": year_level or None,
                "section": section,
            }
            students_repo.insert(cursor, student_id, name, record)

            # Set the password
            cursor.execute(
                "UPDATE students SET password = %s WHERE student_id = %s",
                (password_hash, student_id),
            )

        token = _make_token({
            "role": "student",
            "user_id": student_id,
            "display_name": name,
        })

        return jsonify({
            "success": True,
            "message": "Registration successful.",
            "token": token,
            "role": "student",
            "display_name": name,
        }), 201

    except mysql.connector.Error:
        logger.exception("Student registration failed")
        return jsonify({
            "success": False,
            "message": "Registration failed. Please try again.",
        }), 500


# ---------------------------------------------------------------------------
# ATTENDANCE
# ---------------------------------------------------------------------------


@api_bp.route("/attendance/history", methods=["GET"])
@public
@jwt_required
def api_attendance_history():
    """
    Attendance history, filtered by the current user's role.

    Students see only their own records. Instructors/admins can filter.
    Query params: ?date=YYYY-MM-DD&subject_id=N
    """
    user = request.jwt_user
    selected_date = request.args.get("date")
    subject_id = request.args.get("subject_id")

    try:
        subject_id = int(subject_id) if subject_id else None
    except (TypeError, ValueError):
        subject_id = None

    try:
        with db_cursor(dictionary=True) as cursor:
            if user.get("role") == "student":
                # Students see only their own attendance
                query = """
                    SELECT a.id, a.attendance_date, a.student_id,
                           s.name AS student_name, sub.subject_code,
                           sub.subject_name, sub.section,
                           a.time_in, a.status
                    FROM attendance a
                    JOIN students s ON s.student_id = a.student_id
                    JOIN subjects sub ON sub.id = a.subject_id
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
                cursor.execute(query, values)
            else:
                records = attendance_repo.filtered(
                    cursor, selected_date=selected_date, subject_id=subject_id
                )
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
                        for r in records
                    ],
                })

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
@jwt_required
def api_attendance_live():
    """Live session status — who has been recognized so far."""
    # Import here to avoid the heavy recognize_face import at module scope
    from recognize_face import session as recognition_session

    active = recognition_session.subject

    if not recognition_session.is_running or active is None:
        return jsonify({"running": False, "students": []})

    student_ids = sorted(recognition_session.recognized_ids())

    try:
        with db_cursor(dictionary=True) as cursor:
            rows = attendance_repo.recognised_today(
                cursor, active.subject_id, student_ids
            )
    except mysql.connector.Error:
        logger.exception("Could not read live recognized list")
        return jsonify({
            "running": True,
            "students": [],
            "recognised": len(student_ids),
        })

    return jsonify({
        "running": True,
        "subject_code": active.subject_code,
        "recognised": len(student_ids),
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


@api_bp.route("/dashboard", methods=["GET"])
@public
@jwt_required
def api_dashboard():
    """Dashboard statistics."""
    try:
        with db_cursor(dictionary=True) as cursor:
            counters = attendance_repo.counters(cursor)

        return jsonify({
            "success": True,
            "counters": {
                "students": counters["students"],
                "subjects": counters["subjects"],
                "attendance_today": counters["attendance_today"],
                "active_sessions": counters["active_sessions"],
            },
        })
    except mysql.connector.Error:
        logger.exception("Dashboard query failed")
        return jsonify({"success": False, "message": "Could not load dashboard data."}), 500
"""

    @api_bp.route('/enrol/start', methods=['POST'])
    @api_bp.route('/enrol/frame', methods=['POST'])
    @api_bp.route('/enrol/finish', methods=['POST'])
    @api_bp.route('/enrol/cancel', methods=['POST'])

    These enrolment endpoints mirror the web enrolment flow. For the mobile app,
    the student's phone camera captures frames and sends them to the server for
    the same quality checks that the browser enrolment uses.

    Implementation is deferred to a follow-up — the existing web/enrolment.py
    endpoints can be called directly from the mobile app once CSRF is handled,
    or dedicated API versions can be added here.
"""
