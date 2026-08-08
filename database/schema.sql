-- =====================================================
-- AI Attendance System MySQL Database Schema Setup
-- Database: attendancesystem_db
-- =====================================================

CREATE DATABASE IF NOT EXISTS attendancesystem_db;
USE attendancesystem_db;

-- 1. Admin Table
--
-- `password` holds a bcrypt hash (60 characters), never a plaintext
-- password (SE-1). It is never compared in SQL: the collation here is
-- utf8mb4_general_ci, which is case-insensitive and PAD SPACE, so a SQL
-- comparison accepted `ADMIN` and `admin   ` as `admin` (SE-15).
-- Authentication fetches the row by username and verifies in Python.
--
-- `must_change_password` is set for any account still using a credential
-- this system ships with. See security/access.py.
CREATE TABLE IF NOT EXISTS admin (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(100) NOT NULL UNIQUE,
    password VARCHAR(255) NOT NULL,
    must_change_password TINYINT(1) NOT NULL DEFAULT 0
);

-- 2. Instructors Table
CREATE TABLE IF NOT EXISTS instructors (
    id INT AUTO_INCREMENT PRIMARY KEY,
    instructor_id VARCHAR(100) NOT NULL UNIQUE,
    fullname VARCHAR(255) NOT NULL,
    password VARCHAR(255) NOT NULL,
    must_change_password TINYINT(1) NOT NULL DEFAULT 0
);

-- 3. Students Table
CREATE TABLE IF NOT EXISTS students (
    student_id VARCHAR(100) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    college_department VARCHAR(255),
    program VARCHAR(255),
    year_level INT,
    section VARCHAR(100)
);

-- 4. Subjects Table
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
);

-- 5. Attendance Table
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
);

-- Seed Default Admin Account
--
-- Username `admin`, password `admin`, stored as a bcrypt hash and flagged
-- must_change_password = 1 so the account can do nothing but change it.
-- The credential is public knowledge; the flag is what stops it being a
-- working way in.
--
-- Prefer `python setup_db.py`, which generates a fresh salt. The literal
-- below is a valid bcrypt hash of `admin` and exists only so this file
-- remains runnable on its own.
INSERT INTO admin (id, username, password, must_change_password)
SELECT 1, 'admin', '$2b$12$PSll5Xge17WxsNIq9J85XuNmJU6fja5wcAby1MePT9jnKHpupJ5sa', 1
WHERE NOT EXISTS (SELECT 1 FROM admin WHERE id = 1);
