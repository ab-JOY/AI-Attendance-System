-- =====================================================
-- 002 - Enrolments: the missing core relation (FS-3, RE-4)
--
-- There has never been a student-to-subject relation anywhere in this schema.
-- The consequence is FS-3, and it was demonstrated rather than argued: with
-- subject CS401 (BSCS, section B), /end-attendance reported the unrelated
-- `test-id` student as Absent, because it iterates *every row in students*.
-- Every subject's register is therefore the whole school.
--
-- A row here is "this student is taking this offering". `subjects` is keyed by
-- a surrogate id and carries course and section, so a row in it is a section
-- offering rather than a subject - which is why the link is to `subjects.id`
-- and not to `subject_code`. See 004.
--
-- ON DELETE CASCADE on both sides: an enrolment is meaningless without either
-- end of it. This is also the first foreign key in the database (RE-4).
-- =====================================================

CREATE TABLE IF NOT EXISTS enrolments (
    student_id VARCHAR(100) NOT NULL,
    subject_id INT NOT NULL,
    enrolled_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (student_id, subject_id),

    -- The primary key already indexes (student_id, subject_id); this one
    -- serves the other direction, which is the common query: everybody
    -- enrolled in a subject, asked once per session end.
    INDEX idx_enrolments_subject (subject_id),

    CONSTRAINT fk_enrolments_student
        FOREIGN KEY (student_id) REFERENCES students (student_id)
        ON DELETE CASCADE ON UPDATE CASCADE,

    CONSTRAINT fk_enrolments_subject
        FOREIGN KEY (subject_id) REFERENCES subjects (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
