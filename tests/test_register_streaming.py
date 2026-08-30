"""
The export reads the register in batches, not all at once (R6, PE-8).

**A claim in three documents that the code did not support.** The
`services/reporting.py` docstring, `todo.md` PE-8 and `handover-phase-5.md`
§4.3 all said export rows "come from a server-side cursor one at a time" and
that "the sheet never holds them". The second half was true - openpyxl's
write-only mode serialises each row and drops it. The first was not:
`register_workbook()` iterated `attendance_repo.filtered()`, whose last
statement is `return cursor.fetchall()`, so the entire filtered register was
materialised as a list of dicts before the first row ever reached the sheet.

The memory behaviour PE-8 existed to remove was still there, one layer below
where it had been removed, and the figure is bound for the manuscript
(lessons.md L9 - a benchmark's assumptions are measurements too).

**No database here on purpose.** What is being tested is *how the rows are
asked for*, and a real cursor would answer either way. A fake cursor is the
only thing that can tell `fetchall()` from `fetchmany()`, which is precisely
the distinction the claim rests on. `tests/integration/test_export.py` covers
the rows themselves against a real server.
"""

from __future__ import annotations

import pytest

from repositories import attendance as attendance_repo


class FakeCursor:
    """
    A cursor that records how it was read.

    `fetchall()` raises rather than returning rows: a test that let it succeed
    would pass on the code this file exists to rule out.
    """

    def __init__(self, rows):
        self.rows = list(rows)
        self.position = 0
        self.executed = []
        self.fetchmany_sizes = []

    def execute(self, query, values=None):
        self.executed.append((query, values))

    def fetchmany(self, size):
        self.fetchmany_sizes.append(size)
        batch = self.rows[self.position:self.position + size]
        self.position += len(batch)
        return batch

    def fetchall(self):
        raise AssertionError(
            "iter_filtered() called fetchall() - the whole register was "
            "materialised before the first row reached the sheet, which is "
            "the PE-8 behaviour the export claims not to have"
        )


class ListReadingCursor(FakeCursor):
    """`filtered()`'s cursor: it is *supposed* to fetch everything."""

    def fetchall(self):
        return self.rows


def rows(count):
    return [{"id": index, "student_id": f"SEC-TEST-{index:04d}"} for index in range(count)]


def test_the_register_is_never_fetched_all_at_once():
    """The finding itself, stated as the thing that must not happen."""
    cursor = FakeCursor(rows(1200))

    consumed = list(attendance_repo.iter_filtered(cursor))

    assert len(consumed) == 1200


def test_rows_arrive_in_bounded_batches():
    cursor = FakeCursor(rows(1200))

    list(attendance_repo.iter_filtered(cursor))

    size = attendance_repo.EXPORT_FETCH_SIZE

    assert cursor.fetchmany_sizes == [size, size, size, size], (
        "expected three full batches, a partial one, and a final empty read "
        f"that ends the loop; got {cursor.fetchmany_sizes}"
    )


def test_the_first_row_is_available_before_the_last_is_fetched():
    """
    Laziness, asserted rather than assumed.

    A generator that built a list internally and yielded from it would pass
    every assertion above. This one cannot: after taking one row, only the
    first batch may have been read.
    """
    cursor = FakeCursor(rows(1200))

    stream = attendance_repo.iter_filtered(cursor)
    first = next(stream)

    assert first["id"] == 0
    assert cursor.position == attendance_repo.EXPORT_FETCH_SIZE, (
        f"{cursor.position} rows had been read from the server before the "
        "first one was handed out"
    )

    stream.close()


def test_an_empty_register_reads_once_and_stops():
    cursor = FakeCursor([])

    assert list(attendance_repo.iter_filtered(cursor)) == []
    assert len(cursor.fetchmany_sizes) == 1


def test_the_export_itself_never_materialises_the_register(monkeypatch):
    """
    R6 where it actually matters, and the reason this is not a source check.

    `tests/integration/test_export.py` cannot see this: `filtered()` and
    `iter_filtered()` return the same rows in the same order, so the workbook
    is byte-identical either way. The difference is *how much of the register
    exists at once*, which only a cursor that refuses `fetchall()` can observe.

    A test that grepped `reporting.py` for "iter_filtered" would also pass on
    the docstring above it, which names the function three times (lessons.md
    L12). So this drives `register_workbook()` and lets the cursor object.
    """
    import contextlib

    from services import reporting

    cursor = FakeCursor(
        [
            {
                "attendance_date": "2026-08-16",
                "student_id": f"SEC-TEST-{index:04d}",
                "student_name": "Nobody",
                "subject_code": "CS401",
                "section": "A",
                "time_in": None,
                "status": "Absent",
            }
            for index in range(1200)
        ]
    )

    @contextlib.contextmanager
    def fake_db_cursor(*_args, **_kwargs):
        yield cursor

    monkeypatch.setattr(reporting, "db_cursor", fake_db_cursor)

    _buffer, written = reporting.register_workbook()

    assert written == 1200
    assert cursor.fetchmany_sizes, "the export did not read in batches at all"


@pytest.mark.parametrize(
    ("selected_date", "subject_id", "expected_fragments", "expected_values"),
    [
        (None, None, [], []),
        ("2026-08-16", None, ["a.attendance_date = %s"], ["2026-08-16"]),
        (None, 3, ["a.subject_id = %s"], [3]),
        (
            "2026-08-16",
            3,
            ["a.attendance_date = %s", "a.subject_id = %s"],
            ["2026-08-16", 3],
        ),
    ],
)
def test_both_readers_build_the_same_query(
    selected_date, subject_id, expected_fragments, expected_values
):
    """
    FS-11's shape, guarded structurally.

    The export ignoring the operator's filters is what FS-11 *was*, and two
    copies of the WHERE assembly is how it would come back - a filter added to
    the screen's reader and not the spreadsheet's. They share
    `_filtered_query()`, and this fails if they stop doing so.
    """
    list_cursor = ListReadingCursor(rows(1))
    stream_cursor = FakeCursor(rows(1))

    attendance_repo.filtered(list_cursor, selected_date, subject_id)
    list(attendance_repo.iter_filtered(stream_cursor, selected_date, subject_id))

    assert list_cursor.executed == stream_cursor.executed, (
        "the screen and the spreadsheet are no longer asking the same "
        "question - that is FS-11 reopening"
    )

    query, values = stream_cursor.executed[0]

    assert values == expected_values

    for fragment in expected_fragments:
        assert fragment in query
