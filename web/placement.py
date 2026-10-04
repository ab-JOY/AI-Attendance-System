"""
Where a student belongs, as a form submits it (migration 009, DM-2).

Three routes take the same three fields - web registration, the edit screen and
the mobile API - and they must agree about what a valid placement is. This is
that agreement, written once.

A placement is a program, a year level and a section name. The department is
not asked for: it is a property of the program.
"""

from __future__ import annotations

from repositories import academics as academics_repo


def optional_id(value):
    """
    A positive integer from a query string or form field, or None.

    None unless the value is all digits, so a filter typed into the URL by hand
    cannot reach a query as anything but an int. The same rule as the subject
    filter in web/reports.py.
    """
    value = str(value).strip() if value is not None else ""

    return int(value) if value.isdigit() else None


def parse_placement(source):
    """
    `(placement, None)` from a form or JSON mapping, or `(None, message)`.

    `placement` is `{"program_id", "year_level", "section"}`. Nothing here
    touches the database; whether the program exists is decided where the row
    is written, inside that transaction, by `section_id_for()`.

    ⚠️ **The length is checked here, not left to the column.** This server has
    no STRICT_TRANS_TABLES, so an over-long section name would be silently
    truncated on an INSERT that reports success (lessons.md L11).
    """
    program_id = optional_id(source.get("program_id"))

    if program_id is None:
        return None, "Choose a program from the list."

    year_level = optional_id(source.get("year_level"))

    if year_level not in academics_repo.YEAR_LEVELS:
        return None, "Choose a year level from the list."

    section = str(source.get("section") or "").strip()

    if not section:
        return None, "Section is required."

    if len(section) > academics_repo.MAX_SECTION_NAME:
        return None, (
            f"Keep the section to {academics_repo.MAX_SECTION_NAME} "
            "characters or fewer."
        )

    return {
        "program_id": program_id,
        "year_level": year_level,
        "section": section,
    }, None


def section_id_for(cursor, placement):
    """
    The `sections.id` for a parsed placement, creating the section if new.

    None when the program no longer exists - the caller decides whether that
    is a refusal (the edit screen) or an Unassigned student (the end of a
    two-minute capture, where the images are already on disk).
    """
    return academics_repo.section_for(
        cursor,
        placement["program_id"],
        placement["year_level"],
        placement["section"],
    )
