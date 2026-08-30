-- =====================================================
-- 004 - attendance joins on keys, and cannot hold a duplicate (RE-3, RE-4)
--
-- Three problems in one table, and they share a cause: nothing in it was a
-- key.
--
-- RE-3: duplicate suppression was a read-then-write check in Python, a
-- textbook TOCTOU race under concurrent recognition. The fix is a UNIQUE
-- constraint, and once it exists the check in save_attendance() is deleted -
-- not kept alongside it.
--
-- RE-4: `subject_code` was a free-text VARCHAR. A typo at session start
-- created attendance nobody could find, because nothing checked the code
-- against `subjects` (FS-7). It cannot simply become a foreign key either:
-- `subjects` is keyed by a surrogate id and carries course and section, so two
-- sections of CS401 are two rows sharing one code and the code is not unique.
-- The join therefore moves to subject_id. Decided with the user, 2026-08-11.
--
-- `student_name` was a copy of students.name taken at write time, and
-- update_student never propagated a rename into it, so it went stale silently.
-- With a real key to join on, the copy is deleted rather than synchronised.
--
-- ⚠️ SAFETY. READ THIS BEFORE EDITING ANY STATEMENT BELOW.
--
-- The backfill maps subject_code to subjects.id only where the code is
-- unambiguous; a code belonging to two offerings cannot be resolved from the
-- attendance row alone, and guessing is how a student ends up marked present
-- for a section they are not in. Rows it cannot map are left NULL.
--
-- Two earlier drafts of this file tried to stop there and let the schema catch
-- the rest. Both were wrong, and both were caught by running them against a
-- scratch database with a deliberately ambiguous subject_code rather than by
-- reasoning about them:
--
--   1. `MODIFY subject_id INT NOT NULL` does NOT fail on a NULL here. This
--      server's sql_mode is NO_ZERO_IN_DATE,NO_ZERO_DATE,NO_ENGINE_SUBSTITUTION
--      - no STRICT_TRANS_TABLES - so it silently converts NULL to 0. Same class
--      of silent coercion that 005 avoids for times.
--   2. Adding the foreign key first does not catch it either. With
--      foreign_key_checks = 1, the ALTER TABLE rebuild did *not* re-validate
--      the constraint, and the run finished "successfully" leaving an
--      attendance row with subject_id 0 referencing a subject that does not
--      exist - a corrupt referential state produced by a migration that
--      reported success.
--
-- And by the time anything noticed, the DROP COLUMN statements had already
-- committed - DDL cannot be rolled back - so subject_code was gone and there
-- was no way to re-derive it.
--
-- The gate is therefore explicit, deterministic, and runs before anything is
-- dropped. If it fires, the only residue is two added nullable columns:
-- subject_code is untouched and the fix is to resolve the rows and re-run.
-- =====================================================

ALTER TABLE attendance ADD COLUMN subject_id INT NULL AFTER student_id;

ALTER TABLE attendance ADD COLUMN session_id INT NULL AFTER subject_id;

UPDATE attendance a
JOIN (
    SELECT subject_code, MIN(id) AS subject_id, COUNT(*) AS offerings
    FROM subjects
    GROUP BY subject_code
) resolved ON resolved.subject_code = a.subject_code AND resolved.offerings = 1
SET a.subject_id = resolved.subject_id
WHERE a.subject_id IS NULL;

-- THE GATE.
--
-- CASE evaluates its branches lazily, so when there are no unmapped rows this
-- is a no-op returning 0. When there are, it evaluates a subquery that yields
-- two rows where one is required, and the server raises error 1242,
-- "Subquery returns more than 1 row". Verified both ways on this server.
--
-- ⚠️ If you are reading this because migration 004 failed at this statement
-- with error 1242, that message is generic and this is what it means: some
-- attendance rows have a subject_code matching no subject, or matching more
-- than one offering. Find them with
--
--   SELECT a.subject_code, COUNT(*) FROM attendance a
--   LEFT JOIN (SELECT subject_code, COUNT(*) n FROM subjects GROUP BY subject_code) s
--          ON s.subject_code = a.subject_code AND s.n = 1
--   WHERE s.subject_code IS NULL GROUP BY a.subject_code;
--
-- resolve them, then drop the two added columns and run again. Nothing has
-- been lost: subject_code and student_name are still there.
SELECT CASE
    WHEN (SELECT COUNT(*) FROM attendance WHERE subject_id IS NULL) > 0
    THEN (SELECT 1 FROM (SELECT 1 UNION ALL SELECT 2) unmapped_rows_exist)
    ELSE 0
END AS abort_if_any_attendance_row_is_unmapped;

-- Safe now: the gate above proved there are no NULLs to coerce.
ALTER TABLE attendance MODIFY subject_id INT NOT NULL;

ALTER TABLE attendance
    ADD CONSTRAINT fk_attendance_subject
    FOREIGN KEY (subject_id) REFERENCES subjects (id)
    ON DELETE CASCADE;

ALTER TABLE attendance
    ADD CONSTRAINT fk_attendance_student
    FOREIGN KEY (student_id) REFERENCES students (student_id)
    ON DELETE CASCADE ON UPDATE CASCADE;

-- Nullable, and ON DELETE SET NULL: an attendance record outlives the session
-- row that produced it. Losing the session must not lose the attendance.
ALTER TABLE attendance
    ADD CONSTRAINT fk_attendance_session
    FOREIGN KEY (session_id) REFERENCES attendance_sessions (id)
    ON DELETE SET NULL;

-- RE-3. One attendance record per student, per offering, per day. The
-- read-then-write check in save_attendance() is removed in the same change.
ALTER TABLE attendance
    ADD CONSTRAINT uq_attendance_student_subject_date
    UNIQUE (student_id, subject_id, attendance_date);

-- Everything below this line is destructive, and nothing above it was.

-- The old non-unique index was on (attendance_date, subject_code).
ALTER TABLE attendance DROP INDEX idx_date_subject;

ALTER TABLE attendance DROP COLUMN subject_code;

ALTER TABLE attendance DROP COLUMN student_name
