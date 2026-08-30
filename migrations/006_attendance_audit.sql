-- =====================================================
-- 006 - An audit trail for attendance corrections (FS-10)
--
-- FS-10: there is no way to correct attendance. A false negative - the system
-- failing to recognise a student who was present - cannot be fixed by the
-- instructor, so the register is wrong and stays wrong.
--
-- The override itself is a screen. What has to exist first is the record of
-- it, because an attendance system where a human can silently change a
-- biometric determination is worse than one where they cannot change it at
-- all. Every correction names who made it, when, what it was before, and why.
--
-- `changed_by` is a username string rather than a foreign key, for the same
-- reason as attendance_sessions.started_by: an audit trail that disappears
-- when the account is deleted is not an audit trail.
--
-- ON DELETE CASCADE from attendance is deliberate and worth stating: if the
-- attendance row is gone - because the student was deleted, and RA 10173 says
-- that erasure has to be real - the audit rows about it go too. The trail
-- exists to explain a record that exists.
-- =====================================================

CREATE TABLE IF NOT EXISTS attendance_audit (
    id INT AUTO_INCREMENT PRIMARY KEY,
    attendance_id INT NOT NULL,
    changed_by VARCHAR(100) NOT NULL,
    changed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    old_status VARCHAR(50) NULL,
    new_status VARCHAR(50) NOT NULL,
    reason VARCHAR(255) NULL,

    INDEX idx_audit_attendance (attendance_id),

    CONSTRAINT fk_audit_attendance
        FOREIGN KEY (attendance_id) REFERENCES attendance (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
