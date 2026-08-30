"""
Student records: listing, searching, editing and deletion.

Enrolment - the part that touches a camera - is `web/enrolment.py`. This
blueprint owns the record, not the face.
"""

from __future__ import annotations

import logging

import mysql.connector
from flask import Blueprint, flash, redirect, render_template, request, url_for

from infra import dataset_store
from infra.db import db_cursor
from repositories import students as students_repo
from repositories import subjects as subjects_repo
from security.access import role_required
from security.paths import (
    UnsafeStudentPathError,
    student_dataset_path,
    validate_student_id,
    validate_student_name,
)
from web.errors import error_page

logger = logging.getLogger(__name__)

students_bp = Blueprint("students", __name__)


@students_bp.route('/students')
@role_required('admin')
def students():
    # The registration form on this page now offers the student's first class
    # list (US-10), so it needs the offerings to choose from - the same
    # selection the attendance dropdown uses.
    with db_cursor(dictionary=True) as cursor:
        records = students_repo.all_students(cursor)
        offerings = subjects_repo.for_selection(cursor)

    return render_template(
        'students.html', students=records, subjects=offerings
    )


@students_bp.route('/manage_students')
@role_required('admin')
def manage_students():
    with db_cursor(dictionary=True) as cursor:
        records = students_repo.all_students(cursor)

    return render_template("manage_students.html", students=records)


@students_bp.route('/search_student', methods=['POST'])
@role_required('admin')
def search_student():
    query = request.form.get('query', '')

    with db_cursor(dictionary=True) as cursor:
        records = students_repo.search(cursor, query)

    return render_template("manage_students.html", students=records)


@students_bp.route(
    '/delete_student/<string:student_id>',
    methods=['POST']
)
@role_required('admin')
def delete_student(student_id):

    # The hand-written rollback in each handler below is gone: db_cursor()
    # rolls back on any exception and commits only on a clean exit, and the
    # `with` unwinds before the matching `except` runs. Five copies of the same
    # two lines were five chances to forget one.
    try:
        with db_cursor(dictionary=True, commit=True) as cursor:
            student = students_repo.identity(cursor, student_id)

            if student is None:
                return redirect(url_for('students.manage_students'))

            # SE-3. This was the worst of the four sites: an unvalidated name
            # concatenated into a path and handed to shutil.rmtree(). A student
            # named `..\..\Windows\Temp` deleted that directory instead.
            # student_dataset_path() refuses anything that is not a direct
            # child of dataset/, so a bad record now fails loudly rather than
            # deleting the wrong tree.
            dataset_path = student_dataset_path(student['student_id'])

            # Delete the dataset folder first.
            #
            # Through dataset_store rather than a second `shutil.rmtree` with a
            # second read-only retry hook: app.py carried its own copy of that
            # handler beside the store's identical one, which is the shape MA-4
            # exists to delete.
            dataset_store.remove_folder(dataset_path)

            students_repo.delete(cursor, student_id)

        flash(
            f"{student['name']} was deleted, along with their attendance "
            "records and face images.",
            "success",
        )

        return redirect(url_for('students.manage_students'))

    except UnsafeStudentPathError as error:
        logger.error("Refused to delete a student with an unsafe folder: %s", error)

        return error_page(
            400,
            "This student's record cannot be turned into a safe dataset "
            "folder name, so nothing was deleted. Correct the student's "
            "details first."
        )

    except PermissionError:
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
        logger.exception("Delete student failed: database error")

        return error_page(500)

    except OSError:
        logger.exception("Delete student failed: could not remove dataset folder")

        return error_page(500, "The student's dataset folder could not be removed.")

    except Exception:
        logger.exception("Delete student failed")

        return error_page(500)


@students_bp.route('/edit_student/<string:student_id>')
@role_required('admin')
def edit_student(student_id):

    try:
        with db_cursor(dictionary=True) as cursor:
            student = students_repo.details(cursor, student_id)

        if student is None:
            return redirect(url_for('students.manage_students'))

        return render_template(
            "edit_students.html",
            student=student
        )

    except mysql.connector.Error:
        logger.exception("Edit student failed: database error")
        return error_page(500)


@students_bp.route(
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

    # SE-3. The ID from the URL was previously trusted outright. The name no
    # longer becomes a path (todo.md §7.5) but is still validated, because it
    # is stored and rendered and the allowlist is what keeps a hostile value
    # out of the database in the first place.
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

    # ⚠️ **Seventy lines of folder renaming used to live here, and their
    # absence is the point of the §7.5 migration.**
    #
    # The dataset folder was `{student_id}_{name}`, so editing a student's name
    # meant renaming a directory to match - inside the same request, with no
    # transaction covering it. That produced: an `os.rename()` under a database
    # cursor, a hand-written compensating rename in the `except` branch to undo
    # it when the UPDATE failed, a 409 for the case where the new folder name
    # already existed, and a 500 explaining that File Explorer might be holding
    # the folder open. Every one of those was a consequence of a display name
    # being load-bearing for storage.
    #
    # The folder is `dataset/{student_id}` now. A rename touches one row.
    try:
        with db_cursor(dictionary=True, commit=True) as cursor:
            changed = students_repo.update_details(
                cursor,
                student_id,
                name,
                college_department,
                program,
                year_level,
                section,
            )

            if changed == 0 and not students_repo.exists(cursor, student_id):
                # Either the student does not exist or nothing changed. Tell
                # the two apart rather than reporting a 404 for a no-op edit.
                return error_page(404, "That student record was not found.")

        flash(f"{name}'s record was updated.", "success")

        return redirect(url_for('students.manage_students'))

    except mysql.connector.Error:
        logger.exception("Update student failed: database error")
        return error_page(500)

    except Exception:
        logger.exception("Update student failed")
        return error_page(500)
