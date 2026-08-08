# Data Privacy

**System:** AI-Based Facial Recognition Attendance System
**Last reviewed:** 2026-08-08 (Phase 2)
**Legal frame:** Republic Act 10173, the Data Privacy Act of 2012 (Philippines)

This document exists because the system processes **sensitive personal
information** as RA 10173 defines it, and because an examiner will ask how
that is handled. It is written to be accurate rather than reassuring: §7
lists what the system does **not** do, and that section is as important as
the rest.

---

## 1. What is collected, and why

| Data | Where it lives | Why it is needed |
|---|---|---|
| Face images (100 per student) | `dataset/{student_id}_{name}/` | Training data for the LBPH recogniser |
| Biometric template (LBPH histograms) | `trainer/trainer.yml` | Derived from the images; used to recognise a face at attendance time |
| Label map | `trainer/labels.txt` | Maps a model label to a student ID and name |
| Student ID, name, department, programme, year, section | MySQL `students` | Identifies who an attendance record belongs to |
| Attendance events (student, subject, date, time, status) | MySQL `attendance` | The purpose of the system |
| Administrator and instructor credentials | MySQL `admin`, `instructors` | Access control |
| Application log | `logs/app.log` | Operational diagnosis and an audit trail |

**Face images and the biometric templates derived from them are sensitive
personal information** under RA 10173 §3(l). Everything else is ordinary
personal information. The distinction matters: sensitive personal
information requires the data subject's **consent**, and processing it
without a lawful basis carries criminal penalties under §§25–32.

### Purpose limitation

The declared purpose is **recording class attendance**. The data must not be
used for anything else — not identification outside class, not behavioural
monitoring, not sharing with third parties — without a fresh consent that
names the new purpose.

---

## 2. Consent

**Required before enrolment, for every student.** Enrolment captures a face,
and a face is biometric data; there is no lawful basis for capturing one here
other than consent.

The consent record must state, in language the student understands:

1. **What** is captured — 100 still images of their face, and the numeric
   template derived from them.
2. **Why** — to mark them present in class automatically.
3. **Where it is stored** — on the institution's machine, in `dataset/` and
   `trainer/`.
4. **How long** it is kept (§4).
5. That consent may be **withdrawn**, how to do it, and that withdrawal
   triggers erasure (§5).
6. That refusing consent means attendance is taken manually instead, **with
   no penalty**. Consent that cannot be refused is not consent.

For students under 18, consent comes from a parent or guardian.

> **Current status: not implemented in software.** There is no consent field
> in the `students` table and no consent screen in the enrolment flow. Until
> there is, consent must be collected and filed **on paper** before any
> `/capture_face` request is made, and the file retained for as long as the
> data is. Adding a consent record to the schema belongs with the Phase 4
> data-model work.

---

## 3. Who can access what

Enforced in `security/access.py`; every route is classified and the
classification is asserted by `tests/test_route_security.py`.

| Role | Can reach |
|---|---|
| Anonymous | The login page only |
| Instructor | Dashboard, attendance sessions, the live camera feed during a session, reports, their own password |
| Administrator | Everything, including enrolment, deletion, instructor management and export |

Notes on the boundaries:

- **Nobody browses the face images through the application.** There is no
  route that serves a file out of `dataset/`. Access to the images is access
  to the machine's filesystem, which is an operating-system control, not an
  application one.
- **`/video_feed` is authenticated** (SE-2). Before Phase 2 it was not, and
  anyone who could reach the host could watch the classroom.
- Credentials are bcrypt hashes and are **never rendered in a page** (SE-17).

---

## 4. Retention

| Data | Retention |
|---|---|
| Face images and biometric template | For the duration of the student's enrolment in the institution, and no longer |
| Attendance records | The institution's academic records retention period |
| Application log | 5 rotated files of 5 MB (`LOG_BACKUP_COUNT`, `LOG_MAX_BYTES`) |

The biometric data has the shortest justified life of anything here: once a
student is no longer attending classes, there is no purpose that requires
their face. **Delete the dataset folder and retrain at the end of a student's
enrolment.** Attendance history does not depend on the images — it lives in
the `attendance` table and survives deletion.

> **Current status: manual.** Nothing expires automatically. Retention is an
> operator responsibility until a scheduled review exists.

---

## 5. Erasure, and how to actually do it

A student may withdraw consent at any time. Erasure then has **three** parts,
and missing the third leaves their biometric template in service:

1. **Delete the student** through *Manage Students → Delete*. This removes
   the row from `students`, deletes their attendance records, and removes
   `dataset/{student_id}_{name}/`.
2. **Retrain the model.** *Students → Train Model*, or `python -c "from
   train_model import train_model; train_model()"`. Until this runs,
   `trainer/trainer.yml` still contains the histograms computed from their
   face, and `trainer/labels.txt` still names them.
3. **Remove the backup generation.** `train_model.py` keeps one previous
   model as `trainer/trainer.yml.bak` and `trainer/labels.txt.bak` so a bad
   retrain can be reverted. That backup still contains the erased student.
   Either delete the `.bak` pair, or retrain twice so the backup is itself
   post-erasure.

Verify by confirming the student no longer appears in `trainer/labels.txt`.

---

## 6. Security measures in place

- **Credentials** are bcrypt hashes, verified in Python. They are never
  compared in SQL, because the `utf8mb4_general_ci` collation made that
  comparison case- and trailing-space-insensitive (SE-15).
- **Every route** is authenticated and role-checked by default; an
  unclassified route is refused rather than served (SE-2, SE-4, SE-5).
- **Destructive actions** require POST and a CSRF token (SE-6, SE-7).
- **Dataset paths** are built by a single validated helper that proves the
  resolved path is inside `dataset/`, so a crafted student name can no longer
  reach `shutil.rmtree()` outside it (SE-3).
- **Session cookies** are HttpOnly, SameSite=Lax and expire after 30 minutes
  (SE-11).
- **Failed logins** are counted per account and address; five failures lock
  the account for 15 minutes (SE-13).
- **Errors** show a generic page. Database messages go to the log, not to the
  browser (SE-10).
- **Face images and templates are excluded from version control.**
  `dataset/` and `trainer/` are gitignored, and this is checked before every
  commit. Git history is permanent and copies to every clone, so a single
  accidental commit is not undoable by deleting the file afterwards.

---

## 7. What this system does **not** do

Stated plainly, because an evaluation that omits these is not credible.

1. **No encryption at rest.** Face images are ordinary JPEG files on disk and
   the biometric template is a plain YAML file. Anyone with file access to
   the machine — or a stolen laptop, or an unencrypted backup — has all of
   it. Full-disk encryption on the host is the mitigation available today.
2. **No consent record in the system.** See §2. Paper only.
3. **No access log for the biometric data specifically.** `logs/app.log`
   records logins, role refusals, enrolment and deletion, which is an audit
   trail for *application* actions. It cannot record someone opening
   `dataset/` in File Explorer.
4. **No automated retention or erasure.** Both are operator actions (§4, §5).
5. **No transport encryption.** The system runs on Flask's development server
   over plain HTTP (PO-4). On a single machine that is loopback traffic; the
   moment it is served to another machine, credentials and the camera stream
   cross the network in the clear. `SESSION_COOKIE_SECURE` exists in
   configuration for that day and defaults to off because enabling it over
   HTTP would prevent anyone logging in.
6. **Liveness detection is defeatable** (SE-12). The challenge is one of two
   fixed head poses with no depth, texture or blink check, so a phone playing
   a recording of an enrolled student passes it. This is a limitation of the
   attendance guarantee rather than of privacy, but it is material to any
   claim about anti-spoofing.
7. **No data-protection officer, breach-notification procedure, or
   registration with the National Privacy Commission.** These are
   institutional obligations under RA 10173 that sit outside the software.

---

## 8. RA 10173 mapping

| Provision | How it is addressed | Gap |
|---|---|---|
| §11 — general data privacy principles (transparency, legitimate purpose, proportionality) | Purpose is declared and narrow (§1); only the data needed to recognise and record is collected | Transparency depends on the paper consent form |
| §12 — criteria for lawful processing (personal information) | Consent; contract with the institution | — |
| §13 — sensitive personal information | Consent required before enrolment (§2) | Not enforced in software |
| §16 — rights of the data subject (be informed, object, access, correct, erase, damages) | Erasure procedure in §5; correction through *Edit Student* | No self-service access or export for a student; no consent-withdrawal screen |
| §20 — security of personal information (organisational, physical, technical) | §6 | No encryption at rest, no transport encryption (§7.1, §7.5) |
| §20(f) — breach notification | — | No procedure exists (§7.7) |
| §21 — accountability of the personal information controller | The institution is the controller | No DPO designated |

---

## 9. Operator checklist

Before enrolling any student:

- [ ] Signed consent on file (parent or guardian if under 18)
- [ ] The student has been told they may refuse, and how to withdraw later
- [ ] Full-disk encryption is enabled on the host machine
- [ ] The default `admin`/`admin` credential has been changed
- [ ] `git check-ignore dataset trainer` returns both paths

When a student leaves, or withdraws consent:

- [ ] Student deleted through *Manage Students*
- [ ] Model retrained
- [ ] `.bak` model pair deleted or superseded
- [ ] Confirmed the student no longer appears in `trainer/labels.txt`
