"""
Subject offerings and their class lists.

The class-list screen (FS-3) is here rather than with students because the
`enrolments` table is a fact about an *offering*: who is taking CS401 section B
this term. Who is in a class decides whose absence is recorded, so the class
list belongs to the people responsible for that class: an administrator, for
any offering, and the instructor it is assigned to, for their own (expert
panel request, 2026-10-23). An instructor only picks from students who are
already registered; they cannot add an offering, a program or a section.
"""

from __future__ import annotations

import logging
import re
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
from security.access import instructor_scope, role_required
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

# The fields that belong to one section rather than to the subject. A subject
# is one code and one name; each section it is taught to has its own
# instructor and meets on its own days at its own times - PE1 for BSCS 1A
# and for BSCE 1A are two offerings, often with two instructors.
SECTION_FIELDS = ('instructor_id', 'day', 'time_in', 'time_out')

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


class _Refused(Exception):
    """
    A refusal decided inside a write's transaction.

    ⚠️ **Raised, never returned.** `db_cursor(commit=True)` commits on *any*
    clean exit, an early `return` included, and rolls back only on an
    exception. The Add form writes one offering per section, so a refusal at
    the third section returned from inside the block would keep the first two.
    """

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# The Add form takes one subject - code, name, instructor - and the sections it
# is taught to, each with its own days and times: `sections-<n>-program_id`,
# `sections-<n>-day` and so on. One offering is written per section, because
# each section has its own class list, its own register and its own schedule,
# and Late is derived from that schedule (FS-19).
#
# A form with no `sections-<n>-` fields is read as one section from the
# unprefixed names, which is what the Edit form submits.
SECTION_ROW = re.compile(r"^sections-(\d+)-")

# Bounds the work one request can ask for. A program has a handful of
# sections; a form with more than this is not one an operator filled in.
MAX_SECTIONS_PER_SUBJECT = 20


class _SectionRow:
    """One section of the Add form, read through the unprefixed field names."""

    def __init__(self, form, number):
        self._form = form
        self._prefix = f"sections-{number}-"

    def get(self, key, default=None):
        return self._form.get(self._prefix + key, default)

    def getlist(self, key):
        return self._form.getlist(self._prefix + key)


def _section_rows(form):
    """The form's section rows in the order they were numbered, or `[form]`."""
    numbers = sorted({
        int(match.group(1))
        for match in map(SECTION_ROW.match, form.keys())
        if match is not None
    })

    if not numbers:
        return [form]

    return [_SectionRow(form, number) for number in numbers]


def _subject_form():
    """
    `(values, placement, None)` from the form, or `(None, None, error_page)`.

    One section, from the unprefixed field names - the Edit form. The Add form
    goes through `_subject_forms()`, which takes several.

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
    values, placement, problem = _offering_form(request.form, request.form)

    if problem is not None:
        return None, None, error_page(400, problem)

    return values, placement, None


def _offering_form(form, row):
    """
    `(values, placement, None)` for one section `row` of `form`, or
    `(None, None, message)`.

    The subject's code, name and instructor are read from `form`; the days,
    the times and the placement from `row`, which is `form` itself for a
    one-section form.
    """
    values = tuple(
        (row if field in SECTION_FIELDS else form).get(field, '').strip()
        for field in SUBJECT_FIELDS
    )

    days, problem = _days_from(row)

    if problem is not None:
        return None, None, problem

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
        return None, None, "Choose an instructor from the list."

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
        return None, None, "These fields are required: " + ", ".join(
            field.replace('_', ' ') for field in missing
        ) + "."

    placement, problem = parse_placement(row)

    if problem is not None:
        return None, None, problem

    return values, placement, None


def _subject_forms():
    """
    `([(values, placement), ...], None)` - one per section - or `(None, error_page)`.

    A refusal names the section it is about when there is more than one, so
    the operator knows which row to fix.
    """
    rows = _section_rows(request.form)

    if len(rows) > MAX_SECTIONS_PER_SUBJECT:
        return None, error_page(
            400,
            f"Add at most {MAX_SECTIONS_PER_SUBJECT} sections at a time.",
        )

    offerings = []
    seen = set()

    for number, row in enumerate(rows, start=1):
        values, placement, problem = _offering_form(request.form, row)

        if problem is not None:
            if len(rows) > 1:
                problem = f"Section {number}: {problem}"
            return None, error_page(400, problem)

        # The unique key on `sections` is case-insensitive, so `4b` is `4B`.
        key = (
            placement['program_id'],
            placement['year_level'],
            placement['section'].casefold(),
        )

        if key in seen:
            return None, error_page(
                400,
                f"Section {number} repeats an earlier section: Year "
                f"{placement['year_level']} section {placement['section']} "
                "is listed twice.",
            )

        seen.add(key)
        offerings.append((values, placement))

    return offerings, None


def _with_section(cursor, values, placement, excluding=None):
    """
    The repository's arguments: `values` with the section id in its place.

    Resolved here, under the caller's cursor, so a section created for this
    offering and an offering that then failed to save cannot leave an empty
    section behind.

    Raises `_Refused` when the chosen program no longer exists, or when this
    subject code is already offered to that section - two offerings of one
    subject to one section would split its students across two class lists
    and two registers. `excluding` is the offering being edited, which is
    allowed to keep its own section.
    """
    section_id = section_id_for(cursor, placement)

    if section_id is None:
        raise _Refused(400, "That program no longer exists.")

    if subjects_repo.is_offered_to(cursor, values[0], section_id, excluding):
        raise _Refused(
            409,
            f"{values[0]} is already offered to Year "
            f"{placement['year_level']} section {placement['section']}. "
            "Edit that one instead.",
        )

    return (*values[:SECTION_AT], section_id, *values[SECTION_AT:])


def _same_name(one, other):
    return " ".join(one.split()).casefold() == " ".join(other.split()).casefold()


def _name_for_code(cursor, code, name, excluding=None):
    """
    The name to store for `code`: `name`, or the one its other offerings carry.

    A subject's name belongs to its code (DM-11). PE1 taught to BSCS and to
    Engineering is one subject, and two spellings of its name would list it
    as two subjects and split it in every report. A name differing only in
    case or spacing is taken as the existing one; anything else raises
    `_Refused`.
    """
    existing = subjects_repo.name_for_code(cursor, code, excluding)

    if existing is None:
        return name

    if _same_name(existing, name):
        return existing

    raise _Refused(
        409,
        f"{code} is already named \"{existing}\" on its other sections. "
        "Use that name, or a different subject code.",
    )


def _with_name(values, name):
    """`values` with the subject name replaced."""
    return (values[0], name, *values[2:])


@subjects_bp.route('/subjects')
@role_required('admin')
def subjects():
    """
    The subject list, and the Add form.

    `?add_to=<id>` opens the form filled in from that offering - its code,
    name, instructor, program and year level - so another section of the
    same subject is one row to finish rather than a form to retype.
    """
    add_to = optional_id(request.args.get('add_to'))

    try:
        with db_cursor(dictionary=True) as cursor:
            records = subjects_repo.all_subjects(cursor)
            instructors = instructors_repo.all_instructors(cursor)
            programs = academics_repo.programs(cursor)
            prefill = (
                subjects_repo.get(cursor, add_to)
                if add_to is not None else None
            )

    except mysql.connector.Error:
        logger.exception("Could not load the subjects list")
        return error_page(500)

    return render_template(
        "subjects.html",
        subjects=records,
        known_subjects={
            record['subject_code']: record['subject_name'] for record in records
        },
        prefill=prefill or {},
        weekdays=WEEKDAYS,
        instructors=instructors,
        programs=programs,
        year_levels=academics_repo.YEAR_LEVELS,
    )


@subjects_bp.route('/add_subject', methods=['POST'])
@role_required('admin')
def add_subject():
    """
    One subject, taught to one or more sections: one offering per section.

    All or nothing. A refusal at any section writes none of them, so the
    operator fixes that row and submits the same form again.
    """
    offerings, failure = _subject_forms()

    if failure is not None:
        return failure

    code = offerings[0][0][0]

    try:
        with db_cursor(commit=True) as cursor:
            name = _name_for_code(cursor, code, offerings[0][0][1])

            for values, placement in offerings:
                subjects_repo.insert(
                    cursor,
                    *_with_section(cursor, _with_name(values, name), placement),
                )

    except _Refused as refusal:
        logger.warning("Refused subject %r: %s", code, refusal.message)
        return error_page(refusal.status, refusal.message)

    except mysql.connector.IntegrityError:
        # The foreign key refused it: the chosen instructor account was
        # deleted after the form was rendered.
        logger.warning("Refused subject %r: no such instructor", code)
        return error_page(409, "That instructor no longer exists.")

    except mysql.connector.Error:
        logger.exception("Could not add subject %r", code)
        return error_page(500)

    sections = ", ".join(placement['section'] for _, placement in offerings)

    logger.info(
        "Subject %r added for %d section(s) (%s) by %r",
        code, len(offerings), sections, session.get('user'),
    )

    flash(
        f"{code} was added for "
        + (f"section {sections}" if len(offerings) == 1
           else f"{len(offerings)} sections ({sections})")
        + ". Use Class List on each to say who is taking it - "
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

    code, name = values[0], values[1]
    renamed = 0

    try:
        with db_cursor(commit=True) as cursor:
            # DM-11. Renaming an offering that keeps its code renames the
            # subject: every other section of it too. Moving an offering onto
            # a code that is already another name is refused instead - that
            # is a different subject, not a rename.
            current_code = subjects_repo.code_of(cursor, id)
            current_code = current_code[1] if current_code else None
            keeps_code = (
                current_code is not None
                and current_code.casefold() == code.casefold()
            )

            if not keeps_code:
                name = _name_for_code(cursor, code, name, excluding=id)

            subjects_repo.update(
                cursor, id,
                *_with_section(cursor, _with_name(values, name), placement, id),
            )

            if keeps_code:
                renamed = subjects_repo.rename_code(cursor, code, name, excluding=id)

    except _Refused as refusal:
        logger.warning("Refused update of subject %s: %s", id, refusal.message)
        return error_page(refusal.status, refusal.message)

    except mysql.connector.IntegrityError:
        logger.warning("Refused update of subject %s: no such instructor", id)
        return error_page(409, "That instructor no longer exists.")

    except mysql.connector.Error:
        logger.exception("Could not update subject %s", id)
        return error_page(500)

    logger.info("Subject %s updated by %r", id, session.get('user'))

    if renamed:
        logger.info("Subject %r renamed to %r on %d other offering(s)", code, name, renamed)

    flash(
        f"{code} was updated."
        + (f" Its name was changed on {renamed} other "
           f"section{'s' if renamed != 1 else ''} too." if renamed else ""),
        "success",
    )

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


# What an instructor is told when a request names an offering assigned to
# someone else. Their pages only link their own, so reaching this means the
# offering was reassigned after the page loaded, or the URL was typed by hand.
NOT_YOUR_CLASS_LIST = (
    "That subject is not assigned to you, so you cannot change its class list."
)


def _refuse_other_instructors(cursor, subject_id):
    """
    A 403 page when the signed-in instructor does not teach this offering, or
    None when they may use its class list.

    Always None for an administrator, without touching the database.

    ⚠️ **This is the guard, on every class-list route, read and write.** The
    links on the Face Capture page only point at the instructor's own subjects,
    but the routes take the subject id from the URL or the form, and that is
    what a forged request would change.
    """
    instructor_id = instructor_scope()

    if instructor_id is None:
        return None

    if subjects_repo.is_taught_by(cursor, subject_id, instructor_id):
        return None

    logger.warning(
        "Refused class-list access: %r is not the instructor of subject %s",
        session.get('user'), subject_id,
    )

    return error_page(403, NOT_YOUR_CLASS_LIST)


@subjects_bp.route('/subject_enrolments/<int:subject_id>')
@role_required('admin', 'instructor')
def subject_enrolments(subject_id):
    """Who is taking this offering, and who could be added to it."""
    try:
        with db_cursor(dictionary=True) as cursor:
            refusal = _refuse_other_instructors(cursor, subject_id)

            if refusal is not None:
                return refusal

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
@role_required('admin', 'instructor')
def enrol_student():
    """
    Add one student to one offering.

    An offering is taught to one section, and its students are added freely.
    A student of the same program in another year level or section - an
    irregular student taking a back subject or one off their block - is added
    only when the form says `irregular=1`, which the page sends from its own
    "Add as irregular" button. Another program's student is refused.
    """
    parsed, failure = _class_list_form()

    if failure is not None:
        return failure

    subject_id, student_id = parsed
    as_irregular = request.form.get('irregular') == '1'

    try:
        # Every refusal below returns before anything is written, so the
        # commit on leaving the block has nothing to commit.
        with db_cursor(dictionary=True, commit=True) as cursor:
            refusal = _refuse_other_instructors(cursor, subject_id)

            if refusal is not None:
                return refusal

            placement = enrolments_repo.placement(cursor, student_id, subject_id)

            if placement is None:
                return error_page(409, "That student or subject no longer exists.")

            if placement == enrolments_repo.OTHER_PROGRAM:
                logger.warning(
                    "Refused enrolment of %s into %s: another program",
                    student_id, subject_id,
                )
                return error_page(
                    400,
                    f"{student_id} is not in the program this subject is "
                    "offered to.",
                )

            if placement == enrolments_repo.IRREGULAR and not as_irregular:
                logger.warning(
                    "Refused enrolment of %s into %s: another section, "
                    "not marked irregular",
                    student_id, subject_id,
                )
                return error_page(
                    400,
                    f"{student_id} is not in the section this subject is "
                    "taught to. Add them as an irregular student if they "
                    "are taking it.",
                )

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

    irregular = placement == enrolments_repo.IRREGULAR

    logger.info(
        "Enrolled %s into subject %s%s",
        student_id, subject_id, " as irregular" if irregular else "",
    )

    flash(
        f"{student_id} was added to the class list"
        f"{' as an irregular student' if irregular else ''}.",
        "success",
    )

    return redirect(url_for('subjects.subject_enrolments', subject_id=subject_id))


@subjects_bp.route('/enrol_section', methods=['POST'])
@role_required('admin', 'instructor')
def enrol_section():
    """
    Add every student of the offering's own section to its class list (DM-7).

    The offering names a section and so does each student, so the usual class
    list - everybody in that section - is one statement rather than one click
    per student. Students from other sections are added one at a time, as
    irregular students (`enrol_student`).
    """
    subject_id = optional_id(request.form.get('subject_id'))

    if subject_id is None:
        return error_page(400, "That subject could not be identified.")

    try:
        with db_cursor(dictionary=True, commit=True) as cursor:
            refusal = _refuse_other_instructors(cursor, subject_id)

            if refusal is not None:
                return refusal

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
@role_required('admin', 'instructor')
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
            refusal = _refuse_other_instructors(cursor, subject_id)

            if refusal is not None:
                return refusal

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
