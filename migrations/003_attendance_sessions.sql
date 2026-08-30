-- =====================================================
-- 003 - Attendance sessions as a first-class record
--
-- A session was previously nothing at all. `/start-attendance` set a string on
-- an in-memory object and `/end-attendance` read it back; nothing recorded
-- that a session had happened, who ran it, when it started, or when it ended.
-- The only trace was the attendance rows themselves, which cannot distinguish
-- "no session was held today" from "a session was held and nobody came" - and
-- those are very different facts for an attendance system to be unable to
-- state.
--
-- It also gives the Absent rows in 004 something to hang from: absence is a
-- fact about a session, not about a date. Without this, marking a student
-- absent for a day the class never met would look identical to marking them
-- absent for one they missed.
--
-- `started_by` is the operator's username as a plain string, deliberately not
-- a foreign key: it is an audit trail, and an audit trail that disappears when
-- the account is deleted is not one.
-- =====================================================

CREATE TABLE IF NOT EXISTS attendance_sessions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    subject_id INT NOT NULL,
    session_date DATE NOT NULL,
    started_at DATETIME NOT NULL,
    ended_at DATETIME NULL,
    started_by VARCHAR(100) NULL,

    INDEX idx_sessions_subject_date (subject_id, session_date),

    CONSTRAINT fk_sessions_subject
        FOREIGN KEY (subject_id) REFERENCES subjects (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
