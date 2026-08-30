-- =====================================================
-- 001 - Baseline
--
-- The schema as it stood at the end of Phase 3, reproduced exactly so this
-- file applies cleanly to the existing deployed database as well as creating
-- a fresh one. It replaces database/schema.sql and the duplicated CREATE
-- TABLE blocks that were in setup_db.py (PO-5).
--
-- Everything here is `IF NOT EXISTS`, so recording it against a database that
-- already has these tables is a no-op, which is the point: the deployed
-- database is at this state already.
--
-- What this baseline deliberately does NOT do is fix anything. No foreign
-- keys, no unique constraint on attendance, subjects.time_in still VARCHAR.
-- Those are 002 onward, so the diff that introduces each one is readable on
-- its own.
--
-- The deployment server is XAMPP's bundled database (basedir C:/xampp/mysql),
-- which reports version 10.4.32-MariaDB. Every migration in this directory
-- therefore keeps to syntax that MariaDB 10.4 and MySQL 8 both accept, so
-- nothing here depends on settling which one it is called. In practice that
-- means: no ALTER TABLE ... RENAME COLUMN (MariaDB gained it in 10.5 - use
-- CHANGE), no functional indexes, and no utf8mb4_0900_* collations.
-- =====================================================

-- 1. Admin
--
-- `password` holds a bcrypt hash (60 characters), never a plaintext password
-- (SE-1). It is never compared in SQL: the collation is utf8mb4_general_ci,
-- which is case-insensitive and PAD SPACE, so a SQL comparison accepted
-- `ADMIN` and `admin   ` as `admin` (SE-15). Authentication fetches the row by
-- username and verifies in Python.
--
-- `must_change_password` is set for any account still using a credential this
-- system ships with. See security/access.py.
CREATE TABLE IF NOT EXISTS admin (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(100) NOT NULL UNIQUE,
    password VARCHAR(255) NOT NULL,
    must_change_password TINYINT(1) NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2. Instructors
CREATE TABLE IF NOT EXISTS instructors (
    id INT AUTO_INCREMENT PRIMARY KEY,
    instructor_id VARCHAR(100) NOT NULL UNIQUE,
    fullname VARCHAR(255) NOT NULL,
    password VARCHAR(255) NOT NULL,
    must_change_password TINYINT(1) NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3. Students
CREATE TABLE IF NOT EXISTS students (
    student_id VARCHAR(100) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    college_department VARCHAR(255),
    program VARCHAR(255),
    year_level INT,
    section VARCHAR(100)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 4. Subjects
--
-- A row here is a *section offering*: it carries course and section as well as
-- the code, so two sections of CS401 are two rows sharing one subject_code.
-- That is why subject_code is not unique and cannot be a join key - see 003.
CREATE TABLE IF NOT EXISTS subjects (
    id INT AUTO_INCREMENT PRIMARY KEY,
    subject_code VARCHAR(100) NOT NULL,
    subject_name VARCHAR(255) NOT NULL,
    instructor VARCHAR(255),
    day VARCHAR(100),
    course VARCHAR(100),
    section VARCHAR(100),
    time_in VARCHAR(50),
    time_out VARCHAR(50)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 5. Attendance
CREATE TABLE IF NOT EXISTS attendance (
    id INT AUTO_INCREMENT PRIMARY KEY,
    student_id VARCHAR(100) NOT NULL,
    student_name VARCHAR(255) NOT NULL,
    subject_code VARCHAR(100) NOT NULL,
    attendance_date DATE NOT NULL,
    time_in TIME NOT NULL,
    status VARCHAR(50) NOT NULL,
    INDEX idx_student (student_id),
    INDEX idx_date_subject (attendance_date, subject_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
