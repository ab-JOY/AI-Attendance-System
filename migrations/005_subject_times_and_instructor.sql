-- =====================================================
-- 005 - subjects: real times, and an instructor key (FS-8, RE-4)
--
-- FS-8: `subjects.time_in` and `time_out` are VARCHAR(50) while
-- `attendance.time_in` is a real TIME. Deriving Late means comparing a TIME
-- against a string, which MySQL will do by coercing the string - and coerce is
-- the operative word. Nothing has ever been marked Late; every record is
-- "Present".
--
-- ⚠️ THE CONVERSION IS NOT A `MODIFY`. Changing the column type directly would
-- work on this deployment, where the one row holds '08:00' and '10:00' - and
-- would silently turn anything it could not parse into 00:00:00 rather than
-- failing, because MariaDB is not in strict mode here. A subject whose time
-- became midnight would mark an entire class Late, from a migration that
-- reported success. So the values are converted into new columns under a
-- pattern guard, checked, and only then swapped in.
--
-- RE-4: `instructor` is free text. The backfill matches it against
-- instructors.fullname, which is the only link that exists; on this deployment
-- `instructors` is empty, so nothing matches and instructor_id stays NULL.
-- That is correct rather than a failure - the column is nullable precisely
-- because the free-text values may name someone who has no account.
-- =====================================================

ALTER TABLE subjects ADD COLUMN time_in_value TIME NULL AFTER time_in;

ALTER TABLE subjects ADD COLUMN time_out_value TIME NULL AFTER time_out;

-- Only values that actually look like a time. Anything else is left NULL and
-- is visible afterwards as a NULL rather than as midnight.
UPDATE subjects
SET time_in_value = CAST(time_in AS TIME)
WHERE time_in REGEXP '^[0-9]{1,2}:[0-9]{2}';

UPDATE subjects
SET time_out_value = CAST(time_out AS TIME)
WHERE time_out REGEXP '^[0-9]{1,2}:[0-9]{2}';

ALTER TABLE subjects DROP COLUMN time_in;

ALTER TABLE subjects DROP COLUMN time_out;

-- MariaDB gained ALTER TABLE ... RENAME COLUMN only in 10.5, and this
-- deployment is 10.4, so CHANGE is used instead. It works on MySQL 8 too.
ALTER TABLE subjects CHANGE time_in_value time_in TIME NULL;

ALTER TABLE subjects CHANGE time_out_value time_out TIME NULL;

ALTER TABLE subjects ADD COLUMN instructor_id INT NULL AFTER instructor;

UPDATE subjects s
JOIN instructors i ON i.fullname = s.instructor
SET s.instructor_id = i.id
WHERE s.instructor_id IS NULL;

-- ON DELETE SET NULL: deleting an instructor account must not delete the
-- subjects they taught. The free-text `instructor` column is kept beside it
-- for exactly the subjects whose instructor has no account.
ALTER TABLE subjects
    ADD CONSTRAINT fk_subjects_instructor
    FOREIGN KEY (instructor_id) REFERENCES instructors (id)
    ON DELETE SET NULL
