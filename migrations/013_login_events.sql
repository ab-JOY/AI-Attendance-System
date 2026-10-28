-- =====================================================
-- 013 - A history of sign-ins, for the administrator's Activity page
--
-- Requested by the user on 2026-10-28: the administrator should see what the
-- system is doing - who signed in, and when. Until now a sign-in reached only
-- the server log, which nobody reads from the website.
--
-- One row per attempt: success, a wrong password, or refused while locked
-- out. `channel` is 'web' for administrators and instructors, 'app' for
-- students on the phone app.
--
-- `username` is what was typed, not a foreign key, for the same reason as
-- attendance_sessions.started_by: a history that disappears with the account
-- is not a history. ⚠️ On a failed attempt it can be anything the visitor
-- typed - shown escaped, never trusted.
--
-- No IP address and no device details are kept. The page answers "who and
-- when"; storing more personal data than that question needs would be the
-- opposite of RA 10173's proportionality principle.
--
-- Replay-safe.
-- =====================================================

CREATE TABLE IF NOT EXISTS login_events (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(100) NOT NULL,
    role VARCHAR(20) NULL,
    outcome VARCHAR(20) NOT NULL,
    channel VARCHAR(10) NOT NULL,
    occurred_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_login_events_time (occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
