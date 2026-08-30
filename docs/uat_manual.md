# User Acceptance Testing Manual

**System:** AI-Based Facial Recognition Attendance System
**Version:** branch `phase-5-web-and-ux`, schema migration 007
**Revised:** 2026-08-16 · **Phase:** 1 of n — initial acceptance
**Audience:** the tester. No programming knowledge assumed.

---

## 0. Before you start

### 0.1 ⚠️ Consent — legal, not a formality

This system stores photographs of identifiable students and the biometric
templates derived from them. Under **RA 10173** that is sensitive personal
information, and nothing in the software records consent yet — so it is paper,
and it has to exist **before** the capture.

The forms are in [`consent_form.md`](consent_form.md): **Form A** for a
student you enrol, **Form B** for anyone you point the camera at to test a
rejection. They are not interchangeable — Form B states the real purpose, which
is the point of having two.

Get a signed form from:

- every student you enrol, **and**
- anyone you point the camera at to test a rejection. Their form must state the
  real purpose: *"to test whether the system wrongly recognises you."*

No consent, no capture. Note the reduced coverage on the sign-off sheet.

### 0.2 Start the database

Open the **XAMPP Control Panel** and start **MySQL**. It does not start itself
after a restart, so if every page returns a server error, check this first.

### 0.3 The two cameras

- **Enrolment** uses the camera on the computer you are browsing from.
- **Recognition** uses the camera attached to the server.

For this phase they are the same machine. Enrolment needs `http://localhost`
unless you configure a certificate (§7).

### 0.4 Start the system

```
cd <project folder>
.venv\Scripts\activate
python app.py
```

Open **`http://localhost:5000`** and sign in fresh — sign out first if a
browser session is already open.

The app updates the database itself on start. If it **refuses to start**, it
prints one line naming the fix — copy that line verbatim into a defect report.

---

## 1. Scope

**In:** the full workflow end to end — setup and access (A), reference data
(B), enrolment (C), training (D), a live session (E), reports and export (F),
messages and layout (X), corrections (G), roles (H), error handling (I).

**Out:**

- **Accuracy as a result.** Four enrolled people, images from one sitting, no
  impostor set. **Do not record accuracy percentages from this phase.**
- **Anti-spoofing.** Liveness is beatable by a video replay and known to be.
- **Multi-user, network, and load.** One operator, one machine.

---

## 2. Environment

Record on the sign-off sheet.

| Item | Expected | Yours |
|---|---|---|
| Machine / OS | Windows 10 or 11 | |
| Python | 3.11 (not 3.12+) | |
| Database | MariaDB via XAMPP, running | |
| Browser | Chrome or Edge, current | |
| URL | `http://localhost:5000` | |
| Camera | Webcam, working | |
| Lighting | Normal indoor | |
| Tester / Date | | |

---

## 3. How to run a case

1. Meet the **Precondition** first. A failure caused by a missing precondition
   is not a defect.
2. Follow the **Steps**.
3. Compare against **Expected**. Mark Pass / Fail / Blocked.
4. On a failure: screenshot it, note the time, and fill in §8 while it is in
   front of you — including the exact wording of any message.

---

## 4. Test cases

### A. Setup, login, access control

| ID | Test | Precondition | Steps | Expected |
|---|---|---|---|---|
| **A1** | App starts | Database running | `python app.py` | Starts, prints the address. No error. |
| **A2** | Login page | A1 | Open `http://localhost:5000` | Login form appears. |
| **A3** | Forced password change | Fresh install, `admin`/`admin` | Sign in as `admin`/`admin`, then try to reach `/dashboard` by URL | Sent straight to the change-password screen, and back to it if you try to leave. |
| **A4** | Password change works | A3 | Set a new password, sign out, sign in with it | Signs in. Old password rejected. |
| **A5** | Lockout | A4 | Enter a wrong password 5 times | Refused each time. After 5, locked for 15 minutes with a message saying so. |
| **A6** | Case sensitivity | A4 | Sign in with the right password in the wrong case | Refused. |
| **A7** | Signed-out access | — | Sign out, then open `/reports` by URL | Sent to the login page. |
| **A8** | Account screens use your account | A4 | Settings → change the admin username → sign out → sign in with the new one | Signs in under the new username. |

### B. Reference data

| ID | Test | Precondition | Steps | Expected |
|---|---|---|---|---|
| **B1** | Add a subject | Admin | Subjects → fill in code, name, instructor, day, course, section, Time In, Time Out → Add | Appears in the list with the times shown. |
| **B2** | Time In is a real time | B1 | Edit the subject, check Time In | A time picker holding e.g. `08:00`. **Set it deliberately — this is what Late is judged against.** |
| **B3** | Edit a subject | B1 | Subjects → Edit → change the name → Update | Saved and shown. |
| **B4** | ⚠️ **Populate the class list** | B1 + one enrolled student (C3) | Subjects → Class List → add the student | Student appears on that subject's class list. |
| **B5** | Delete a subject | A spare subject exists | Subjects → Delete | Removed, with its attendance records. |

> **B4 gates every attendance test.** Attendance is recorded only for students
> on the subject's class list. With an empty class list a session runs,
> recognises nobody, and records nothing — which looks like broken recognition.

### C. Enrolment (browser capture)

> Consent first (§0.1).

100 images across nine stages: look straight (25), left (15), right (15), up
(10), down (10), smile (10), closer (5), medium (5), farther (5). About one
image every 0.8 s, and each stage must be held for several frames — a couple of
minutes total. **That is expected, not a hang.**

| ID | Test | Precondition | Steps | Expected |
|---|---|---|---|---|
| **C1** | Capture page opens | Admin, consent obtained | Students → fill in ID, name, department, program, year, section → submit | Capture page opens, browser asks for camera permission. Allow it. |
| **C2** | Permission refused | C1 | Reload and **deny** camera permission | A message explains what to do. Not a blank error page. |
| **C3** | Full capture | C1 allowed | Work through all nine stages | Progress advances, instruction changes per stage, finishes at 100/100. Student appears in the list. |
| **C4** | Guidance is usable | C3 | While capturing, move off-centre, too close, too far | It says **which** thing is wrong, not just that it failed. |
| **C5** | Cancel | C1 | Start, complete a few stages, Cancel | Student does **not** appear. Nothing partial kept. |
| **C6** | Close the tab | C1 | Start, then close the tab | No student created. Can start again (allow up to 5 min for the slot to release). |
| **C7** | Two at once | C1 running | Start another capture in a second tab | Refused, with a message. |
| **C8** | Bad input | Admin | Add a student with an ID like `12/34` | Refused clearly. Nothing created. |
| **C9** | Recapture | C3 done | Manage Students → recapture that student | Runs. **If it fails or is cancelled, the existing images survive.** |

### D. Training

| ID | Test | Precondition | Steps | Expected |
|---|---|---|---|---|
| **D1** | Training starts | C3 | Watch after a capture completes | Says the model is being rebuilt. Page does not freeze. |
| **D2** | Progress is visible | D1 | Watch the status | Shows the stage and elapsed time, ends in a clear success. |
| **D3** | Two at once | D1 running | Trigger training again | Refused. First run unaffected. |

### E. Live attendance session

> **Precondition for the whole section: B4 is done.**

| ID | Test | Precondition | Steps | Expected |
|---|---|---|---|---|
| **E1** | Session starts | Model trained, class list populated | Attendance → choose subject → Start | Camera view appears. No error. |
| **E2** | Subject is chosen | E1 | Look at the subject control | A dropdown of real subjects, not a text box. |
| **E3** | Face detected | E1 | Stand in front of the camera | A box is drawn around your face. |
| **E4** | ⚠️ **Recognised and recorded** | E1, you are enrolled **and on the class list** | Stand an arm's length to a metre away, face the camera, hold still | Your name appears; status moves verifying → liveness → recorded. You are marked present. |
| **E4b** | ⚠️ **Live list updates** | E4 | Look at *Recognised so far* **without ending the session** | Your name and ID appear within a few seconds and the counter goes up. |
| **E5** | Liveness | E4 in progress | Follow the prompts | Two prompts in random order, ~8 s each. Completing them confirms identity. |
| **E6** | ⚠️ **Never stuck silently** | E1 | Stand further back, or in dimmer light | If it cannot confirm you it **says why** ("move closer / more light"). Never a percentage forever with no explanation. |
| **E7** | Unenrolled person refused | Someone not enrolled, with consent | Have them face the camera | Not recorded. Shown as unknown or not enrolled. |
| **E8** | Not on the class list | A student enrolled but not added in B4 | Have them face the camera | Not recorded for this subject. |
| **E9** | No double-recording | E4 | Stay in front of the camera after being marked | Recorded once. |
| **E10** | ⚠️ **Late is recorded** | A subject whose Time In is **earlier** than now (B2) | Run a session, be recognised, then check **Reports** | Status is **Late**, not Present. |
| **E10b** | Screen agrees with the register | E10 | Look at the label above your face during the session | Says **Late**, in amber — the same word Reports shows. |
| **E11** | Ending records absences | E1, class list has students who did not appear | End the session, same subject | Summary shows present and absent counts. Absent students are saved, and only students on that class list. |
| **E11b** | ⚠️ **Wrong subject refused** | E1, two subjects exist | With a session running, choose a **different** subject in the End dropdown → End | Refused, naming the subject actually running. Nothing recorded, session still running. |
| **E11c** | Absence has no arrival time | E11 | Reports → find a student marked Absent | Time In is **blank**, not a time. |
| **E12** | Ending twice | E11 | End again for the same subject | No error. A present student is not turned into an absentee. |

### F. Reports and export

| ID | Test | Precondition | Steps | Expected |
|---|---|---|---|---|
| **F1** | Records appear | E11 | Reports | Records listed with student, subject, time, status. |
| **F2** | Totals are right | F1 | Compare the Present/Absent/Total boxes against the rows | They match. Absent is not stuck at 0. |
| **F3** | Filters work | F1 | Filter by date, then by subject | Only matching records shown. |
| **F4** | Export honours filters | F3 | With a filter applied → Export Excel | File contains only the filtered records, named with a timestamp. |
| **F4b** | Export opens cleanly | F4 | Open the file | No repair prompt. Times read as `08:31:00`. An Absent row's Time In is empty (see E11c). |
| **F4c** | Nothing kept on the server | F4 | Look in the project's `reports/` folder | No new file appears. Any files already there can be ignored. |
| **F5** | Dashboard counters | Some data exists | Dashboard | Real numbers, not zeros. |

### X. Messages, confirmations, layout

| ID | Test | Precondition | Steps | Expected |
|---|---|---|---|---|
| **X1** | Actions confirm themselves | Admin | Delete a spare subject, or edit a student and save | A message at the top of the page says what happened. |
| **X2** | Says what was *not* done | Admin, a student on a class list | Subjects → Class List → remove that student | The message says their existing attendance records were kept. |
| **X3** | ⚠️ **Apostrophe names still confirm** | A student whose name contains `'` (e.g. `O'Brien`) | Manage Students → Delete | A confirmation dialogue appears **naming that student**. |
| **X4** | Cancel does nothing | X3 | Delete, then Cancel | Student is still there. |
| **X5** | Back button | Admin | Submit a form, press Back | The submit button is enabled again, not stuck greyed out. |
| **X6** | Keyboard navigation | Any page | Tab from the top | First stop is a "Skip to main content" link; every focused control has a visible outline. |
| **X7** | Readable without colour | F1 | Look at the Status column | Each row says the word Present / Late / Absent. |
| **X8** | Narrow window | Any page | Make the window narrow | Sidebar collapses to a Menu button. No sideways scrolling. |

### G. Correcting a record

| ID | Test | Precondition | Steps | Expected |
|---|---|---|---|---|
| **G1** | Correction is reachable | F1 | Reports → Correct on a row | Screen opens showing student, subject, date, time, current status. |
| **G2** | A correction applies | G1 | Absent → Present, type a reason, apply | Status changes. Reports shows Present. |
| **G3** | ⚠️ **It is recorded** | G2 | Look at Correction history | One entry: when, who, from what, to what, and your reason. |
| **G4** | Reason required | G1 | Apply with the reason box empty | Refused. Nothing changes. |
| **G5** | No-op refused | G1 | Set the status it already has | Told so. No history entry added. |
| **G6** | History accumulates | G2 | Correct the same record again | Both corrections appear, in order. |
| **G7** | Instructor can correct | H1 | Sign in as the instructor → Reports → Correct | Allowed. History records the instructor's username. |
| **G8** | Time In not rewritten | G2 | After Absent → Present, check Time In | **Still blank.** No arrival time is invented; the history is the record of the change. |

### H. Roles

| ID | Test | Precondition | Steps | Expected |
|---|---|---|---|---|
| **H1** | Create an instructor | Admin | Instructors → add one | Created. The list shows a password **status**, never a password. |
| **H2** | Instructor's view | H1 | Sign in as the instructor | Dashboard, Attendance, Reports and corrections available. |
| **H3** | Admin pages blocked | H2 | As the instructor, open `/students` by URL. Repeat for `/subjects`, `/instructors`, `/settings` | Refused each time. |

### I. When things go wrong

| ID | Test | Precondition | Steps | Expected |
|---|---|---|---|---|
| **I1** | Missing page | Signed in | Open `/nonsense` | A proper error page with navigation. |
| **I2** | No technical detail | I1, or any failure | Read the message | Plain language. No database errors, file paths, or code. |
| **I3** | Database stopped mid-use | Signed in | Stop MySQL in XAMPP, click around, **restart it** | Error pages, not crashes. Works again after restarting MySQL, without restarting the app. |
| **I4** | Camera in use | — | Open the Windows Camera app, then start a session | A message explaining the camera is unavailable. |

---

## 5. Priority

If time is short, in this order:

1. **B4** — populate a class list. Nothing works without it.
2. **C3** — a full nine-stage capture.
3. **E4** — a student is actually marked present.
4. **E4b** — the live list updates during the session.
5. **E10** — Late is recorded.
6. **E6** — it never gets stuck silently.
7. **E11** — absences are recorded, and only for the class list.
8. **G3** — a correction is recorded with who and why.
9. **A3 / A4** — the seeded password cannot be left in place.

---

## 6. Sign-off

| Section | Cases | Pass | Fail | Blocked | Tester | Date |
|---|---|---|---|---|---|---|
| A — Setup and access | 8 | | | | | |
| B — Reference data | 5 | | | | | |
| C — Enrolment | 9 | | | | | |
| D — Training | 3 | | | | | |
| E — Attendance session | 16 | | | | | |
| F — Reports and export | 7 | | | | | |
| X — Messages and layout | 8 | | | | | |
| G — Corrections | 8 | | | | | |
| H — Roles | 3 | | | | | |
| I — Error handling | 4 | | | | | |
| **Total** | **71** | | | | | |

**Overall:** ☐ Accepted ☐ Accepted with defects ☐ Rejected

**Tester:** ________________________  **Date:** ____________

**Witnessed by:** ___________________  **Date:** ____________

**Consent forms collected:** ☐ Yes, for every person captured — count: ______

---

## 7. Known limitations — do not raise these as defects

| # | Behaviour | Why |
|---|---|---|
| 1 | Liveness can be beaten by a video replay. | Head poses only — no depth, texture or blink check. It raises the effort to spoof the system; it does not prevent it. |
| 2 | Enrolment needs `localhost` unless a certificate is configured. | Browsers expose the camera on secure origins only. HTTPS is implemented — run `python scripts/make_dev_cert.py` and set the two paths in `.env`. |
| 3 | Only 4 people are enrolled. | Sample size. Any accuracy you observe is not a result. |
| 4 | Accuracy is not a measured result yet. | No impostor set; training images from one sitting. See `docs/limitations.md`. |
| 5 | No password reset. | Planned. An admin can change an instructor's password from the Instructors screen. |
| 6 | The model holds about 30 students before it becomes unreadable. | A measured property of the recognition method. |
| 7 | It is the Flask development server. | Fine for this phase; it is not a deployment. |

> **If a test case in §4 and a limitation here disagree, the case wins — run
> the case and report what you see.**

---

## 8. Defect report

One per defect. Copy this block.

```
DEFECT ID:        UAT-001
TEST CASE:        (e.g. E4)
DATE / TIME:      (needed to match the server log)
TESTER:

SEVERITY:         ☐ Critical - cannot continue testing
                  ☐ Major    - a main function is wrong
                  ☐ Minor    - works, but wrongly or awkwardly
                  ☐ Cosmetic - wording, layout, spelling

WHAT I DID:
  1.
  2.
  3.

WHAT I EXPECTED:

WHAT ACTUALLY HAPPENED:
  (exact wording of any message, word for word)

SCREENSHOT:       ☐ attached

CHECKED FIRST:    ☐ MySQL is running in XAMPP    (§0.2)
                  ☐ The class list is populated  (B4)
                  ☐ Not in Known Limitations     (§7)
```

> Those three account for most "it's broken" reports on this system.

---

## 9. After the session

1. **Collect the consent forms** and store them with the test records.
2. **Save the server log** from `logs/` alongside the completed manual.
3. **Note anything slow, confusing or awkward**, even where a case passed. A
   case can pass and the feature still be unusable.
4. **Do not delete student data to tidy up.** Erasure is a three-step process;
   doing it by hand leaves biometric templates behind.
