"""
Subject offerings and their class lists.

The class-list screen (FS-3) is here rather than with students because the
`enrolments` table is a fact about an *offering*: who is taking CS401 section B
this term. Admin-only, deliberately - who is in a class decides whose absence
is recorded, which is the same class of decision as adding a student.
"""

from __future__ import annotations

import logging

import mysql.connector
from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from infra.db import db_cursor
from repositories import enrolments as enrolments_repo
from repositories import subjects as subjects_repo
from security.access import role_required
from security.paths import UnsafeStudentPathError, validate_student_id
from web.errors import error_page

logger = logging.getLogger(__name__)

subjects_bp = Blueprint("subjects", __name__)


# The eight fields both the add and update forms submit, in the order
# subjects_repo.insert() and update() take them.
SUBJECT_FIELDS = (
    'subject_code',
    'subject_name',
    'instructor',
    'day',
    'course',
    'section',
    'time_in',
    'time_out',
)

# The two that identify the offering. The rest may legitimately be blank - a
# subject with no assigned instructor, or no scheduled time, is a real state
# the schema allows (`subjects.time_in` is TIME NULL, and RE-4 made
# instructor_id nullable on purpose).
REQUIRED_SUBJECT_FIELDS = ('subject_code', 'subject_name')


def _subject_form():
    """
    `(values, None)` from the form, or `(None, error_page)`.

    ⚠️ This used to be `request.form['subject_code']` and seven more like it. A
    missing field raised `BadRequestKeyError`, which Flask turns into a bare
    400 naming nothing - the operator was told the request could not be
    processed and had to guess which of eight fields was at fault. Every other
    form route in this application validates and names the field; the shape
    below is `_class_list_form()`'s, further down this file.
    """
    values = tuple(
        request.form.get(field, '').strip() for field in SUBJECT_FIELDS
    )

    missing = [
        field
        for field, value in zip(SUBJECT_FIELDS, values, strict=True)
        if field in REQUIRED_SUBJECT_FIELDS and not value
    ]

    if missing:
        return None, error_page(
            400,
            "These fields are required: " + ", ".join(
                field.replace('_', ' ') for field in missing
            ) + ".",
        )

    return values, None


@subjects_bp.route('/subjects')
@role_required('admin')
def subjects():
    try:
        with db_cursor(dictionary=True) as cursor:
            records = subjects_repo.all_subjects(cursor)

    except mysql.connector.Error:
        logger.exception("Could not load the subjects list")
        return error_page(500)

    return render_template("subjects.html", subjects=records)


@subjects_bp.route('/add_subject', methods=['POST'])
@role_required('admin')
def add_subject():
    values, failure = _subject_form()

    if failure is not None:
        return failure

    try:
        with db_cursor(commit=True) as cursor:
            subjects_repo.insert(cursor, *values)

    except mysql.connector.Error:
        logger.exception("Could not add subject %r", values[0])
        return error_page(500)

    logger.info("Subject %r added by %r", values[0], session.get('user'))

    flash(
        f"{values[0]} was added. Use Class List to say who is taking it - "
        "a subject with no class list records nobody.",
        "success",
    )

    return redirect(url_for('subjects.subjects'))


@subjects_bp.route('/edit_subject/<int:id>')
@role_required('admin')
def edit_subject(id):
    try:
        with db_cursor(dictionary=True) as cursor:
            subject = subjects_repo.get(cursor, id)

    except mysql.connector.Error:
        logger.exception("Could not load subject %s for editing", id)
        return error_page(500)

    # ⚠️ A missing subject used to reach the template as None, and
    # edit_subject.html calls url_for(..., id=subject.id) - a Jinja Undefined
    # in a URL build raises, so /edit_subject/999999 answered 500 with
    # `jinja2.UndefinedError: 'None' has no attribute 'id'`. Every sibling
    # route - subject_enrolments, edit_student, edit_instructor - handles this;
    # this one was missed. A stale bookmark is a 404, not a server fault.
    if subject is None:
        return error_page(404, "That subject could not be found.")

    return render_template("edit_subject.html", subject=subject)


@subjects_bp.route('/update_subject/<int:id>', methods=['POST'])
@role_required('admin')
def update_subject(id):
    values, failure = _subject_form()

    if failure is not None:
        return failure

    try:
        with db_cursor(commit=True) as cursor:
            subjects_repo.update(cursor, id, *values)

    except mysql.connector.Error:
        logger.exception("Could not update subject %s", id)
        return error_page(500)

    logger.info("Subject %s updated by %r", id, session.get('user'))

    flash(f"{values[0]} was updated.", "success")

    return redirect(url_for('subjects.subjects'))


# POST, not GET (SE-6). A destructive action behind a GET is triggerable by an
# <img src>, a link prefetch or a crawler - no form submission required. The
# template link became an inline POST form with a CSRF token.
@subjects_bp.route('/delete_subject/<int:id>', methods=['POST'])
@role_required('admin')
def delete_subject(id):
    try:
        with db_cursor(commit=True) as cursor:
            subjects_repo.delete(cursor, id)

    except mysql.connector.Error:
        logger.exception("Could not delete subject %s", id)
        return error_page(500)

    logger.info("Subject %s deleted by %r", id, session.get('user'))

    flash(
        "The subject was deleted, along with its class list and attendance "
        "records.",
        "success",
    )

    return redirect(url_for('subjects.subjects'))


# ==========================================================
# CLASS LISTS (FS-3)
#
# The `enrolments` table added in migration 002 is the missing relation that
# made /end-attendance mark every student in the database absent for every
# subject. A join table nobody can populate would leave every register empty,
# which is a different wrong answer rather than a right one - so this is the
# screen that makes the schema usable.
# ==========================================================


@subjects_bp.route('/subject_enrolments/<int:subject_id>')
@role_required('admin')
def subject_enrolments(subject_id):
    """Who is taking this offering, and who could be added to it."""
    try:
        with db_cursor(dictionary=True) as cursor:
            subject = subjects_repo.summary(cursor, subject_id)

            if subject is None:
                return error_page(404, "That subject could not be found.")

            enrolled = enrolments_repo.enrolled_in(cursor, subject_id)
            available = enrolments_repo.not_enrolled_in(cursor, subject_id)

    except mysql.connector.Error:
        logger.exception("Could not load the class list for subject %s", subject_id)
        return error_page(500)

    return render_template(
        'subject_enrolments.html',
        subject=subject,
        enrolled=enrolled,
        available=available,
    )


def _class_list_form():
    """
    `(subject_id, student_id)` from the form, or an error page.

    Both class-list routes validate identically, and the pair used to be
    written out twice with one of them missing its log line.
    """
    subject_id = request.form.get('subject_id', '').strip()
    student_id = request.form.get('student_id', '').strip()

    if not subject_id.isdigit():
        return None, error_page(400, "That subject could not be identified.")

    try:
        student_id = validate_student_id(student_id)
    except UnsafeStudentPathError as error:
        logger.warning("Rejected class-list change: %s", error)
        return None, error_page(400, str(error))

    return (int(subject_id), student_id), None


@subjects_bp.route('/enrol_student', methods=['POST'])
@role_required('admin')
def enrol_student():
    """Add one student to one offering."""
    parsed, failure = _class_list_form()

    if failure is not None:
        return failure

    subject_id, student_id = parsed

    try:
        with db_cursor(commit=True) as cursor:
            enrolments_repo.enrol(cursor, student_id, subject_id)

    except mysql.connector.IntegrityError:
        # A foreign key refused it: the student or the subject has been
        # deleted since the page was rendered.
        logger.warning(
            "Refused enrolment of %s into %s: no such student or subject",
            student_id, subject_id,
        )
        return error_page(409, "That student or subject no longer exists.")

    except mysql.connector.Error:
        logger.exception("Could not enrol %s into subject %s", student_id, subject_id)
        return error_page(500)

    logger.info("Enrolled %s into subject %s", student_id, subject_id)

    flash(f"{student_id} was added to the class list.", "success")

    return redirect(url_for('subjects.subject_enrolments', subject_id=subject_id))


@subjects_bp.route('/unenrol_student', methods=['POST'])
@role_required('admin')
def unenrol_student():
    """
    Remove one student from one offering.

    ⚠️ This does not delete their attendance - see the note on
    `repositories.enrolments.unenrol`.
    """
    parsed, failure = _class_list_form()

    if failure is not None:
        return failure

    subject_id, student_id = parsed

    try:
        with db_cursor(commit=True) as cursor:
            enrolments_repo.unenrol(cursor, student_id, subject_id)

    except mysql.connector.Error:
        logger.exception("Could not unenrol %s from subject %s", student_id, subject_id)
        return error_page(500)

    logger.info("Unenrolled %s from subject %s", student_id, subject_id)

    # ⚠️ Names what is kept. Removing somebody from a class list is a statement
    # about the future; their attendance stays, and an operator expecting a
    # deletion needs to be told it did not happen.
    flash(
        f"{student_id} was removed from the class list. Their existing "
        "attendance records were kept.",
        "success",
    )

    return redirect(url_for('subjects.subject_enrolments', subject_id=subject_id))
