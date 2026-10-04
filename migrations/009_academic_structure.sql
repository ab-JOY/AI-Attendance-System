-- =====================================================
-- 009 - Academic structure: departments, programs, sections (DM-1..DM-4)
--
-- A student's department, program, year level and section were four unrelated
-- strings on `students`, typed from dropdowns hardcoded in two templates and a
-- mobile screen. Nothing made them agree with each other or with anything else:
-- the deployed database held `College of Computing` for every student, which
-- is not the name of any department it knows. Instructors belonged to nothing.
--
-- DM-1: `departments` and `programs` ALREADY EXIST on the deployed database.
-- They were created by hand on 2026-09-13, outside this directory, so no
-- migration records them, no code reads them, and a fresh install has neither.
-- That is PO-5 again - a schema that differs by how the database was made.
-- The two CREATE statements below reproduce the deployed definitions exactly
-- and are `IF NOT EXISTS`, and the seed rows are `INSERT IGNORE` against the
-- unique codes, so this file is a no-op on those tables where they exist and
-- builds them where they do not.
--
-- The hierarchy is department -> program -> section, and a section carries its
-- year level: "BSCS, 4th year, section B" is one row. A student points at a
-- section and everything above it is read through the joins, so the four facts
-- can no longer disagree.
--
-- ⚠️ THIS FILE DROPS NOTHING. `students.college_department`, `program`,
-- `year_level` and `section` are left exactly as they were and are simply no
-- longer read. A student whose typed program is not a `programs.program_code`
-- cannot be placed, is left with `section_id` NULL ("Unassigned" on screen),
-- and those columns are then the only record of what was typed. Dropping them
-- is a separate migration with a check in front of it (todo.md §7.7) - DDL
-- cannot be rolled back, so everything before the first DROP is the part that
-- is still recoverable (lessons.md L11).
--
-- ⚠️ THE DEPARTMENT IS READ FROM THE PROGRAM, NOT FROM THE OLD TEXT. The old
-- form let any department be paired with any program, so the text was never
-- evidence of anything. A student is placed by `program` alone.
-- =====================================================

CREATE TABLE IF NOT EXISTS departments (
    id INT AUTO_INCREMENT PRIMARY KEY,
    department_code VARCHAR(50) NOT NULL,
    department_name VARCHAR(255) NOT NULL,
    UNIQUE KEY department_code (department_code),
    UNIQUE KEY department_name (department_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS programs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    department_id INT NOT NULL,
    program_code VARCHAR(100) NOT NULL,
    program_name VARCHAR(255) NOT NULL,
    UNIQUE KEY program_code (program_code),
    CONSTRAINT fk_program_department
        FOREIGN KEY (department_id) REFERENCES departments (id)
        ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- The rows the deployed tables hold, so a fresh install offers the same
-- choices. IGNORE: both columns are unique, so an existing row is left alone
-- rather than duplicated or overwritten.
INSERT IGNORE INTO departments (department_code, department_name) VALUES
    ('CTHM', 'College of Tourism and Hospitality Management'),
    ('CAS', 'College of Arts and Science'),
    ('CTE', 'College of Teachers Education'),
    ('CIT', 'College of Industrial Technology'),
    ('CCIT', 'College of Computing and Information Technology'),
    ('CON', 'College of Nursing'),
    ('COE', 'College of Engineering'),
    ('CCJ', 'College of Criminal Justice'),
    ('CBAPA', 'College of Business Accountancy and Public Administration');

-- One statement per program rather than a join against a derived table of
-- literals: on MySQL 8 a derived column and a table column can carry different
-- collations, and comparing the two raises "Illegal mix of collations". A
-- literal compared with a column never does.
INSERT IGNORE INTO programs (department_id, program_code, program_name)
SELECT id, 'BSTOURISM', 'Bachelor of Science in Tourism Management'
FROM departments WHERE department_code = 'CTHM';

INSERT IGNORE INTO programs (department_id, program_code, program_name)
SELECT id, 'BSBIO', 'Bachelor of Science in Biology'
FROM departments WHERE department_code = 'CAS';

INSERT IGNORE INTO programs (department_id, program_code, program_name)
SELECT id, 'BEED', 'Bachelor of Elementary Education'
FROM departments WHERE department_code = 'CTE';

INSERT IGNORE INTO programs (department_id, program_code, program_name)
SELECT id, 'BSIT', 'Bachelor of Science in Industrial Technology'
FROM departments WHERE department_code = 'CIT';

INSERT IGNORE INTO programs (department_id, program_code, program_name)
SELECT id, 'BSCS', 'Bachelor of Science in Computer Science'
FROM departments WHERE department_code = 'CCIT';

INSERT IGNORE INTO programs (department_id, program_code, program_name)
SELECT id, 'BSN', 'Bachelor of Science in Nursing'
FROM departments WHERE department_code = 'CON';

INSERT IGNORE INTO programs (department_id, program_code, program_name)
SELECT id, 'BSCE', 'Bachelor of Science in Civil Engineering'
FROM departments WHERE department_code = 'COE';

INSERT IGNORE INTO programs (department_id, program_code, program_name)
SELECT id, 'BSCRIM', 'Bachelor of Science in Criminology'
FROM departments WHERE department_code = 'CCJ';

INSERT IGNORE INTO programs (department_id, program_code, program_name)
SELECT id, 'BSBA', 'Bachelor of Science in Business Administration'
FROM departments WHERE department_code = 'CBAPA';

-- A section is a program, a year level and a name. The unique key is what
-- makes "find or create" one statement in repositories/academics.py, and the
-- column collation makes it case-insensitive, so `4b` and `4B` are one section.
CREATE TABLE IF NOT EXISTS sections (
    id INT AUTO_INCREMENT PRIMARY KEY,
    program_id INT NOT NULL,
    year_level INT NOT NULL,
    section_name VARCHAR(100) NOT NULL,
    UNIQUE KEY uq_sections_program_year_name (program_id, year_level, section_name),
    CONSTRAINT fk_sections_program
        FOREIGN KEY (program_id) REFERENCES programs (id)
        ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Every section the existing students are in. Only complete placements: a
-- student with a program and no section has nowhere to be put.
INSERT IGNORE INTO sections (program_id, year_level, section_name)
SELECT DISTINCT p.id, s.year_level, TRIM(s.section)
FROM students s
JOIN programs p ON p.program_code = TRIM(s.program)
WHERE s.year_level IS NOT NULL
AND s.year_level > 0
AND TRIM(COALESCE(s.section, '')) <> '';

ALTER TABLE students ADD COLUMN section_id INT NULL;

UPDATE students s
JOIN programs p ON p.program_code = TRIM(s.program)
JOIN sections sec
    ON sec.program_id = p.id
    AND sec.year_level = s.year_level
    AND sec.section_name = TRIM(s.section)
SET s.section_id = sec.id
WHERE s.section_id IS NULL;

-- ON DELETE SET NULL: removing a section must not remove its students. They
-- become Unassigned, which is visible on the list and fixable from Edit.
ALTER TABLE students
    ADD CONSTRAINT fk_students_section
    FOREIGN KEY (section_id) REFERENCES sections (id)
    ON DELETE SET NULL;

-- DM-3. An instructor belongs to a department. Left NULL here: the only
-- evidence available is which subjects they teach, and inferring a department
-- from that is a guess about a person. It is set on Manage Instructors.
ALTER TABLE instructors ADD COLUMN department_id INT NULL;

ALTER TABLE instructors
    ADD CONSTRAINT fk_instructors_department
    FOREIGN KEY (department_id) REFERENCES departments (id)
    ON DELETE SET NULL;

-- DM-4. Migration 005 ran this backfill when `instructors` was empty, and the
-- subject form went on writing the free-text name, so on the deployed database
-- every subject had `instructor_id` NULL while every typed name matched an
-- account exactly. Reports scoped to an instructor are scoped by this key.
--
-- Only an unambiguous name: two accounts sharing a fullname are left unlinked
-- rather than one of them being handed the other's classes.
UPDATE subjects s
JOIN (
    SELECT fullname, MIN(id) AS id
    FROM instructors
    GROUP BY fullname
    HAVING COUNT(*) = 1
) only_one ON only_one.fullname = s.instructor
SET s.instructor_id = only_one.id
WHERE s.instructor_id IS NULL
