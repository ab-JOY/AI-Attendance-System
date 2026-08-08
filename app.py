import logging
import os
import shutil
import stat
import subprocess
import sys
from datetime import timedelta

import mysql.connector
import pandas as pd
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_wtf.csrf import CSRFError, CSRFProtect

from config.exit_codes import EXIT_CANCELLED, EXIT_SUCCESS
from config.logging_config import configure_logging

# Aliased deliberately. There is a route handler named `settings()` at the
# bottom of this file, and importing the config object as a bare `settings`
# let that def shadow it. Nothing failed at import - app.secret_key is read
# before the def executes - but get_db_connection() runs per request, by
# which point `settings` was the view function and every database call
# raised AttributeError. Do not rename this back without renaming the route.
from config.settings import settings as app_config
from security.access import (
    MUST_CHANGE_PASSWORD,
    authenticated,
    install_access_control,
    json_api,
    public,
    role_required,
)
from security.passwords import (
    PasswordTooLongError,
    hash_password,
    is_known_default,
    verify_password,
)
from security.paths import (
    UnsafeStudentPathError,
    student_dataset_path,
    validate_student_id,
    validate_student_name,
)
from security.rate_limit import LoginRateLimiter

# app.py is the entry point, so it owns logging configuration for the process.
#
# This has to run BEFORE importing recognize_face, which loads the LBPH model
# and constructs a MediaPipe FaceMesh at module scope (PE-4, deferred to
# Phase 3) and logs the outcome of both. With no handlers attached yet those
# startup messages - including "model failed to load" - would be discarded.
# The noqa markers below come off when PE-4 is fixed and the import is cheap.
configure_logging()

from recognize_face import generate_frames, start_camera, stop_camera  # noqa: E402
from train_model import train_model  # noqa: E402

logger = logging.getLogger(__name__)

app = Flask(__name__)

# Was hardcoded as "ai_attendance_secret_key" (SE-8). That value is in git
# history, and a known Flask secret key means session cookies can be forged.
# Now required from the environment - see .env.example.
app.secret_key = app_config.secret_key

# ==============================
# SESSION HARDENING (SE-11)
# ==============================
#
# HTTPONLY keeps the cookie out of reach of any injected script. SAMESITE=Lax
# stops a cross-site GET carrying it, which is the second half of the CSRF
# defence below. The lifetime turns an unattended browser on a shared
# classroom machine from a permanent session into a 30-minute one.
#
# SESSION_COOKIE_SECURE comes from configuration and defaults to False - see
# config/settings.py for why hardcoding True would lock everyone out of the
# HTTP-on-localhost deployment this system actually has.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=app_config.session_cookie_secure,
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=app_config.session_lifetime_minutes),
)

# ==============================
# CSRF (SE-7)
# ==============================
#
# Applied to the whole application rather than form by form. None of the ~15
# state-changing routes had any CSRF protection, and protecting them
# individually would mean the next route added is unprotected by default.
# CSRFProtect rejects any POST without a valid token, so the failure mode for
# a forgotten token is a visible 400 rather than a silent hole.
#
# The token reaches templates as `csrf_token()`; the two fetch() calls in
# attendance.html send it in an X-CSRFToken header.
csrf = CSRFProtect(app)

# ==============================
# ACCESS CONTROL (SE-2, SE-4, SE-5)
# ==============================
#
# Default deny. Every view below is explicitly marked @public, @authenticated
# or @role_required, and anything unmarked is refused. See security/access.py
# and tests/test_route_security.py.
install_access_control(app)

# ==============================
# LOGIN THROTTLING (SE-13)
# ==============================
login_rate_limiter = LoginRateLimiter(
    max_attempts=app_config.login_max_attempts,
    lockout_seconds=app_config.login_lockout_seconds,
    window_seconds=app_config.login_attempt_window_seconds,
)


# ==============================
# DATABASE CONNECTION
# ==============================
def get_db_connection():
    return mysql.connector.connect(**app_config.db_kwargs())

# ==============================
# DELETE READ-ONLY FILES/FOLDERS
# ==============================
def remove_readonly_and_retry(function, path, exc_info):
    """
    Makes a read-only file or folder writable and retries deletion.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
        function(path)

    except Exception:
        raise


# ==============================
# ERROR PAGES (SE-10, US-1, RE-8)
# ==============================
#
# Around fifteen routes used to do `return f"Database error: {error}", 500`.
# That put the driver's message - table names, column names, MySQL version -
# on a blank page with no navigation: a schema disclosure and a dead end at
# the same time. The details go to the log, where they are useful; the client
# gets a code and a way back.

ERROR_MESSAGES = {
    400: "That request could not be processed. Please go back and try again.",
    403: "You do not have permission to view this page.",
    404: "That page could not be found.",
    405: "That action is not available from this page.",
    409: "That change conflicts with existing data.",
    500: "Something went wrong on the server. The details have been logged.",
}


def error_page(status_code, message=None):
    """Render the generic error page. Never include exception text here."""
    return (
        render_template(
            "error.html",
            status_code=status_code,
            message=message or ERROR_MESSAGES.get(status_code, ERROR_MESSAGES[500]),
        ),
        status_code,
    )


CSRF_ERROR_MESSAGE = (
    "This form has expired or was not submitted from this site. "
    "Reload the page and try again."
)


@app.errorhandler(CSRFError)
def handle_csrf_error(error):
    """
    Distinct from the generic 400 on purpose.

    For the operator, "reload the page" is actionable where "that request
    could not be processed" is not - a rejected token is usually just an
    expired session, not an attack. For the test suite, it is the difference
    between proving CSRF was enforced and proving *something* returned 400:
    a POST with a missing form field also produces 400, so a test that only
    checked the status code would pass on code with no CSRF protection at
    all.
    """
    logger.warning(
        "CSRF validation failed for %s: %s", request.path, error.description
    )
    return error_page(400, CSRF_ERROR_MESSAGE)


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(405)
@app.errorhandler(409)
def handle_http_error(error):
    return error_page(error.code)


@app.errorhandler(500)
def handle_internal_error(error):
    logger.exception("Unhandled server error", exc_info=error)
    return error_page(500)


@app.errorhandler(Exception)
def handle_unexpected_exception(error):
    """
    Catch-all so an exception raised anywhere cannot reach the browser as a
    traceback. Flask re-raises HTTPExceptions to their own handlers, so this
    only sees genuine faults.
    """
    logger.exception("Unhandled exception while serving %s", request.path)
    return error_page(500)


# ==============================
# LOGIN PAGE
# ==============================
@app.route('/')
@public
def home():
    return render_template('login.html')


# ==============================
# LOGIN PROCESS
# ==============================
#
# Rewritten for SE-1, SE-13 and SE-15. Three things changed and each matters
# on its own:
#
# 1. The password is no longer part of the query. It used to be
#    `WHERE username=%s AND password=%s`, which let MySQL decide the match -
#    and the column collation is utf8mb4_general_ci, case-insensitive and
#    PAD SPACE, so `ADMIN` and `admin   ` both authenticated as `admin`
#    (measured, SE-15). The row is fetched by identifier and the password is
#    verified in Python against a bcrypt hash.
# 2. The *identifier* is re-checked in Python for the same reason. bcrypt
#    fixes the password half of SE-15 and does nothing for the username half.
# 3. Consecutive failures are counted and the account is locked (SE-13).
@app.route('/login', methods=['POST'])
@public
def login():

    role = request.form.get('role', '')
    user_id = request.form.get('user_id', '')
    password = request.form.get('password', '')

    rate_limit_key = LoginRateLimiter.key(user_id, request.remote_addr)
    locked_for = login_rate_limiter.seconds_until_unlocked(rate_limit_key)

    if locked_for > 0:
        logger.warning(
            "Login refused: %r is locked out for another %d second(s)",
            user_id,
            int(locked_for),
        )
        return render_template(
            'login.html',
            error=(
                "Too many failed attempts. Try again in "
                f"{max(1, int(locked_for // 60))} minute(s)."
            ),
        ), 429

    account = None
    conn = None
    cursor = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        if role == "admin":
            cursor.execute(
                "SELECT * FROM admin WHERE username=%s",
                (user_id,)
            )
            row = cursor.fetchone()

            # The collation makes the lookup above case-insensitive too, so
            # the identifier is confirmed here rather than trusted.
            if row is not None and row['username'] == user_id:
                account = row

        elif role == "instructor":
            cursor.execute(
                "SELECT * FROM instructors WHERE instructor_id=%s",
                (user_id,)
            )
            row = cursor.fetchone()

            if row is not None and row['instructor_id'] == user_id:
                account = row

    finally:
        if cursor is not None:
            cursor.close()

        if conn is not None and conn.is_connected():
            conn.close()

    if account is None or not verify_password(password, account.get('password')):
        login_rate_limiter.record_failure(rate_limit_key)

        # Deliberately identical whether the account exists or not, so the
        # response cannot be used to enumerate usernames.
        logger.info("Failed login for %r as %r", user_id, role)

        return render_template(
            'login.html',
            error='Invalid Login Credentials'
        ), 401

    login_rate_limiter.record_success(rate_limit_key)

    session.clear()
    session.permanent = True

    if role == "admin":
        session['user'] = account['username']
        session['role'] = 'admin'
    else:
        session['user'] = account['fullname']
        session['role'] = 'instructor'
        session['instructor_id'] = account['instructor_id']

    # Flagged in the database, or still using a credential this system ships
    # with. Either way the account can reach nothing but the change-password
    # page until it is dealt with (see security/access.py).
    must_change = bool(account.get('must_change_password')) or is_known_default(
        account.get('password')
    )

    if must_change:
        session[MUST_CHANGE_PASSWORD] = True
        logger.warning(
            "%r signed in with a credential that must be changed", user_id
        )
        return redirect(url_for('change_password'))

    logger.info("Successful login for %r as %r", user_id, role)

    return redirect(url_for('dashboard'))
# ==============================
# DASHBOARD
# ==============================
@app.route('/dashboard')
@authenticated
def dashboard():
    return render_template('dashboard.html')


# ==============================
# STUDENTS PAGE
# ==============================
@app.route('/students')
@role_required('admin')
def students():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT * FROM students ORDER BY name ASC")
    students = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template('students.html', students=students)


# ==============================
# CAPTURE FACE + SAVE STUDENT
# ==============================
@app.route('/capture_face', methods=['POST'])
@role_required('admin')
def capture_face():

    # Get form data
    student_id = request.form.get('student_id', '')
    name = request.form.get('name', '')
    college_department = request.form.get('college_department', '')
    program = request.form.get('program', '')
    year_level = request.form.get('year_level', '')
    section = request.form.get('section', '')

    # SE-3. Validate before the row is inserted and before capture_dataset.py
    # is handed these values, so nothing hostile is ever stored, let alone
    # turned into a path. security/paths.py is the single authority on what
    # an acceptable ID and name are; capture_dataset.py uses it too.
    try:
        student_id = validate_student_id(student_id)
        name = validate_student_name(name)
    except UnsafeStudentPathError as error:
        logger.warning("Rejected student enrolment: %s", error)
        return error_page(400, str(error))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Check if student already exists
    cursor.execute(
        "SELECT * FROM students WHERE student_id=%s",
        (student_id,)
    )

    existing_student = cursor.fetchone()

    # Insert only if student does not exist
    if existing_student is None:

        cursor.execute("""
            INSERT INTO students
            (
                student_id,
                name,
                college_department,
                program,
                year_level,
                section
            )
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            student_id,
            name,
            college_department,
            program,
            year_level,
            section
        ))

        conn.commit()

    cursor.close()
    conn.close()

    # Capture student's face
    try:
        result = subprocess.run([
            sys.executable,
            "capture_dataset.py",
            student_id,
            name
        ], check=True)

        if result.returncode == EXIT_SUCCESS:
            # Auto-train LBPH model after capturing dataset
            logger.info("Auto-training model after dataset capture")
            train_success, train_msg = train_model()
            logger.info("Train result: %s - %s", train_success, train_msg)

    except subprocess.CalledProcessError as error:
        if error.returncode == EXIT_CANCELLED:
            logger.info("Face capture cancelled by user")
            return redirect(url_for('students'))
        logger.exception("Capture dataset script failed")
        return error_page(500, "Face capture did not complete. See the log for details.")
    except Exception:
        logger.exception("Camera error during face capture")
        return error_page(500, "The camera could not be started.")

    return redirect(url_for('students'))

# ==============================
# MANAGE STUDENTS
# ==============================
@app.route('/manage_students')
@role_required('admin')
def manage_students():

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT * FROM students ORDER BY name ASC")
    students = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "manage_students.html",
        students=students
    )

# ==============================
# SEARCH STUDENT
# ==============================
@app.route('/search_student', methods=['POST'])
@role_required('admin')
def search_student():

    query = request.form.get('query', '')

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT * FROM students
        WHERE student_id LIKE %s OR name LIKE %s
        ORDER BY name ASC
    """, (f"%{query}%", f"%{query}%"))

    students = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template("manage_students.html", students=students)


# ==============================
# DELETE STUDENT
# ==============================
@app.route(
    '/delete_student/<string:student_id>',
    methods=['POST']
)
@role_required('admin')
def delete_student(student_id):

    conn = None
    cursor = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Get the student's name first.
        cursor.execute("""
            SELECT
                student_id,
                name
            FROM students
            WHERE student_id = %s
        """, (student_id,))

        student = cursor.fetchone()

        if student is None:
            return redirect(url_for('manage_students'))

        # SE-3. This was the worst of the four sites: an unvalidated name
        # concatenated into a path and handed to shutil.rmtree(). A student
        # named `..\..\Windows\Temp` deleted that directory instead.
        # student_dataset_path() refuses anything that is not a direct child
        # of dataset/, so a bad record now fails loudly rather than deleting
        # the wrong tree.
        dataset_path = student_dataset_path(
            student['student_id'],
            student['name']
        )

        # Delete the dataset folder first.
        if dataset_path.is_dir():
            shutil.rmtree(
                dataset_path,
                onerror=remove_readonly_and_retry
            )

        # Delete attendance records connected to the student.
        cursor.execute("""
            DELETE FROM attendance
            WHERE student_id = %s
        """, (student_id,))

        # Delete the student record.
        cursor.execute("""
            DELETE FROM students
            WHERE student_id = %s
        """, (student_id,))

        conn.commit()

        return redirect(url_for('manage_students'))

    except UnsafeStudentPathError as error:
        if conn is not None:
            conn.rollback()

        logger.error("Refused to delete a student with an unsafe folder: %s", error)

        return error_page(
            400,
            "This student's record cannot be turned into a safe dataset "
            "folder name, so nothing was deleted. Correct the student's "
            "details first."
        )

    except PermissionError:
        if conn is not None:
            conn.rollback()

        logger.exception("Delete student failed: dataset folder is in use")

        # Actionable and leaks nothing: it names what the operator has to do,
        # not what the filesystem said.
        return error_page(
            500,
            "The student's dataset folder is currently in use. Close the "
            "attendance camera, the recognition program, File Explorer and "
            "any image preview, then try again."
        )

    except mysql.connector.Error:
        if conn is not None:
            conn.rollback()

        logger.exception("Delete student failed: database error")

        return error_page(500)

    except OSError:
        if conn is not None:
            conn.rollback()

        logger.exception("Delete student failed: could not remove dataset folder")

        return error_page(500, "The student's dataset folder could not be removed.")

    except Exception:
        if conn is not None:
            conn.rollback()

        logger.exception("Delete student failed")

        return error_page(500)

    finally:
        if cursor is not None:
            cursor.close()

        if conn is not None and conn.is_connected():
            conn.close()

# ==============================
# EDIT STUDENT
# ==============================
@app.route('/edit_student/<string:student_id>')
@role_required('admin')
def edit_student(student_id):

    conn = None
    cursor = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT
                student_id,
                name,
                college_department,
                program,
                year_level,
                section
            FROM students
            WHERE student_id = %s
        """, (student_id,))

        student = cursor.fetchone()

        if student is None:
            return redirect(url_for('manage_students'))

        return render_template(
            "edit_students.html",
            student=student
        )

    except mysql.connector.Error:
        logger.exception("Edit student failed: database error")
        return error_page(500)

    finally:
        if cursor is not None:
            cursor.close()

        if conn is not None and conn.is_connected():
            conn.close()


# ==============================
# UPDATE STUDENT
# ==============================
@app.route(
    '/update_student/<string:student_id>',
    methods=['POST']
)
@role_required('admin')
def update_student(student_id):

    # Read the submitted form values.
    name = request.form.get('name', '').strip()
    college_department = request.form.get(
        'college_department',
        ''
    ).strip()
    program = request.form.get('program', '').strip()
    year_level = request.form.get('year_level', '').strip()
    section = request.form.get('section', '').strip()

    # SE-3. The new name goes on to build a folder path for os.rename(), so
    # it is validated before anything else happens - and so is the ID from
    # the URL, which was previously trusted outright.
    try:
        student_id = validate_student_id(student_id)
        name = validate_student_name(name)
    except UnsafeStudentPathError as error:
        logger.warning("Rejected student update: %s", error)
        return error_page(400, str(error))

    # Validate required values.
    if not program:
        return error_page(400, "Program is required.")

    if not section:
        return error_page(400, "Section is required.")

    try:
        year_level = int(year_level)

    except (TypeError, ValueError):
        return error_page(400, "Year level must be a valid number.")

    conn = None
    cursor = None

    old_dataset_path = None
    new_dataset_path = None
    folder_was_renamed = False

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Retrieve the student's old information.
        cursor.execute("""
            SELECT
                student_id,
                name
            FROM students
            WHERE student_id = %s
        """, (student_id,))

        existing_student = cursor.fetchone()

        if existing_student is None:
            return error_page(404, "That student record was not found.")

        old_name = existing_student['name']

        # SE-3. Both sides of the rename go through the same validator, so
        # neither the stored name nor the submitted one can send os.rename()
        # outside dataset/.
        old_dataset_path = student_dataset_path(student_id, old_name)
        new_dataset_path = student_dataset_path(student_id, name)

        # Rename the dataset folder if the student's name changed.
        if (
            old_name != name
            and old_dataset_path.is_dir()
        ):
            if new_dataset_path.exists():
                return error_page(
                    409,
                    "A dataset folder with the updated student name "
                    "already exists."
                )

            try:
                os.rename(
                    old_dataset_path,
                    new_dataset_path
                )

                folder_was_renamed = True

            except PermissionError:
                logger.exception(
                    "Dataset folder rename denied by the operating system"
                )

                return error_page(
                    500,
                    "The student's dataset folder is currently in use. "
                    "Close the attendance camera, the recognition program, "
                    "File Explorer and any image preview, then try again."
                )

        # Update all editable student fields.
        cursor.execute("""
            UPDATE students
            SET
                name = %s,
                college_department = %s,
                program = %s,
                year_level = %s,
                section = %s
            WHERE student_id = %s
        """, (
            name,
            college_department,
            program,
            year_level,
            section,
            student_id
        ))

        conn.commit()

        return redirect(url_for('manage_students'))

    except mysql.connector.Error:
        if conn is not None:
            conn.rollback()

        # Restore the original folder name if the SQL update failed.
        if (
            folder_was_renamed
            and new_dataset_path
            and old_dataset_path
            and new_dataset_path.is_dir()
            and not old_dataset_path.exists()
        ):
            try:
                os.rename(
                    new_dataset_path,
                    old_dataset_path
                )

            except OSError:
                logger.exception(
                    "Could not restore dataset folder after a failed update"
                )

        logger.exception("Update student failed: database error")
        return error_page(500)

    except UnsafeStudentPathError as error:
        if conn is not None:
            conn.rollback()

        logger.error("Refused to update a student with an unsafe folder: %s", error)
        return error_page(400, str(error))

    except Exception:
        if conn is not None:
            conn.rollback()

        logger.exception("Update student failed")
        return error_page(500)

    finally:
        if cursor is not None:
            cursor.close()

        if conn is not None and conn.is_connected():
            conn.close()


# ==============================
# RECAPTURE STUDENT FACE
# ==============================
@app.route('/recapture_face', methods=['POST'])
@role_required('admin')
def recapture_face():

    student_id = request.form.get(
        'student_id',
        ''
    ).strip()

    # SE-3. This route calls shutil.rmtree() on a path built from the record,
    # so the ID is validated before it is even looked up.
    try:
        student_id = validate_student_id(student_id)
    except UnsafeStudentPathError as error:
        logger.warning("Rejected recapture request: %s", error)
        return error_page(400, str(error))

    conn = None
    cursor = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT
                student_id,
                name
            FROM students
            WHERE student_id = %s
        """, (student_id,))

        student = cursor.fetchone()

    except mysql.connector.Error:
        logger.exception("Recapture failed: database error")
        return error_page(500)

    finally:
        if cursor is not None:
            cursor.close()

        if conn is not None and conn.is_connected():
            conn.close()

    if student is None:
        return error_page(404, "That student ID was not found.")

    try:
        dataset_path = student_dataset_path(
            student['student_id'],
            student['name']
        )

    except UnsafeStudentPathError as error:
        logger.error("Refused to recapture with an unsafe folder: %s", error)
        return error_page(
            400,
            "This student's record cannot be turned into a safe dataset "
            "folder name, so nothing was removed."
        )

    # Remove the student's old face dataset.
    try:
        if dataset_path.is_dir():
            shutil.rmtree(
                dataset_path,
                onerror=remove_readonly_and_retry
            )

    except PermissionError:
        logger.exception(
            "Recapture failed: dataset folder is in use"
        )

        return error_page(
            500,
            "The student's dataset folder is currently in use. Close the "
            "attendance camera, the recognition program, File Explorer and "
            "any image preview, then try again."
        )

    except OSError:
        logger.exception("Recapture failed: could not remove old dataset folder")

        return error_page(500, "The old dataset folder could not be removed.")

    # Start the face-capture script.
    try:
        result = subprocess.run(
            [
                sys.executable,
                "capture_dataset.py",
                student['student_id'],
                student['name']
            ],
            check=True
        )

        logger.info(
            "Face recapture finished with exit code %s",
            result.returncode
        )

        # Auto-train LBPH model after recapturing face dataset
        logger.info("Auto-training model after face recapture")
        train_success, train_msg = train_model()
        logger.info("Train result: %s - %s", train_success, train_msg)

    except subprocess.CalledProcessError as error:
        if error.returncode == EXIT_CANCELLED:
            logger.info("Face recapture cancelled by user")
            return redirect(url_for('manage_students'))

        logger.exception("Capture dataset script failed")

        return error_page(
            500,
            "Face capture did not complete. See the log for details."
        )

    except PermissionError:
        logger.exception("Permission denied starting the face-capture process")

        return error_page(
            500,
            "Permission was denied while starting the face-capture program."
        )

    except Exception:
        logger.exception("Camera error during face recapture")
        return error_page(500, "The camera could not be started.")

    return redirect(url_for('manage_students'))


# ==========================================================
# MANUAL MODEL RETRAINING ROUTE
# ==========================================================
@app.route('/train_model', methods=['POST'])
@role_required('admin')
@json_api
def train_model_route():
    success, message = train_model()
    return jsonify({
        "success": success,
        "message": message
    })


# ==========================================================
# SUBJECTS MODULE
# ==========================================================

@app.route('/subjects')
@role_required('admin')
def subjects():

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT * FROM subjects ORDER BY id DESC")
    subjects = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template("subjects.html", subjects=subjects)


@app.route('/add_subject', methods=['POST'])
@role_required('admin')
def add_subject():

    subject_code = request.form['subject_code']
    subject_name = request.form['subject_name']
    instructor = request.form['instructor']
    day = request.form['day']
    course = request.form['course']
    section = request.form['section']
    time_in = request.form['time_in']
    time_out = request.form['time_out']

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO subjects
        (subject_code, subject_name, instructor, day, course, section, time_in, time_out)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (subject_code, subject_name, instructor, day, course, section, time_in, time_out))

    conn.commit()
    cursor.close()
    conn.close()

    return redirect(url_for('subjects'))


@app.route('/edit_subject/<int:id>')
@role_required('admin')
def edit_subject(id):

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT * FROM subjects WHERE id=%s", (id,))
    subject = cursor.fetchone()

    cursor.close()
    conn.close()

    return render_template("edit_subject.html", subject=subject)


@app.route('/update_subject/<int:id>', methods=['POST'])
@role_required('admin')
def update_subject(id):

    subject_code = request.form['subject_code']
    subject_name = request.form['subject_name']
    instructor = request.form['instructor']
    day = request.form['day']
    course = request.form['course']
    section = request.form['section']
    time_in = request.form['time_in']
    time_out = request.form['time_out']

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE subjects
        SET subject_code=%s,
            subject_name=%s,
            instructor=%s,
            day=%s,
            course=%s,
            section=%s,
            time_in=%s,
            time_out=%s
        WHERE id=%s
    """, (subject_code, subject_name, instructor, day, course, section, time_in, time_out, id))

    conn.commit()
    cursor.close()
    conn.close()

    return redirect(url_for('subjects'))


# POST, not GET (SE-6). A destructive action behind a GET is triggerable by
# an <img src>, a link prefetch or a crawler - no form submission required.
# The template link became an inline POST form with a CSRF token.
@app.route('/delete_subject/<int:id>', methods=['POST'])
@role_required('admin')
def delete_subject(id):

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM subjects WHERE id=%s", (id,))

    conn.commit()
    cursor.close()
    conn.close()

    return redirect(url_for('subjects'))
# ==============================
# INSTRUCTORS PAGE
# ==============================
@app.route('/instructors')
@role_required('admin')
def instructors():

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT *
        FROM instructors
        ORDER BY fullname ASC
    """)

    instructors = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "instructors.html",
        instructors=instructors
    )


# ==============================
# ADD INSTRUCTOR
# ==============================
@app.route('/add_instructor', methods=['POST'])
@role_required('admin')
def add_instructor():

    instructor_id = request.form.get('instructor_id', '').strip()
    fullname = request.form.get('fullname', '').strip()
    password = request.form.get('password', '')

    if not instructor_id or not fullname or not password:
        return error_page(400, "Instructor ID, full name and password are required.")

    # SE-1. The password an admin types here is stored as a bcrypt hash, and
    # the account is flagged so the instructor must replace the
    # administrator-chosen password with one only they know at first login.
    try:
        hashed_password = hash_password(password)
    except PasswordTooLongError as error:
        return error_page(400, str(error))

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO instructors
        (
            instructor_id,
            fullname,
            password,
            must_change_password
        )
        VALUES (%s,%s,%s,1)
    """, (
        instructor_id,
        fullname,
        hashed_password
    ))

    conn.commit()

    cursor.close()
    conn.close()

    logger.info("Instructor %r created by %r", instructor_id, session.get('user'))

    return redirect(url_for('manage_instructors'))


# ==============================
# MANAGE INSTRUCTORS
# ==============================
@app.route('/manage_instructors')
@role_required('admin')
def manage_instructors():

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT *
        FROM instructors
        ORDER BY fullname ASC
    """)

    instructors = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "manage_instructors.html",
        instructors=instructors
    )


# ==============================
# SEARCH INSTRUCTOR
# ==============================
@app.route('/search_instructor', methods=['POST'])
@role_required('admin')
def search_instructor():

    query = request.form.get('query', '')

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT *
        FROM instructors
        WHERE instructor_id LIKE %s
        OR fullname LIKE %s
        ORDER BY fullname ASC
    """, (
        f"%{query}%",
        f"%{query}%"
    ))

    instructors = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "manage_instructors.html",
        instructors=instructors
    )


# ==============================
# EDIT INSTRUCTOR
# ==============================
@app.route('/edit_instructor/<instructor_id>')
@role_required('admin')
def edit_instructor(instructor_id):

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute(
        "SELECT * FROM instructors WHERE instructor_id=%s",
        (instructor_id,)
    )

    instructor = cursor.fetchone()

    cursor.close()
    conn.close()

    if instructor is None:
        return error_page(404, "That instructor was not found.")

    # FS-6: this said "edit_instructor.html", which does not exist - the file
    # is edit_instructors.html - so the route returned 500 for every request.
    # Fixed in passing because a route that 500s for the legitimate user is
    # not meaningfully "secured".
    return render_template(
        "edit_instructors.html",
        instructor=instructor
    )
# ==============================
# UPDATE INSTRUCTOR
# ==============================
@app.route('/update_instructor/<instructor_id>', methods=['POST'])
@role_required('admin')
def update_instructor(instructor_id):

    fullname = request.form.get('fullname', '').strip()
    password = request.form.get('password', '')

    if not fullname:
        return error_page(400, "Full name is required.")

    conn = get_db_connection()
    cursor = conn.cursor()

    if password:
        # SE-1. An administrator setting someone else's password knows it,
        # so the instructor is required to replace it at next login.
        try:
            hashed_password = hash_password(password)
        except PasswordTooLongError as error:
            cursor.close()
            conn.close()
            return error_page(400, str(error))

        cursor.execute("""
            UPDATE instructors
            SET
                fullname=%s,
                password=%s,
                must_change_password=1
            WHERE instructor_id=%s
        """, (
            fullname,
            hashed_password,
            instructor_id
        ))
    else:
        # The form marks the password field optional. Leaving it blank must
        # not overwrite the stored hash with an empty string.
        cursor.execute("""
            UPDATE instructors
            SET fullname=%s
            WHERE instructor_id=%s
        """, (
            fullname,
            instructor_id
        ))

    conn.commit()

    cursor.close()
    conn.close()

    return redirect(url_for('manage_instructors'))
# ==============================
# DELETE INSTRUCTOR
# ==============================
# POST, not GET (SE-6). See the note on delete_subject.
@app.route('/delete_instructor/<instructor_id>', methods=['POST'])
@role_required('admin')
def delete_instructor(instructor_id):

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM instructors WHERE instructor_id=%s",
        (instructor_id,)
    )

    conn.commit()

    cursor.close()
    conn.close()

    return redirect(url_for('manage_instructors'))
# ==============================
# 🔥 ATTENDANCE (IMPORTANT FIX ADDED)
# ==============================
@app.route('/attendance')
@authenticated
def attendance():
    return render_template('attendance.html')


# ==============================
# START FACE RECOGNITION CAMERA
# ==============================
@app.route('/start-attendance', methods=['POST'])
@authenticated
@json_api
def start_attendance():

    subject_code = request.form.get("subject_code")

    logger.info("Start attendance requested for subject %s", subject_code)



    if not subject_code:
        return jsonify({
            "success": False,
            "message": "No subject selected"
        })


    try:

        started = start_camera(subject_code)

        logger.info("Camera start result: %s", started)

        return jsonify({
            "success": started
        })


    except Exception:

        logger.exception("Camera error while starting attendance")

        # SE-10: the exception text used to go straight to the browser.
        return jsonify({
            "success": False,
            "message": "The camera could not be started."
        })
@app.route('/stop_camera', methods=['POST'])
@authenticated
@json_api
def stop_camera_route():

    stop_camera()

    return jsonify({
        "success": True
    })
@app.route('/end-attendance', methods=['POST'])
@authenticated
def end_attendance():

    stop_camera()

    subject_code = request.form.get(
        'subject_code',
        ''
    ).strip()

    if not subject_code:
        return error_page(400, "Subject code is required.")

    conn = None
    cursor = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Get only today's attendance for the selected subject.
        cursor.execute("""
            SELECT DISTINCT
                student_id
            FROM attendance
            WHERE subject_code = %s
            AND attendance_date = CURDATE()
        """, (subject_code,))

        attendance_records = cursor.fetchall()

        # Store unique present student IDs.
        present_ids = {
            str(record['student_id'])
            for record in attendance_records
        }

        # Get all registered students.
        cursor.execute("""
            SELECT
                student_id,
                name
            FROM students
            ORDER BY name ASC
        """)

        students = cursor.fetchall()

        session_results = []

        for student in students:

            student_id = str(student['student_id'])

            if student_id in present_ids:
                status = "Present"
            else:
                status = "Absent"

            session_results.append({
                "student_id": student_id,
                "name": student['name'],
                "status": status
            })

        total_students = len(students)

        # Only count IDs that still exist in the students table.
        valid_student_ids = {
            str(student['student_id'])
            for student in students
        }

        valid_present_ids = (
            present_ids & valid_student_ids
        )

        total_present = len(valid_present_ids)
        total_absent = total_students - total_present

        return render_template(
            "attendance.html",
            session_results=session_results,
            total_students=total_students,
            total_present=total_present,
            total_absent=total_absent,
            selected_subject=subject_code
        )

    except mysql.connector.Error:
        logger.exception("End attendance failed: database error")
        return error_page(500)

    finally:
        if cursor is not None:
            cursor.close()

        if conn is not None and conn.is_connected():
            conn.close()
# ==============================
# REPORTS
# ==============================
@app.route('/reports')
@authenticated
def reports():

    selected_date = request.args.get('date')
    selected_subject = request.args.get('subject')

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Load subjects dropdown
    cursor.execute("""
        SELECT *
        FROM subjects
        ORDER BY subject_code
    """)
    subjects = cursor.fetchall()

    query = """
        SELECT *
        FROM attendance
        WHERE 1=1
    """

    values = []

    if selected_date:
        query += " AND attendance_date=%s"
        values.append(selected_date)

    if selected_subject:
        query += " AND subject_code=%s"
        values.append(selected_subject)

    query += """
        ORDER BY attendance_date DESC,
        time_in DESC
    """

    cursor.execute(query, values)
    records = cursor.fetchall()

    total_present = sum(
        1 for r in records
        if r['status'] == 'Present'
    )

    total_absent = sum(
        1 for r in records
        if r['status'] == 'Absent'
    )

    total_records = len(records)

    cursor.close()
    conn.close()

    return render_template(
        'reports.html',
        records=records,
        subjects=subjects,
        total_present=total_present,
        total_absent=total_absent,
        total_records=total_records
    )

@app.route('/export_excel')
@authenticated
def export_excel():

    conn = get_db_connection()

    query = """
        SELECT
        attendance_date,
        student_id,
        student_name,
        subject_code,
        time_in,
        status
        FROM attendance
        ORDER BY attendance_date DESC
    """

    df = pd.read_sql(query, conn)

    filename = "attendance_report.xlsx"

    df.to_excel(filename, index=False)

    conn.close()

    return send_file(
        filename,
        as_attachment=True
    )
# ==============================
# SETTINGS
# ==============================
@app.route('/settings')
@role_required('admin')
def settings():

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT * FROM admin LIMIT 1")
    admin = cursor.fetchone()

    cursor.close()
    conn.close()

    return render_template(
        'settings.html',
        admin=admin
    )

@app.route('/update_admin', methods=['POST'])
@role_required('admin')
def update_admin():

    username = request.form.get('username', '').strip()

    if not username:
        return error_page(400, "Username is required.")

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE admin
        SET username=%s
        WHERE id=1
    """, (username,))

    conn.commit()

    cursor.close()
    conn.close()

    session['user'] = username

    return redirect(url_for('settings'))


# ==============================
# CHANGE PASSWORD
# ==============================
#
# SE-16, found during this phase. This route used to update
# `admin WHERE id=1` no matter who was signed in, so an instructor had no way
# to change their own password and the one form pointed at somebody else's
# account. That blocks the forced-change flow, which has to work for both
# roles, so it is fixed here rather than deferred.
#
# GET renders the form. That is new: the access-control hook redirects an
# account flagged `must_change_password` here, and a POST-only route would
# have answered that redirect with a 405.

def _credential_table_for_session():
    """
    (table, primary-key column, primary-key value) for the signed-in account.

    Returns None for a session with no recognised role, which the caller
    turns into a 403 rather than guessing.
    """
    if session.get('role') == 'admin':
        return "admin", "id", 1

    if session.get('role') == 'instructor':
        return "instructors", "instructor_id", session.get('instructor_id')

    return None


@app.route('/change_password', methods=['GET', 'POST'])
@authenticated
def change_password():

    target = _credential_table_for_session()

    if target is None or target[2] is None:
        logger.error(
            "change_password reached with an unusable session: role=%r",
            session.get('role'),
        )
        return error_page(403)

    # `table` and `key_column` are interpolated into the statements below.
    # They come from the fixed pair of literals in
    # _credential_table_for_session(), never from the request; the key value
    # is always a bound parameter.
    table, key_column, key_value = target
    must_change = bool(session.get(MUST_CHANGE_PASSWORD))

    if request.method == 'GET':
        return render_template(
            'change_password.html',
            must_change=must_change
        )

    current_password = request.form.get('current_password', '')
    new_password = request.form.get('new_password', '')
    confirm_password = request.form.get('confirm_password', '')

    def form_error(message):
        return render_template(
            'change_password.html',
            must_change=must_change,
            error=message
        ), 400

    if new_password != confirm_password:
        return form_error("The new passwords do not match.")

    if len(new_password) < 8:
        return form_error("The new password must be at least 8 characters.")

    if is_known_default(new_password):
        return form_error(
            "That password ships with the system and is public knowledge. "
            "Choose a different one."
        )

    try:
        new_hash = hash_password(new_password)
    except PasswordTooLongError as error:
        return form_error(str(error))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute(
            f"SELECT password FROM `{table}` WHERE `{key_column}` = %s",
            (key_value,)
        )
        account = cursor.fetchone()

        if account is None or not verify_password(
            current_password, account['password']
        ):
            logger.warning(
                "Rejected password change for %r: current password incorrect",
                session.get('user'),
            )
            return form_error("The current password is incorrect.")

        cursor.execute(
            f"UPDATE `{table}` SET password = %s, must_change_password = 0 "
            f"WHERE `{key_column}` = %s",
            (new_hash, key_value)
        )
        conn.commit()

    finally:
        cursor.close()
        conn.close()

    session.pop(MUST_CHANGE_PASSWORD, None)

    logger.info("Password changed for %r", session.get('user'))

    if session.get('role') == 'admin':
        return redirect(url_for('settings'))

    return redirect(url_for('dashboard'))

# ==============================
# LOGOUT
# ==============================
@app.route('/logout')
@public
def logout():
    session.clear()
    return redirect(url_for('home'))


# SE-2. This was reachable by anyone who could reach the host: the live
# camera feed of a classroom, unauthenticated. It is loaded as an <img src>,
# so the browser sends the session cookie with it and the hook can enforce
# authentication exactly as it does for any other route.
@app.route('/video_feed')
@authenticated
def video_feed():

    return Response(
        generate_frames(),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )
# ==============================
# RUN APP
# ==============================
if __name__ == '__main__':
    # Still the Flask development server binding loopback only - that is PO-4,
    # scoped to a later phase. Only the debug flag moves to configuration here.
    app.run(debug=app_config.flask_debug)
