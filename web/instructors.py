"""
Instructor accounts.

⚠️ **Every route here catches `mysql.connector.Error` (R14).** They used to
rely on the catch-all 500 handler, which renders the right page but logs
"Unhandled exception while serving /add_instructor" and nothing about which
instructor or what was being attempted. Their neighbours in `web/students.py`
and the class-list routes already logged that context, and the difference was
an oversight rather than a decision - the log is the artefact you read after a
failed demo.
"""

from __future__ import annotations

import logging

import mysql.connector
from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from infra.db import db_cursor
from repositories import academics as academics_repo
from repositories import instructors as instructors_repo
from security.access import role_required
from security.passwords import PasswordTooLongError, hash_password
from web.errors import error_page
from web.placement import optional_id

logger = logging.getLogger(__name__)

instructors_bp = Blueprint("instructors", __name__)


def _department_from_form(cursor):
    """
    `(department_id, None)` from the form, or `(None, error_page)`.

    The department is optional - an instructor may be Unassigned, which the
    list says out loud - so a blank field is None and not a refusal. A value
    that names no department *is* refused, here, rather than surfacing as a
    foreign-key error from the INSERT.
    """
    raw = request.form.get('department_id', '').strip()

    if not raw:
        return None, None

    department_id = optional_id(raw)

    if department_id is None or not academics_repo.department_exists(
        cursor, department_id
    ):
        return None, error_page(400, "Choose a department from the list.")

    return department_id, None


@instructors_bp.route('/instructors')
@role_required('admin')
def instructors():
    try:
        with db_cursor(dictionary=True) as cursor:
            records = instructors_repo.all_instructors(cursor)
            departments = academics_repo.departments(cursor)

    except mysql.connector.Error:
        logger.exception("Could not load the instructors list")
        return error_page(500)

    return render_template(
        "instructors.html", instructors=records, departments=departments
    )


@instructors_bp.route('/manage_instructors')
@role_required('admin')
def manage_instructors():
    """
    The instructor list, grouped by department (DM-3).

    The filter arrives in the query string, so a filtered list is a link and a
    refresh resubmits nothing.
    """
    department_id = optional_id(request.args.get('department_id'))

    try:
        with db_cursor(dictionary=True) as cursor:
            records = instructors_repo.roster(
                cursor, department_id=department_id
            )
            departments = academics_repo.departments(cursor)

    except mysql.connector.Error:
        logger.exception("Could not load the instructor management list")
        return error_page(500)

    return render_template(
        "manage_instructors.html",
        instructors=records,
        departments=departments,
        selected_department=department_id,
    )


@instructors_bp.route('/search_instructor', methods=['POST'])
@role_required('admin')
def search_instructor():
    query = request.form.get('query', '')

    try:
        with db_cursor(dictionary=True) as cursor:
            records = instructors_repo.search(cursor, query)
            departments = academics_repo.departments(cursor)

    except mysql.connector.Error:
        logger.exception("Instructor search failed")
        return error_page(500)

    return render_template(
        "manage_instructors.html",
        instructors=records,
        departments=departments,
        selected_department=None,
    )


@instructors_bp.route('/add_instructor', methods=['POST'])
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
    #
    # Hashed before a connection is taken, not after: bcrypt is deliberately
    # slow, and a pooled connection held across it is one of five that no other
    # request can use.
    try:
        hashed_password = hash_password(password)
    except PasswordTooLongError as error:
        return error_page(400, str(error))

    try:
        with db_cursor(commit=True) as cursor:
            department_id, failure = _department_from_form(cursor)

            if failure is not None:
                return failure

            instructors_repo.insert(
                cursor, instructor_id, fullname, hashed_password,
                department_id,
            )

    except mysql.connector.IntegrityError:
        # The primary key refused it: that instructor ID is already taken.
        logger.warning("Refused duplicate instructor ID %r", instructor_id)
        return error_page(409, "That instructor ID is already in use.")

    except mysql.connector.Error:
        logger.exception("Could not create instructor %r", instructor_id)
        return error_page(500)

    logger.info("Instructor %r created by %r", instructor_id, session.get('user'))

    flash(
        f"{fullname} was added. They must change the password you set at "
        "their first login.",
        "success",
    )

    return redirect(url_for('instructors.manage_instructors'))


@instructors_bp.route('/edit_instructor/<instructor_id>')
@role_required('admin')
def edit_instructor(instructor_id):

    try:
        with db_cursor(dictionary=True) as cursor:
            instructor = instructors_repo.get(cursor, instructor_id)
            departments = academics_repo.departments(cursor)

    except mysql.connector.Error:
        logger.exception("Could not load instructor %r", instructor_id)
        return error_page(500)

    if instructor is None:
        return error_page(404, "That instructor was not found.")

    # FS-6: this said "edit_instructor.html", which does not exist - the file
    # is edit_instructors.html - so the route returned 500 for every request.
    # Fixed in passing because a route that 500s for the legitimate user is
    # not meaningfully "secured".
    return render_template(
        "edit_instructors.html",
        instructor=instructor,
        departments=departments,
    )


@instructors_bp.route('/update_instructor/<instructor_id>', methods=['POST'])
@role_required('admin')
def update_instructor(instructor_id):

    fullname = request.form.get('fullname', '').strip()
    password = request.form.get('password', '')

    if not fullname:
        return error_page(400, "Full name is required.")

    # Hashed before a connection is taken - see add_instructor.
    hashed_password = None

    if password:
        try:
            hashed_password = hash_password(password)
        except PasswordTooLongError as error:
            return error_page(400, str(error))

    try:
        with db_cursor(commit=True) as cursor:
            department_id, failure = _department_from_form(cursor)

            if failure is not None:
                return failure

            if hashed_password is not None:
                instructors_repo.update_details_and_password(
                    cursor, instructor_id, fullname, department_id,
                    hashed_password,
                )
            else:
                instructors_repo.update_details(
                    cursor, instructor_id, fullname, department_id
                )

    except mysql.connector.Error:
        logger.exception("Could not update instructor %r", instructor_id)
        return error_page(500)

    logger.info("Instructor %r updated by %r", instructor_id, session.get('user'))

    if hashed_password is not None:
        flash(
            f"{fullname} was updated, and must change the new password at "
            "their next login.",
            "success",
        )
    else:
        flash(f"{fullname} was updated. The password was left unchanged.",
              "success")

    return redirect(url_for('instructors.manage_instructors'))


# POST, not GET (SE-6). See the note on delete_subject.
@instructors_bp.route('/delete_instructor/<instructor_id>', methods=['POST'])
@role_required('admin')
def delete_instructor(instructor_id):

    try:
        with db_cursor(commit=True) as cursor:
            instructors_repo.delete(cursor, instructor_id)

    except mysql.connector.Error:
        logger.exception("Could not delete instructor %r", instructor_id)
        return error_page(500)

    logger.info("Instructor %r deleted by %r", instructor_id, session.get('user'))

    # ⚠️ Says what did *not* happen as well as what did. `subjects.instructor_id`
    # is ON DELETE SET NULL (RE-4), so the offerings survive - and an operator
    # who assumes otherwise will go looking for them.
    flash(
        "The instructor account was deleted. The subjects they taught were "
        "kept and are now unassigned.",
        "success",
    )

    return redirect(url_for('instructors.manage_instructors'))
