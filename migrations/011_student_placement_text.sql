-- =====================================================
-- 011 - A student's placement, written out as text again (DM-9)
--
-- Since 009 a student belongs to a section through `students.section_id`, and
-- the department, program and year level are read through the joins. The four
-- text columns 009 left behind - `college_department`, `program`,
-- `year_level`, `section` - stopped being written, so every student registered
-- afterwards has NULL in all four, and the ones registered before still say
-- `College of Computing`, which is not the name of any department. Opened on
-- its own, the table reads as if eight students had chosen nothing.
--
-- This fills the four columns from the section each student is in. From here
-- on `repositories/students.py::_copy_placement()` does the same on every
-- insert and edit.
--
-- ⚠️ THE COLUMNS ARE A COPY. Nothing reads them; `section_id` is the fact. A
-- later migration that renames a department, re-codes a program or renames a
-- section has to run this UPDATE again, or the copy goes stale.
--
-- ⚠️ A STUDENT WITH NO SECTION IS NOT TOUCHED - inner joins. Where 009 could
-- not place a student, these columns are the only record of what was typed.
--
-- Replay-safe: a second run writes the same values.
-- =====================================================

UPDATE students s
JOIN sections sec ON sec.id = s.section_id
JOIN programs p ON p.id = sec.program_id
JOIN departments d ON d.id = p.department_id
SET s.college_department = d.department_name,
    s.program = p.program_code,
    s.year_level = sec.year_level,
    s.section = sec.section_name
