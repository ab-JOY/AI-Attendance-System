"""The attendance register on screen, and the same register as a download."""

from __future__ import annotations

import logging
from datetime import datetime

import mysql.connector
from flask import Blueprint, render_template, request, send_file

from infra.db import db_cursor
from repositories import attendance as attendance_repo
from repositories import subjects as subjects_repo
from security.access import authenticated
from services import reporting
from web.errors import error_page

logger = logging.getLogger(__name__)

reports_bp = Blueprint("reports", __name__)


def _filters():
    """
    `(date, subject_id)` from the query string.

    `subject_id` is None unless the value is a positive integer, so a filter
    typed into the URL by hand cannot reach a query.
    """
    selected_date = request.args.get('date')
    selected_subject = request.args.get('subject')

    subject_id = (
        int(selected_subject)
        if selected_subject and str(selected_subject).isdigit()
        else None
    )

    return selected_date, selected_subject, subject_id


@reports_bp.route('/reports')
@authenticated
def reports():
    """
    The attendance register, filtered.

    `total_absent` was structurally 0 before Phase 4: it counted Absent rows in
    a table nothing ever wrote an Absent row into (FS-4).
    """
    selected_date, selected_subject, subject_id = _filters()

    with db_cursor(dictionary=True) as cursor:
        subjects = subjects_repo.for_report_filter(cursor)
        records = attendance_repo.filtered(cursor, selected_date, subject_id)

    total_present = sum(1 for r in records if r['status'] == 'Present')
    total_late = sum(1 for r in records if r['status'] == 'Late')
    total_absent = sum(1 for r in records if r['status'] == 'Absent')

    return render_template(
        'reports.html',
        records=records,
        subjects=subjects,
        selected_date=selected_date,
        selected_subject=selected_subject,
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
    selected_date, _selected_subject, subject_id = _filters()

    try:
        workbook, rows = reporting.register_workbook(selected_date, subject_id)

    except mysql.connector.Error:
        logger.exception("Excel export failed: database error")
        return error_page(500)

    if rows == 0:
        # Not an error - an empty register is a legitimate answer - but nearly
        # always a filter the operator did not mean.
        logger.info(
            "Exported an empty register (date=%r, subject_id=%r)",
            selected_date, subject_id,
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
