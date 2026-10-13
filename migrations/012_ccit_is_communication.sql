-- =====================================================
-- 012 - CCIT is the College of Communication and Information Technology
--
-- 009 seeded it as "College of Computing and Information Technology", which
-- is what the hand-made table on the deployed database said. The user
-- corrected the name on 2026-10-14. The code, the row and its id are
-- unchanged, so every program, section, student and instructor under it
-- stays where it is.
--
-- The second statement is 011's, run again: `students.college_department` is
-- a copy of this name (DM-9) and would otherwise keep the old one.
--
-- Replay-safe: a second run writes the same values.
-- =====================================================

UPDATE departments
SET department_name = 'College of Communication and Information Technology'
WHERE department_code = 'CCIT';

UPDATE students s
JOIN sections sec ON sec.id = s.section_id
JOIN programs p ON p.id = sec.program_id
JOIN departments d ON d.id = p.department_id
SET s.college_department = d.department_name,
    s.program = p.program_code,
    s.year_level = sec.year_level,
    s.section = sec.section_name
