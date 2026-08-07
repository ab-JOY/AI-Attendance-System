-- =====================================================
-- AI Attendance System MySQL Database Schema Setup
-- Database: attendancesystem_db
-- =====================================================

CREATE DATABASE IF NOT EXISTS attendancesystem_db;
USE attendancesystem_db;

-- 1. Admin Table
CREATE TABLE IF NOT EXISTS admin (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(100) NOT NULL UNIQUE,
    password VARCHAR(255) NOT NULL
);

-- 2. Instructors Table
CREATE TABLE IF NOT EXISTS instructors (
    id INT AUTO_INCREMENT PRIMARY KEY,
    instructor_id VARCHAR(100) NOT NULL UNIQUE,
    fullname VARCHAR(255) NOT NULL,
    password VARCHAR(255) NOT NULL
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

-- Seed Default Admin Account (username: admin, password: admin)
INSERT INTO admin (id, username, password)
SELECT 1, 'admin', 'admin'
WHERE NOT EXISTS (SELECT 1 FROM admin WHERE id = 1);
