-- =====================================================
-- 010 - An offering belongs to a section (DM-7, DM-8; todo.md §7.7.2)
--
-- DM-7. `subjects.course` and `subjects.section` were two typed strings. A
-- student belongs to a `sections` row since 009, but an offering belonged to
-- nothing, so nothing could say which program a subject is for: the Classes
-- list on the registration form offered every subject to every student, and
-- told two offerings of one subject apart by printing the typed section after
-- the name.
--
-- An offering now points at the section it is taught to - `subjects.section_id`
-- - and its program and year level are read through that section, exactly as
-- a student's are. The typed columns are left in place and are only what the
-- screens fall back to for an offering this file could not link.
--
-- ⚠️ THIS FILE DROPS NOTHING, for the reason 009 gives: an offering whose
-- typed course and section do not name exactly one section is left with
-- `section_id` NULL, and the text is then the only record of what was meant.
--
-- ⚠️ NOT REPLAY-SAFE PAST THE `ALTER`, like 005 and 009: a second run fails on
-- the duplicate column. Nothing before the constraint depends on the data.
-- =====================================================

ALTER TABLE subjects ADD COLUMN section_id INT NULL;

-- Only an unambiguous section: the typed text has no year level, so "BSCS,
-- section B" names nothing when first year and second year each have a B.
-- Those offerings are left unlinked rather than handed to one of the two -
-- the same rule as the instructor backfill in 009.
--
-- Run BEFORE the program code changes below, so a typed course is read
-- against the codes that existed when it was typed.
UPDATE subjects s
JOIN programs p ON p.program_code = TRIM(s.course)
JOIN (
    SELECT program_id, section_name, MIN(id) AS id
    FROM sections
    GROUP BY program_id, section_name
    HAVING COUNT(*) = 1
) only_one
    ON only_one.program_id = p.id
    AND only_one.section_name = TRIM(s.section)
SET s.section_id = only_one.id
WHERE s.section_id IS NULL;

-- ON DELETE SET NULL: removing a section must not remove the offerings taught
-- to it, or their registers. They become unlinked, which Subjects says.
ALTER TABLE subjects
    ADD CONSTRAINT fk_subjects_section
    FOREIGN KEY (section_id) REFERENCES sections (id)
    ON DELETE SET NULL;

-- DM-8. `BSIT` was Industrial Technology, under the College of Industrial
-- Technology, and there was no Information Technology program at all - in a
-- college named for it. `program_code` is unique, so the two cannot share the
-- code: Industrial Technology takes a new one and keeps its row, its id and
-- every section and student already under it.
--
-- Matched on the name as well as the code, so a database where `BSIT` already
-- means something else is left alone - and the INSERT IGNORE below then does
-- nothing either, because the code is taken.
UPDATE programs
SET program_code = 'BSINDTECH'
WHERE program_code = 'BSIT'
AND program_name = 'Bachelor of Science in Industrial Technology';

INSERT IGNORE INTO programs (department_id, program_code, program_name)
SELECT id, 'BSIT', 'Bachelor of Science in Information Technology'
FROM departments WHERE department_code = 'CCIT'
