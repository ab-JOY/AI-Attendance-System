"""The attendance register on screen, and the same register as a download."""

from __future__ import annotations

import logging
from datetime import datetime

import mysql.connector
from flask import Blueprint, render_template, request, send_file

from infra.db import db_cursor
from repositories import academics as academics_repo
from repositories import attendance as attendance_repo
from repositories import instructors as instructors_repo
from repositories import subjects as subjects_repo
from security.access import authenticated, instructor_scope
from services import reporting
from web.errors import error_page
from web.placement import optional_id

logger = logging.getLogger(__name__)

reports_bp = Blueprint("reports", __name__)


def _filters():
    """
    `(date, subject_id, instructor_id, options)` from the query string.

    `subject_id` is None unless the value is a positive integer, so a filter
    typed into the URL by hand cannot reach a query.

    `options` is everything else the register can be narrowed and ordered by,
    as the keyword arguments `attendance_repo.filtered()` takes: the student's
    `department_id`, `program_id`, `year_level` and `section_id`, and `sort`.

    ⚠️ **`instructor_id` is only a filter for an administrator (DM-5).** For a
    signed-in instructor it is their own key, taken from the session, and
    `?instructor=` is not read at all - otherwise the scope would be a default
    the URL could override. Both the screen and the export come through here,
    so the two cannot disagree about whose register this is.

    ⚠️ **The placement filters need no such care, and that is deliberate.**
    They narrow *within* the scope: an instructor who asks for a department
    they do not teach gets their own classes filtered to nothing, never
    somebody else's.
    """
    selected_date = request.args.get('date')
    subject_id = optional_id(request.args.get('subject'))

    scope = instructor_scope()

    instructor_id = (
        optional_id(request.args.get('instructor')) if scope is None else scope
    )

    options = {
        key: optional_id(request.args.get(key))
        for key in attendance_repo.PLACEMENT_FILTERS
    }

    # A key, never SQL: the repository maps it to an ORDER BY it wrote itself.
    sort = request.args.get('sort', '')

    options['sort'] = (
        sort if sort in attendance_repo.SORT_ORDERS
        else attendance_repo.DEFAULT_SORT
    )

    return selected_date, subject_id, instructor_id, options


def _placement_choices(placements):
    """
    The four dropdowns' options, derived from the sections that exist.

    Each list is the distinct values in `placements`, in the order they
    arrive, so a department or year with nothing in it is never offered.
    """
    departments, programs, years = {}, {}, set()

    for row in placements:
        departments.setdefault(row['department_id'], row)
        programs.setdefault(row['program_id'], row)
        years.add(row['year_level'])

    return {
        "departments": list(departments.values()),
        "programs": list(programs.values()),
        "year_levels": sorted(years),
        "sections": placements,
    }


@reports_bp.route('/reports')
@authenticated
def reports():
    """
    The attendance register, filtered.

    `total_absent` was structurally 0 before Phase 4: it counted Absent rows in
    a table nothing ever wrote an Absent row into (FS-4).

    An instructor sees their own classes and sessions and nothing else; an
    administrator sees everything, or one instructor's by choosing them (DM-5).
    Either can narrow and order what they see by the students' department,
    program, year level and section - an instructor is offered the ones they
    teach.
    """
    selected_date, subject_id, instructor_id, options = _filters()
    scope = instructor_scope()

    placement = {
        key: options[key] for key in attendance_repo.PLACEMENT_FILTERS
    }

    with db_cursor(dictionary=True) as cursor:
        # The subject picker offers only what the reader may see: for an
        # instructor their own offerings, for an administrator all of them.
        subjects = subjects_repo.for_report_filter(cursor, instructor_id=scope)

        # The instructor picker is the administrator's. An instructor has
        # exactly one answer and is not asked.
        instructors = (
            instructors_repo.all_instructors(cursor) if scope is None else []
        )

        choices = _placement_choices(
            academics_repo.placements(cursor, instructor_id=scope)
        )

        records = attendance_repo.filtered(
            cursor, selected_date, subject_id, instructor_id, **options
        )
        class_sessions = attendance_repo.sessions_filtered(
            cursor, selected_date, subject_id, instructor_id, **placement
        )

    total_present = sum(1 for r in records if r['status'] == 'Present')
    total_late = sum(1 for r in records if r['status'] == 'Late')
    total_absent = sum(1 for r in records if r['status'] == 'Absent')

    return render_template(
        'reports.html',
        records=records,
        class_sessions=class_sessions,
        session_limit=attendance_repo.SESSION_LIST_LIMIT,
        subjects=subjects,
        instructors=instructors,
        choices=choices,
        placement=placement,
        sort=options['sort'],
        selected_date=selected_date,
        selected_subject=subject_id,
        selected_instructor=instructor_id if scope is None else None,
        total_present=total_present,
        total_late=total_late,
        total_absent=total_absent,
        total_records=len(records)
    )


XLSX_MIMETYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


@reports_bp.route('/export_excel')
@authenticated
def export_excel():
    """
    Export the register, honouring the filters the operator chose (FS-11, PE-8).

    It used to ignore every filter and dump the whole table to a fixed filename
    in the working directory, so two people exporting at once overwrote each
    other and the file never matched the screen it was exported from. Phase 4
    fixed the filters; this closes PE-8 - pandas handed a raw DBAPI2 connection,
    materialising the register twice - and stops keeping the file.

    ⚠️ **The export never touches the disk.** The previous version wrote a
    timestamped copy into `reports/` and left it there for ever: an
    accumulating pile of attendance registers, which `docs/data_privacy.md` has
    no retention answer for. See `services/reporting.py` for why this is a
    buffer rather than a temporary file - the short version is that the first
    attempt used a temporary file with a `call_on_close` cleanup hook, the hook
    did not fire, and an integration test found the leftovers.
    """
    selected_date, subject_id, instructor_id, options = _filters()

    try:
        workbook, rows = reporting.register_workbook(
            selected_date, subject_id, instructor_id, **options
        )

    except mysql.connector.Error:
        logger.exception("Excel export failed: database error")
        return error_page(500)

    if rows == 0:
        # Not an error - an empty register is a legitimate answer - but nearly
        # always a filter the operator did not mean.
        logger.info(
            "Exported an empty register (date=%r, subject_id=%r, "
            "instructor_id=%r, %r)",
            selected_date, subject_id, instructor_id, options,
        )

    filename = (
        f"attendance_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    )

    return send_file(
        workbook,
        as_attachment=True,
        download_name=filename,
        mimetype=XLSX_MIMETYPE,
    )
