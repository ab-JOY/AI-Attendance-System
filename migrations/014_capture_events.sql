-- =====================================================
-- 014 - A history of face captures, for the administrator's Activity page
--
-- Requested by the user on 2026-10-29. Capturing a face is the most
-- sensitive thing this system does: it collects biometric data, which RA
-- 10173 treats as sensitive personal information. Instructors can capture
-- the students of their own classes, so the administrator must be able to
-- answer "who captured whose face, and when" - which until now only the
-- server log could.
--
-- One row per capture that ends, however it ends: completed, cancelled,
-- failed while saving, or abandoned (no frame for the idle timeout). A
-- capture still running is not here; the Activity page reads that one live.
--
-- `performed_by` is the signed-in user's name as a string, not a foreign
-- key, for the same reason as attendance_sessions.started_by: a history that
-- disappears with the account is not a history. `student_id` is not a
-- foreign key either - a cancelled first capture has no student row, and
-- that attempt is exactly what the history must keep.
--
-- No image, and nothing derived from one, is stored here.
--
-- Replay-safe.
-- =====================================================

CREATE TABLE IF NOT EXISTS capture_events (
    id INT AUTO_INCREMENT PRIMARY KEY,
    student_id VARCHAR(100) NOT NULL,
    student_name VARCHAR(255) NULL,
    performed_by VARCHAR(100) NULL,
    kind VARCHAR(50) NOT NULL,
    outcome VARCHAR(20) NOT NULL,
    images INT NOT NULL,
    occurred_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_capture_events_time (occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
