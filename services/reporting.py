"""
Turning the attendance register into a spreadsheet (FS-11, PE-8).

Two findings meet here and the second is the one that shaped this module.

**FS-11** - the export ignored every filter the operator had chosen and dumped
the whole table to a fixed filename in the working directory, so two people
exporting at once overwrote each other and the file never matched the screen it
came from. Phase 4 fixed the filters.

**PE-8** - `pandas.read_sql()` was handed a raw `mysql.connector` connection.
pandas does not support that driver, falls back to a generic DBAPI2 path and
warns about it; and `read_sql` materialises the entire result as a DataFrame,
then `to_excel` builds the entire workbook in memory beside it. Three copies of
the register at once, for a file the browser downloads and the server forgets.

**What replaced it, and what "streamed" honestly means here.** An `.xlsx` file
is a ZIP container: the central directory is written last, so the bytes cannot
be handed out while the sheet is still being produced. Nothing can change that,
and claiming a streamed export would be a claim about a file format rather than
about this code. What *can* be bounded is the row buffer, and that is what
`openpyxl`'s write-only mode does - each row is serialised to the sheet's XML
stream as it arrives and then dropped, rather than being held in a cell grid
until save time.

So: rows come from the server in bounded batches, the sheet never holds them,
and what ends up in memory is one **compressed** workbook. Measured while
writing this, 5,000 rows produce 72 KB. pandas held the DataFrame, the workbook
and the register simultaneously.

⚠️ **The first half of that sentence was untrue when it was written, and the
correction is worth keeping.** It said rows came "from a server-side cursor one
at a time" while this function iterated `attendance_repo.filtered()`, whose
last statement is `return cursor.fetchall()` - so the whole filtered register
was materialised as a list of dicts before the first row reached the sheet. The
openpyxl half of the claim was real; the query half was prose. It is
`iter_filtered()` now, which yields from `fetchmany()` on an unbuffered cursor,
and the sentence describes the code (lessons.md L9 - a benchmark's assumptions
are measurements too).

**Nothing touches the disk.** The first version of this wrote a temporary file
and deleted it from a `call_on_close` hook - which did not fire under the test
client, so the file was left behind and an integration test caught it. A
cleanup path that can silently not run is worse than not needing one, and an
attendance register accumulating in a temp directory is exactly the retention
problem `docs/data_privacy.md` has no answer for. The buffer is returned
instead.
"""

from __future__ import annotations

import io
import logging

from openpyxl import Workbook

from infra.db import db_cursor
from repositories import attendance as attendance_repo

logger = logging.getLogger(__name__)

# The header row, and the register column each cell comes from. One list, so
# the two cannot drift - they were a hand-aliased SQL SELECT before, where a
# renamed column silently produced a spreadsheet with a missing field.
EXPORT_COLUMNS = (
    ("Date", "attendance_date"),
    ("Student ID", "student_id"),
    ("Student", "student_name"),
    ("Subject", "subject_code"),
    ("Section", "section"),
    ("Time In", "time_in"),
    ("Status", "status"),
)


def _cell(value):
    """
    Render one value for a spreadsheet cell.

    MySQL returns a TIME column as a `timedelta`, which openpyxl cannot write
    and which pandas used to stringify on the way past. Dates and datetimes are
    handed over as they are, so Excel formats them as dates rather than text.
    """
    if value is None:
        return ""

    if hasattr(value, "total_seconds"):
        total = int(value.total_seconds())
        return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"

    return value


def register_workbook(selected_date=None, subject_id=None):
    """
    Build the filtered register as a workbook. Returns `(buffer, row_count)`.

    The buffer is positioned at the start, ready to be handed to `send_file`.

    The count is returned rather than discarded because an export of zero rows
    is worth logging: it is nearly always a filter the operator did not mean,
    and an empty spreadsheet looks the same as a broken one.
    """
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet(title="Attendance")

    sheet.append([heading for heading, _column in EXPORT_COLUMNS])

    written = 0

    # ⚠️ One cursor held for the whole write, deliberately. The alternative -
    # fetch everything, close, then write - is the memory behaviour this
    # function exists to remove.
    with db_cursor(dictionary=True) as cursor:
        for row in attendance_repo.iter_filtered(cursor, selected_date, subject_id):
            sheet.append([_cell(row[column]) for _heading, column in EXPORT_COLUMNS])
            written += 1

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)

    return buffer, written
