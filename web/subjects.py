"""
Subject offerings and their class lists.

The class-list screen (FS-3) is here rather than with students because the
`enrolments` table is a fact about an *offering*: who is taking CS401 section B
this term. Admin-only, deliberately - who is in a class decides whose absence
is recorded, which is the same class of decision as adding a student.
"""

from __future__ import annotations

import logging
from datetime import time, timedelta

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
from repositories import academics as academics_repo
from repositories import enrolments as enrolments_repo
from repositories import instructors as instructors_repo
from repositories import subjects as subjects_repo
from security.access import role_required
from security.paths import UnsafeStudentPathError, validate_student_id
from services.schedule import DAY_SEPARATOR, WEEKDAYS, meeting_days
from web.errors import error_page
from web.placement import optional_id, parse_placement, section_id_for

logger = logging.getLogger(__name__)

subjects_bp = Blueprint("subjects", __name__)


# The text fields both the add and update forms submit. Which section the
# offering is taught to arrives beside them as a placement - program, year
# level and section - and is resolved to a `sections.id` inside the write's
# transaction (DM-7).
SUBJECT_FIELDS = (
    'subject_code',
    'subject_name',
    'instructor_id',
    'day',
    'time_in',
    'time_out',
)

# Where the section id goes among them, in the order subjects_repo.insert()
# and update() take their arguments: after `day`.
SECTION_AT = SUBJECT_FIELDS.index('time_in')

# The two that identify the offering. The rest may legitimately be blank - a
# subject with no assigned instructor, or no scheduled time, is a real state
# the schema allows (`subjects.time_in` is TIME NULL, and RE-4 made
# instructor_id nullable on purpose).
REQUIRED_SUBJECT_FIELDS = ('subject_code', 'subject_name')

# The days a class can meet, and how `subjects.day` stores them, live in
# services/schedule.py: `/start-attendance` reads the same text to refuse a
# session on a day the subject does not meet, and the two must not drift.


def _days_from(form):
    """
    `(stored_text, None)` from the ticked day boxes, or `(None, message)`.

    Stored in week order whatever order the browser sent them, so the same
    schedule is always the same text.
    """
    chosen = set(form.getlist('day'))

    if not chosen:
        return None, "Choose at least one day the class meets."

    unknown = chosen.difference(WEEKDAYS)

    if unknown:
        return None, f"Not a day the form offers: {', '.join(sorted(unknown))}."

    return DAY_SEPARATOR.join(day for day in WEEKDAYS if day in chosen), None


@subjects_bp.app_template_filter('time_box')
def time_box(value):
    """
    A stored TIME as `<input type="time">` needs it: `HH:MM`, or `''`.

    ⚠️ MySQL hands a TIME back as a timedelta, and `str()` of seven and a half
    hours is `7:30:00` - no leading zero. The browser rejects that as invalid
    and shows the box **empty**, so editing any subject that starts before
    10:00 opened with blank times.
    """
    if value is None or value == '':
        return ''

    if isinstance(value, timedelta):
        minutes = int(value.total_seconds()) // 60
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    if isinstance(value, time):
        return value.strftime('%H:%M')

    hours, _, rest = str(value).partition(':')
    return f"{int(hours):02d}:{rest[:2]}"


def _subject_form():
    """
    `(values, placement, None)` from the form, or `(None, None, error_page)`.

    `placement` is the section the offering is taught to, as
    `web.placement.parse_placement()` returns it - the same three fields, and
    the same rules, as where a student belongs. Required: an offering for no
    section is for no program, and the Classes list on the registration form
    could then only offer it to everybody.

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

    days, problem = _days_from(request.form)

    if problem is not None:
        return None, None, error_page(400, problem)

    # ⚠️ A blank time must reach the database as NULL. This server is not in
    # strict mode, so `''` into a TIME column is stored as 00:00:00 on an
    # INSERT reporting success - and a class starting at midnight records
    # every student Late.
    values = tuple(
        days if field == 'day'
        else (value or None) if field in ('time_in', 'time_out')
        else value
        for field, value in zip(SUBJECT_FIELDS, values, strict=True)
    )

    # DM-4. The instructor is an account chosen from a list, not a typed name:
    # `instructors.id`, or None for an offering nobody is assigned to yet. A
    # typed name linked the offering to nobody, so nothing could be scoped to
    # the person who teaches it.
    instructor_at = SUBJECT_FIELDS.index('instructor_id')

    if values[instructor_at] and optional_id(values[instructor_at]) is None:
        return None, None, error_page(400, "Choose an instructor from the list.")

    values = (
        *values[:instructor_at],
        optional_id(values[instructor_at]),
        *values[instructor_at + 1:],
    )

    missing = [
        field
        for field, value in zip(SUBJECT_FIELDS, values, strict=True)
        if field in REQUIRED_SUBJECT_FIELDS and not value
    ]

    if missing:
        return None, None, error_page(
            400,
            "These fields are required: " + ", ".join(
                field.replace('_', ' ') for field in missing
            ) + ".",
        )

    placement, problem = parse_placement(request.form)

    if problem is not None:
        return None, None, error_page(400, problem)

    return values, placement, None


def _with_section(cursor, values, placement):
    """
    The repository's arguments: `values` with the section id in its place.

    None when the chosen program no longer exists. Resolved here, under the
    caller's cursor, so a section created for this offering and an offering
    that then failed to save cannot leave an empty section behind.
    """
    section_id = section_id_for(cursor, placement)

    if section_id is None:
        return None

    return (*values[:SECTION_AT], section_id, *values[SECTION_AT:])


@subjects_bp.route('/subjects')
@role_required('admin')
def subjects():
    try:
        with db_cursor(dictionary=True) as cursor:
            records = subjects_repo.all_subjects(cursor)
            instructors = instructors_repo.all_instructors(cursor)
            programs = academics_repo.programs(cursor)

    except mysql.connector.Error:
        logger.exception("Could not load the subjects list")
        return error_page(500)

    return render_template(
        "subjects.html",
        subjects=records,
        weekdays=WEEKDAYS,
        instructors=instructors,
        programs=programs,
        year_levels=academics_repo.YEAR_LEVELS,
    )


@subjects_bp.route('/add_subject', methods=['POST'])
@role_required('admin')
def add_subject():
    values, placement, failure = _subject_form()

    if failure is not None:
        return failure

    try:
        with db_cursor(commit=True) as cursor:
            arguments = _with_section(cursor, values, placement)

            if arguments is None:
                return error_page(400, "That program no longer exists.")

            subjects_repo.insert(cursor, *arguments)

    except mysql.connector.IntegrityError:
        # The foreign key refused it: the chosen instructor account was
        # deleted after the form was rendered.
        logger.warning("Refused subject %r: no such instructor", values[0])
        return error_page(409, "That instructor no longer exists.")

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

            # Only for a subject that exists: the 404 below needs nothing else.
            instructors = (
                instructors_repo.all_instructors(cursor)
                if subject is not None else []
            )
            programs = (
                academics_repo.programs(cursor)
                if subject is not None else []
            )

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

    return render_template(
        "edit_subject.html",
        subject=subject,
        weekdays=WEEKDAYS,
        days=meeting_days(subject.get("day")),
        instructors=instructors,
        programs=programs,
        year_levels=academics_repo.YEAR_LEVELS,
    )


@subjects_bp.route('/update_subject/<int:id>', methods=['POST'])
@role_required('admin')
def update_subject(id):
    values, placement, failure = _subject_form()

    if failure is not None:
        return failure

    try:
        with db_cursor(commit=True) as cursor:
            arguments = _with_section(cursor, values, placement)

            if arguments is None:
                return error_page(400, "That program no longer exists.")

            subjects_repo.update(cursor, id, *arguments)

    except mysql.connector.IntegrityError:
        logger.warning("Refused update of subject %s: no such instructor", id)
        return error_page(409, "That instructor no longer exists.")

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

            # DM-7. Only the students of the offering's own program can be
            # added, its own section first. An offering linked to no section
            # has no program, and is offered everybody as before.
            available = enrolments_repo.not_enrolled_in(
                cursor,
                subject_id,
                program_id=subject['program_id'],
                section_id=subject['section_id'],
            )

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


@subjects_bp.route('/enrol_section', methods=['POST'])
@role_required('admin')
def enrol_section():
    """
    Add every student of the offering's own section to its class list (DM-7).

    The offering names a section and so does each student, so the usual class
    list - everybody in that section - is one statement rather than one click
    per student. Students from other sections are still added one at a time,
    which is how an irregular student gets onto the list.
    """
    subject_id = optional_id(request.form.get('subject_id'))

    if subject_id is None:
        return error_page(400, "That subject could not be identified.")

    try:
        with db_cursor(dictionary=True, commit=True) as cursor:
            subject = subjects_repo.summary(cursor, subject_id)

            if subject is None:
                return error_page(404, "That subject could not be found.")

            if subject['section_id'] is None:
                return error_page(
                    400,
                    "This subject is not linked to a section yet. Edit the "
                    "subject and choose its program, year level and section.",
                )

            added = enrolments_repo.enrol_section(
                cursor, subject_id, subject['section_id']
            )

    except mysql.connector.Error:
        logger.exception("Could not enrol a section into subject %s", subject_id)
        return error_page(500)

    logger.info(
        "Enrolled section %s into subject %s: %d added",
        subject['section_id'], subject_id, added,
    )

    if added:
        flash(
            f"{added} student{'s' if added != 1 else ''} of section "
            f"{subject['section']} "
            f"{'were' if added != 1 else 'was'} added to the class list.",
            "success",
        )
    else:
        flash(
            f"Every student of section {subject['section']} was already on "
            "the class list.",
            "success",
        )

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
