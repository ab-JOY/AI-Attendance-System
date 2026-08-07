import logging
import os
import shutil
import stat
import subprocess
import sys

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

from config.logging_config import configure_logging
from config.settings import settings

# app.py is the entry point, so it owns logging configuration for the process.
#
# This has to run BEFORE importing recognize_face, which loads the LBPH model
# and constructs a MediaPipe FaceMesh at module scope (PE-4, deferred to
# Phase 3) and logs the outcome of both. With no handlers attached yet those
# startup messages - including "model failed to load" - would be discarded.
# The noqa markers below come off when PE-4 is fixed and the import is cheap.
configure_logging()

from recognize_face import (  # noqa: E402
    generate_frames,
    start_camera,
    stop_camera
)
from train_model import train_model  # noqa: E402

logger = logging.getLogger(__name__)

app = Flask(__name__)

# Was hardcoded as "ai_attendance_secret_key" (SE-8). That value is in git
# history, and a known Flask secret key means session cookies can be forged.
# Now required from the environment - see .env.example.
app.secret_key = settings.secret_key


# ==============================
# DATABASE CONNECTION
# ==============================
def get_db_connection():
    return mysql.connector.connect(**settings.db_kwargs())

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
# LOGIN PAGE
# ==============================
@app.route('/')
def home():
    return render_template('login.html')


# ==============================
# LOGIN PROCESS
# ==============================
@app.route('/login', methods=['POST'])
def login():

    role = request.form['role']
    user_id = request.form['user_id']
    password = request.form['password']

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # ADMIN LOGIN
    if role == "admin":

        cursor.execute("""
            SELECT *
            FROM admin
            WHERE username=%s
            AND password=%s
        """, (user_id, password))

        admin = cursor.fetchone()

        if admin:
            session['user'] = admin['username']
            session['role'] = 'admin'

            cursor.close()
            conn.close()

            return redirect(url_for('dashboard'))

    # INSTRUCTOR LOGIN
    elif role == "instructor":

        cursor.execute("""
            SELECT *
            FROM instructors
            WHERE instructor_id=%s
            AND password=%s
        """, (user_id, password))

        instructor = cursor.fetchone()

        if instructor:
            session['user'] = instructor['fullname']
            session['role'] = 'instructor'
            session['instructor_id'] = instructor['instructor_id']

            cursor.close()
            conn.close()

            return redirect(url_for('dashboard'))

    cursor.close()
    conn.close()

    return render_template(
        'login.html',
        error='Invalid Login Credentials'
    )
# ==============================
# DASHBOARD
# ==============================
@app.route('/dashboard')
def dashboard():
    if 'user' not in session:
        return redirect(url_for('home'))

    return render_template('dashboard.html')


# ==============================
# STUDENTS PAGE
# ==============================
@app.route('/students')
def students():
    if 'user' not in session:
        return redirect(url_for('home'))

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
def capture_face():

    if 'user' not in session:
        return redirect(url_for('home'))

    # Get form data
    student_id = request.form['student_id']
    name = request.form['name']
    college_department = request.form['college_department']
    program = request.form['program']
    year_level = request.form['year_level']
    section = request.form['section']

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

        if result.returncode == 0:
            # Auto-train LBPH model after capturing dataset
            logger.info("Auto-training model after dataset capture")
            train_success, train_msg = train_model()
            logger.info("Train result: %s - %s", train_success, train_msg)

    except subprocess.CalledProcessError as error:
        if error.returncode == 2:
            logger.info("Face capture cancelled by user")
            return redirect(url_for('students'))
        logger.exception("Capture dataset script failed")
        return f"Face capture failed with exit code {error.returncode}.", 500
    except Exception as e:
        return f"Camera Error: {e}", 500

    return redirect(url_for('students'))

# ==============================
# MANAGE STUDENTS
# ==============================
@app.route('/manage_students')
def manage_students():

    if 'user' not in session:
        return redirect(url_for('home'))

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
def search_student():

    if 'user' not in session:
        return redirect(url_for('home'))

    query = request.form['query']

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
def delete_student(student_id):

    if 'user' not in session:
        return redirect(url_for('home'))

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

        # Build the student's dataset folder path.
        folder_name = f"{student['student_id']}_{student['name']}"
        dataset_path = os.path.join("dataset", folder_name)

        # Delete the dataset folder first.
        if os.path.isdir(dataset_path):
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

    except PermissionError as error:
        if conn is not None:
            conn.rollback()

        logger.exception("Delete student failed: dataset folder is in use")

        return (
            "The student's dataset folder is currently being used. "
            "Close the attendance camera, recognition program, "
            "File Explorer, and image preview, then try again.",
            500
        )

    except mysql.connector.Error as error:
        if conn is not None:
            conn.rollback()

        logger.exception("Delete student failed: database error")

        return f"Database deletion error: {error}", 500

    except OSError as error:
        if conn is not None:
            conn.rollback()

        logger.exception("Delete student failed: could not remove dataset folder")

        return f"Could not delete dataset folder: {error}", 500

    except Exception as error:
        if conn is not None:
            conn.rollback()

        logger.exception("Delete student failed")

        return f"Student deletion error: {error}", 500

    finally:
        if cursor is not None:
            cursor.close()

        if conn is not None and conn.is_connected():
            conn.close()

# ==============================
# EDIT STUDENT
# ==============================
@app.route('/edit_student/<string:student_id>')
def edit_student(student_id):

    if 'user' not in session:
        return redirect(url_for('home'))

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

    except mysql.connector.Error as error:
        logger.exception("Edit student failed: database error")
        return f"Database error: {error}", 500

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
def update_student(student_id):

    if 'user' not in session:
        return redirect(url_for('home'))

    # Read the submitted form values.
    name = request.form.get('name', '').strip()
    college_department = request.form.get(
        'college_department',
        ''
    ).strip()
    program = request.form.get('program', '').strip()
    year_level = request.form.get('year_level', '').strip()
    section = request.form.get('section', '').strip()

    # Validate required values.
    if not name:
        return "Student name is required.", 400

    if not program:
        return "Program is required.", 400

    if not section:
        return "Section is required.", 400

    try:
        year_level = int(year_level)

    except (TypeError, ValueError):
        return "Year level must be a valid number.", 400

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
            return "Student record was not found.", 404

        old_name = existing_student['name']

        # Build the old and new dataset folder paths.
        old_folder_name = f"{student_id}_{old_name}"
        new_folder_name = f"{student_id}_{name}"

        old_dataset_path = os.path.join(
            "dataset",
            old_folder_name
        )

        new_dataset_path = os.path.join(
            "dataset",
            new_folder_name
        )

        # Rename the dataset folder if the student's name changed.
        if (
            old_name != name
            and os.path.isdir(old_dataset_path)
        ):
            if os.path.exists(new_dataset_path):
                return (
                    "A dataset folder with the updated student name "
                    "already exists.",
                    409
                )

            try:
                os.rename(
                    old_dataset_path,
                    new_dataset_path
                )

                folder_was_renamed = True

            except PermissionError as error:
                logger.exception(
                    "Dataset folder rename denied by the operating system"
                )

                return (
                    "The student's dataset folder is currently in use. "
                    "Close the attendance camera, face-recognition "
                    "program, image preview, and File Explorer folder, "
                    "then try again.",
                    500
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

    except mysql.connector.Error as error:
        if conn is not None:
            conn.rollback()

        # Restore the original folder name if the SQL update failed.
        if (
            folder_was_renamed
            and new_dataset_path
            and old_dataset_path
            and os.path.isdir(new_dataset_path)
            and not os.path.exists(old_dataset_path)
        ):
            try:
                os.rename(
                    new_dataset_path,
                    old_dataset_path
                )

            except OSError as rename_error:
                logger.exception(
                    "Could not restore dataset folder after a failed update"
                )

        logger.exception("Update student failed: database error")
        return f"Database update error: {error}", 500

    except Exception as error:
        if conn is not None:
            conn.rollback()

        logger.exception("Update student failed")
        return f"Student update error: {error}", 500

    finally:
        if cursor is not None:
            cursor.close()

        if conn is not None and conn.is_connected():
            conn.close()


# ==============================
# RECAPTURE STUDENT FACE
# ==============================
@app.route('/recapture_face', methods=['POST'])
def recapture_face():

    if 'user' not in session:
        return redirect(url_for('home'))

    student_id = request.form.get(
        'student_id',
        ''
    ).strip()

    if not student_id:
        return "Student ID is required.", 400

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

    except mysql.connector.Error as error:
        logger.exception("Recapture failed: database error")
        return f"Database error: {error}", 500

    finally:
        if cursor is not None:
            cursor.close()

        if conn is not None and conn.is_connected():
            conn.close()

    if student is None:
        return "Student ID was not found.", 404

    folder_name = (
        f"{student['student_id']}_{student['name']}"
    )

    dataset_path = os.path.join(
        "dataset",
        folder_name
    )

    # Remove the student's old face dataset.
    try:
        if os.path.isdir(dataset_path):
            shutil.rmtree(
                dataset_path,
                onerror=remove_readonly_and_retry
            )

    except PermissionError as error:
        logger.exception(
            "Recapture failed: dataset folder is in use"
        )

        return (
            "The student's dataset folder is currently being used. "
            "Close the attendance camera, recognition program, "
            "image preview, and File Explorer folder, then try again.",
            500
        )

    except OSError as error:
        logger.exception("Recapture failed: could not remove old dataset folder")

        return (
            f"Could not remove the old dataset folder: {error}",
            500
        )

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
        if error.returncode == 2:
            logger.info("Face recapture cancelled by user")
            return redirect(url_for('manage_students'))

        logger.exception("Capture dataset script failed")

        return (
            f"Face capture failed with exit code "
            f"{error.returncode}.",
            500
        )

    except PermissionError as error:
        logger.exception("Permission denied starting the face-capture process")

        return (
            "Permission was denied while starting the face-capture "
            "program.",
            500
        )

    except Exception as error:
        logger.exception("Camera error during face recapture")
        return f"Camera error: {error}", 500

    return redirect(url_for('manage_students'))


# ==========================================================
# MANUAL MODEL RETRAINING ROUTE
# ==========================================================
@app.route('/train_model', methods=['POST'])
def train_model_route():
    if 'user' not in session:
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    success, message = train_model()
    return jsonify({
        "success": success,
        "message": message
    })


# ==========================================================
# SUBJECTS MODULE
# ==========================================================

@app.route('/subjects')
def subjects():

    if 'user' not in session:
        return redirect(url_for('home'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT * FROM subjects ORDER BY id DESC")
    subjects = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template("subjects.html", subjects=subjects)


@app.route('/add_subject', methods=['POST'])
def add_subject():

    if 'user' not in session:
        return redirect(url_for('home'))

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
def edit_subject(id):

    if 'user' not in session:
        return redirect(url_for('home'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT * FROM subjects WHERE id=%s", (id,))
    subject = cursor.fetchone()

    cursor.close()
    conn.close()

    return render_template("edit_subject.html", subject=subject)


@app.route('/update_subject/<int:id>', methods=['POST'])
def update_subject(id):

    if 'user' not in session:
        return redirect(url_for('home'))

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


@app.route('/delete_subject/<int:id>')
def delete_subject(id):

    if 'user' not in session:
        return redirect(url_for('home'))

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
def instructors():

    if 'user' not in session:
        return redirect(url_for('home'))

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
def add_instructor():

    if 'user' not in session:
        return redirect(url_for('home'))

    instructor_id = request.form['instructor_id']
    fullname = request.form['fullname']
    password = request.form['password']

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO instructors
        (
            instructor_id,
            fullname,
            password
        )
        VALUES (%s,%s,%s)
    """, (
        instructor_id,
        fullname,
        password
    ))

    conn.commit()

    cursor.close()
    conn.close()

    return redirect(url_for('manage_instructors'))


# ==============================
# MANAGE INSTRUCTORS
# ==============================
@app.route('/manage_instructors')
def manage_instructors():

    if 'user' not in session:
        return redirect(url_for('home'))

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
def search_instructor():

    if 'user' not in session:
        return redirect(url_for('home'))

    query = request.form['query']

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

    return render_template(
        "edit_instructor.html",
        instructor=instructor
    )
# ==============================
# UPDATE INSTRUCTOR
# ==============================
@app.route('/update_instructor/<instructor_id>', methods=['POST'])
def update_instructor(instructor_id):

    fullname = request.form['fullname']
    password = request.form['password']

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE instructors
        SET
            fullname=%s,
            password=%s
        WHERE instructor_id=%s
    """, (
        fullname,
        password,
        instructor_id
    ))

    conn.commit()

    cursor.close()
    conn.close()

    return redirect(url_for('manage_instructors'))
# ==============================
# DELETE INSTRUCTOR
# ==============================
@app.route('/delete_instructor/<instructor_id>')
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
def attendance():
    if 'user' not in session:
        return redirect(url_for('home'))

    return render_template('attendance.html')


# ==============================
# START FACE RECOGNITION CAMERA
# ==============================
@app.route('/start-attendance', methods=['POST'])
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


    except Exception as e:

        logger.exception("Camera error while starting attendance")

        return jsonify({
            "success": False,
            "message": str(e)
        })
@app.route('/stop_camera', methods=['POST'])
def stop_camera_route():

    stop_camera()

    return jsonify({
        "success": True
    })
@app.route('/end-attendance', methods=['POST'])
def end_attendance():

    if 'user' not in session:
        return redirect(url_for('home'))

    stop_camera()

    subject_code = request.form.get(
        'subject_code',
        ''
    ).strip()

    if not subject_code:
        return "Subject code is required.", 400

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

    except mysql.connector.Error as error:
        logger.exception("End attendance failed: database error")
        return f"Database error: {error}", 500

    finally:
        if cursor is not None:
            cursor.close()

        if conn is not None and conn.is_connected():
            conn.close()
# ==============================
# REPORTS
# ==============================
@app.route('/reports')
def reports():

    if 'user' not in session:
        return redirect(url_for('home'))

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
def settings():

    if 'user' not in session:
        return redirect(url_for('home'))

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
def update_admin():

    if 'user' not in session:
        return redirect(url_for('home'))

    username = request.form['username']

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

@app.route('/change_password', methods=['POST'])
def change_password():

    if 'user' not in session:
        return redirect(url_for('home'))

    current_password = request.form['current_password']
    new_password = request.form['new_password']
    confirm_password = request.form['confirm_password']

    if new_password != confirm_password:
        return "New passwords do not match."

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT * FROM admin WHERE id=1")
    admin = cursor.fetchone()

    if admin['password'] != current_password:
        cursor.close()
        conn.close()
        return "Current password is incorrect."

    cursor.execute("""
        UPDATE admin
        SET password=%s
        WHERE id=1
    """, (new_password,))

    conn.commit()

    cursor.close()
    conn.close()

    return redirect(url_for('settings'))

# ==============================
# LOGOUT
# ==============================
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))


@app.route('/video_feed')
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
    app.run(debug=settings.flask_debug)