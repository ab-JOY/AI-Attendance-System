# Consent forms

**Purpose:** the paper consent that `docs/data_privacy.md` §2 requires and that
the software does not yet collect.
**Contains:** **Form A** — enrolment · **Form B** — evaluation participant
("impostor") · **Form C** — withdrawal and erasure request.
**Companion documents:** [`data_privacy.md`](data_privacy.md) (what is
collected, kept and deleted) · [`uat_manual.md`](uat_manual.md) §0.1 (when a
form is required during testing).

---

## Operator notes — read once, do not hand these pages to the student

> ⚠️ **These notes are not part of the forms.** Print only the numbered form
> that applies, starting from its own heading.

**Why paper.** `students` has no consent column and the enrolment flow has no
consent screen (`data_privacy.md` §2, §8 — RA 10173 §13 is listed there as
"not enforced in software"). Until that changes, a signed form **collected
before any capture is started** is the entire consent record. The Face Capture
page at `/enrol` is the point of no return: once it runs, 100 images exist.

**Filling in the blanks before you print.** Five things are placeholders
because this repository cannot know them. Replace every one throughout, and do
not leave a form with `[…]` still in it:

| Placeholder | What it is |
|---|---|
| `[INSTITUTION]` | The school or college. It is the personal information controller under RA 10173 §21. |
| `[DEPARTMENT / PROGRAMME]` | The unit running the system |
| `[DPO NAME AND CONTACT]` | The Data Protection Officer. ⚠️ `data_privacy.md` §8 records that **none is designated** — designate one, or name the responsible staff member and their contact, before collecting any form. |
| `[SYSTEM CUSTODIAN]` | Who physically holds the machine the images sit on |
| `[RETENTION END]` | See §4 of `data_privacy.md`: the end of the student's enrolment in the institution. State it as a concrete event, not a duration. |

**Filing.** Keep the signed form for as long as the data it covers exists, and
destroy it when the data is destroyed. A consent form is itself personal
information — store it as securely as you store the images.

**Under 18.** Section 5 of Form A must be completed by a parent or guardian.
Do not capture a minor's face on the student's signature alone.

**This is a template, not legal advice.** It is written from what this system
actually does, so it should be accurate about the processing — but it has not
been reviewed by a lawyer or a DPO. Have `[INSTITUTION]`'s data protection
officer review it before it is used with real students.

**On the faces currently enrolled.** The dataset in this repository is the
project team's own faces. Each team member should still complete Form A, and
if the capture already happened, record the actual capture date rather than
back-dating the signature — an honest late record is worth more than a tidy
false one.

---
---

# Form A — Consent to Face Capture for Attendance

**[INSTITUTION] · [DEPARTMENT / PROGRAMME]**
**Automated Class Attendance by Face Recognition**

Please read this before signing. Ask about anything that is unclear — you are
entitled to an answer before you decide.

---

### 1. Who is asking, and who is responsible

**[INSTITUTION]** is responsible for this data (the "personal information
controller" under the Data Privacy Act of 2012, Republic Act No. 10173).

Questions, requests and complaints go to the Data Protection Officer:

**[DPO NAME AND CONTACT]**

---

### 2. What will be collected

If you agree, the system will record:

| What | Detail |
|---|---|
| **Photographs of your face** | About **100 still images**, taken in one sitting of roughly one minute. You will be asked to look straight ahead, turn slightly left and right, look slightly up and down, smile, and move closer and further away. |
| **A numeric face template** | A set of numbers calculated from those photographs. It is what the system actually compares against when it recognises you. |
| **Your student details** | Student ID, name, department, programme, year level and section. |
| **Your attendance records** | For each class: the date, the time you were recognised, and whether that counted as Present, Late or Absent. |

**Your face photographs and the template are "sensitive personal information"**
under RA 10173. That is why your written consent is required, and why you are
being asked rather than told.

---

### 3. What it will be used for

**One purpose only: recording your attendance in class.**

It will **not** be used to identify you outside class, to track your movements,
to monitor your behaviour or expressions, or for any research, publication or
sharing with anyone outside [INSTITUTION] — unless you are asked again, and
separately, and agree to that new purpose.

---

### 4. What you should know before deciding

Stated plainly, because consent given without these facts is not informed
consent.

- **Where it is stored.** On a computer held by **[SYSTEM CUSTODIAN]** at
  [INSTITUTION]. The images and the template are stored as ordinary files. They
  are **not encrypted**, so anyone with access to that machine, or to an
  unencrypted backup of it, can open them.
- **Who can see what.** Instructors and administrators of the system can see
  your name and your attendance records. Your photographs are not viewable
  through the system by anyone — there is no screen that displays them — but
  they are files on the machine, so anyone with access to the machine itself
  can open them.
- **The system can be wrong.** It may fail to recognise you when you are
  present, or occasionally recognise the wrong person. **Any attendance record
  can be corrected.** Tell your instructor; a correction is recorded together
  with the reason for it and who made it.
- **It is not a security system.** It has a basic check that asks you to turn
  your head, to make it harder to fool with a photograph. That check can be
  defeated by someone playing a video. It is not proof that you were physically
  present.

---

### 5. You can say no

**You may refuse, and nothing bad will happen to you.**

If you refuse, your attendance will be taken **manually**, the way it is taken
now. You will not be marked absent, penalised, treated differently, or asked to
explain yourself. Refusing is a normal choice and is expected to be available
to everyone.

You can also **change your mind later**, at any time, for any reason — see
section 7.

---

### 6. How long it is kept

| What | How long |
|---|---|
| Face photographs and the face template | Until **[RETENTION END]**, and no longer |
| Attendance records | For [INSTITUTION]'s normal academic records retention period |

Your face photographs are kept for the shortest time of anything here: once you
are no longer attending classes, there is no reason to keep your face, and it
is deleted. **Your attendance history does not depend on the photographs** —
deleting them does not delete your record of having attended.

---

### 7. Your rights, and how to withdraw

Under RA 10173 §16 you have the right to:

- **be informed** that your data is being collected and processed;
- **object** to the processing, and to withdraw consent you have already given;
- **access** your data and be told how it has been used;
- **correct** anything inaccurate;
- **have your data erased or blocked**;
- **be indemnified** for damage caused by inaccurate, unlawfully obtained or
  unauthorised use of your data;
- **complain to the National Privacy Commission** — you do not need
  [INSTITUTION]'s permission to do this, and you can do it at any time.

**To withdraw consent**, complete **Form C** and give it to
[DPO NAME AND CONTACT], or ask the system administrator for a copy. You do not
have to give a reason.

**What happens when you withdraw:** your photographs are deleted, the system is
retrained so your face template is removed from it, and the backup copy of the
previous template is removed as well. Your past attendance records are kept —
they are academic records, not biometric data. You go back to having your
attendance taken manually, with no penalty.

⚠️ **Erasure is done by hand and takes more than one step.** Ask for written
confirmation that it has been completed, and keep it.

---

### 8. Consent

> Sign only if all of the boxes below are true for you. If any of them is not,
> do not sign — say so instead.

- [ ] I have read this form, or had it read and explained to me, in a language
      I understand.
- [ ] I have had the chance to ask questions, and my questions were answered.
- [ ] I understand that my face photographs and face template are **sensitive
      personal information**.
- [ ] I understand this will be used **only** to record my class attendance.
- [ ] I understand I could have refused with **no penalty**, and that I may
      withdraw at any time.
- [ ] I understand the images are stored **unencrypted** on a computer at
      [INSTITUTION].
- [ ] I have been given a copy of this form.

**I give my consent to the capture of my face images and the creation of a face
template, for recording my class attendance.**

| | |
|---|---|
| Full name | ____________________________________________ |
| Student ID | ____________________________________________ |
| Programme / Year / Section | ____________________________________________ |
| Signature | ____________________________________________ |
| Date | ______________________ |

---

### 9. Parent or guardian — required if the student is under 18

| | |
|---|---|
| Name of parent or guardian | ____________________________________________ |
| Relationship to the student | ____________________________________________ |
| Signature | ____________________________________________ |
| Date | ______________________ |

---

### 10. For the operator — complete at the time of capture

| | |
|---|---|
| Consent received and checked by | ____________________________________________ |
| Date and time of capture | ______________________ |
| Number of images captured | ______________________ |
| Student folder created | `dataset/` ______________________ |

⚠️ **Do not start the capture before this section can be completed** — that is
the whole point of collecting consent first.

---
---

# Form B — Consent for System Testing (Not Enrolled)

**[INSTITUTION] · [DEPARTMENT / PROGRAMME]**
**Testing an Attendance System's Accuracy**

> ⚠️ **Operator: this is not Form A with a different title, and it must not be
> replaced by one.** The purpose here is genuinely different, and
> `data_privacy.md` §2 requires it to be stated in its own words. Use this form
> for anyone whose face is shown to the camera to test the system, including
> during UAT (`uat_manual.md` §0.1).

---

### 1. What we are asking you to do

We are testing whether an automatic attendance system makes mistakes.

**You are not being enrolled in it.** You are not a student it will ever
recognise, and no attendance will ever be recorded for you.

We want to point the camera at you specifically **to check whether the system
wrongly identifies you as somebody else** — as one of the students who *is*
enrolled. If it does, that is a fault we need to find and measure. You are
helping us find it.

---

### 2. What will be collected

| What | Detail |
|---|---|
| **Images of your face** | Captured while the camera is pointed at you |
| **The system's response** | Whether it claimed to recognise you, who as, and how confident it was |

**No attendance record will be created for you.** Your name is not added to the
student list, and no face template is built from your images.

Your face images are **sensitive personal information** under RA 10173 even
though you are not being enrolled, and even though we hope the system fails to
recognise you. That is why you are being asked.

---

### 3. How long it is kept, and when it is destroyed

Your images are kept **only until this evaluation is finished**, and are then
deleted.

They have no ongoing purpose whatsoever — unlike an enrolled student's images,
they are not needed to run anything. This is the shortest retention in the
whole system.

| | |
|---|---|
| Evaluation expected to finish by | ______________________ |
| Images to be deleted by | ______________________ |
| Deletion confirmed by / on | ____________________________________________ |

---

### 4. Your rights

The same rights as anyone else under RA 10173 §16: to be informed, to object,
to access, to correct, to have your data erased or blocked, to be indemnified,
and to complain to the **National Privacy Commission** at any time without
asking us first.

**You may refuse, and you may stop at any point during the session** — including
after it has started, without explaining why. Nothing depends on your taking
part.

Contact for any request or complaint: **[DPO NAME AND CONTACT]**

---

### 5. Consent

- [ ] I understand I am **not** being enrolled and no attendance will be
      recorded for me.
- [ ] I understand the purpose is **to test whether the system wrongly
      recognises me** as an enrolled student.
- [ ] I understand my face images will be kept only until the evaluation ends,
      and then deleted.
- [ ] I understand I may refuse or stop at any time, with no consequence.
- [ ] I have had the chance to ask questions, and my questions were answered.

**I give my consent to my face being captured and shown to the system for this
test.**

| | |
|---|---|
| Full name | ____________________________________________ |
| Contact (for the deletion confirmation) | ____________________________________________ |
| Signature | ____________________________________________ |
| Date | ______________________ |

**If under 18 — parent or guardian**

| | |
|---|---|
| Name | ____________________________________________ |
| Signature | ____________________________________________ |
| Date | ______________________ |

---
---

# Form C — Withdrawal of Consent and Request for Erasure

**[INSTITUTION] · [DEPARTMENT / PROGRAMME]**

> You do not have to give a reason for this request, and you will not be asked
> for one. Withdrawing consent is a right under RA 10173 §16, not a favour.

---

### 1. Who is withdrawing

| | |
|---|---|
| Full name | ____________________________________________ |
| Student ID (if enrolled) | ____________________________________________ |
| Date of original consent (if known) | ______________________ |

---

### 2. What is being withdrawn

- [ ] **I withdraw my consent** to the use of my face for attendance, and ask
      that my face images and face template be deleted.
- [ ] I was a **test participant** (Form B) and ask that my images be deleted.

**I understand that:**

- My **attendance records will be kept**. They are academic records, not
  biometric data, and deleting my face does not delete my history of having
  attended class.
- My attendance will be taken **manually** from now on, with **no penalty**.
- This takes effect from the date this form is received.

| | |
|---|---|
| Signature | ____________________________________________ |
| Date | ______________________ |

---

### 3. For the operator — the erasure is not one step

> ⚠️ **A student deleted through the application disappears from every screen
> immediately, while their face template is still in service.** Tick every box
> below, in order, and only then sign. The full procedure and the reasoning are
> in [`data_privacy.md`](data_privacy.md) §5.

- [ ] **1. Student deleted** through *Manage Students → Delete*. This removes
      the database row, their attendance rows and `dataset/{student_id}/`.
- [ ] **2. Model retrained** — *Students → Train Model*, or
      `python train_model.py`. Until this runs, `trainer/trainer.yml` still
      contains the histograms computed from their face.
- [ ] **3. Backup generation removed.** Training keeps the previous model as
      `trainer/trainer.yml.bak` and `trainer/labels.txt.bak`, and that backup
      still contains them. Delete the `.bak` pair, **or** retrain a second time
      so the backup is itself post-erasure.
- [ ] **4. `dataset/_migrated_duplicates/` checked** for a leftover
      `{student_id}_{name}` folder. Deletion through the application knows
      nothing about this directory, so a copy can survive an otherwise
      complete erasure.
- [ ] **5. Verified** — the student no longer appears in `trainer/labels.txt`,
      and no directory under `dataset/` carries their ID.
      `python scripts/preflight.py` reports a trained identity with no student
      row, which is exactly the shape of a deletion whose images were left
      behind.
- [ ] **6. This form filed**, and the original consent form destroyed.

> **Note where the machine retrains on every release:** steps 2 and 3 then
> happen on their own — each release rebuilds the model from `dataset/`, and
> the release after that rotates the pre-erasure backup out. **Step 1 still has
> to be done by hand**, because that is what removes the images; if they are
> left on disk, every release faithfully puts the student back into the model.
> Do not rely on this unless you have confirmed two releases have happened.

| | |
|---|---|
| Erasure completed by | ____________________________________________ |
| Date completed | ______________________ |
| Written confirmation sent to the data subject on | ______________________ |
