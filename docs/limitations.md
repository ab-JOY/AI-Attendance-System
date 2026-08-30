# Limitations and Hard Constraints

**System:** AI-Based Facial Recognition Attendance System
**Compiled:** 2026-08-08, after Phase 2 (Security)
**Audience:** the technical writer preparing the manuscript, and anyone
answering questions at the defense.

---

## 0. How to use this document

Every claim below is either **measured** or **stated as unmeasured**. Nothing
is estimated silently. Where a figure appears, the command that produced it is
named in §8 so it can be re-run and cited.

Three things to know before drafting anything:

1. **This system's headline accuracy figure does not mean what it looks like.**
   It is 100%, and it is not a generalisation result. §3 explains exactly what
   it is and what may honestly be said about it. If you write only one section
   carefully, write that one.
2. **A previous version of the documentation contained six claims that could
   not be reproduced**, and two of them were the opposite of the truth. They
   are listed in §7 so they are not recycled. If you find an accuracy or
   performance number in an older draft that does not appear in this document,
   assume it is wrong until it is re-measured.
3. **"Limitation" and "defect" are different, and the difference matters in a
   defense.** A defect is something we would fix given time. A limitation is a
   consequence of a decision that was taken deliberately, with the alternative
   considered. §1 and §2 are limitations; §6 lists what is still outstanding.
   Presenting a considered limitation as an oversight is a weaker position than
   the truth.

The bracketed IDs (`SE-3`, `PE-2`, `FS-4`) are audit references from
`tasks/todo.md`. They exist for traceability; they do not need to appear in the
manuscript.

---

## 1. Hard constraints

These cannot be changed without substantial rework. They are not preferences.

### 1.1 Python 3.11 exactly — not 3.12 or later

`mediapipe==0.10.14` publishes no wheels for Python 3.12+. On a newer
interpreter `pip install` **succeeds** and `import mediapipe` then fails, which
is a worse failure than refusing to install. `pyproject.toml` therefore pins
`requires-python = ">=3.11,<3.12"`, and that upper bound is load-bearing.

### 1.2 `opencv-contrib-python`, not `opencv-python`

The LBPH recogniser lives in `cv2.face`, which ships only in the contrib
package. Installing plain `opencv-python` produces an application that imports
cleanly and cannot recognise anyone. Both `train_model.py` and
`recognize_face.py` guard on `hasattr(cv2, "face")` for this reason.

### 1.3 The LBPH parameters are fixed in code and are not configurable

`LBPH_PARAMS` (`radius=2, neighbors=8, grid 8×8`) is a code constant, on
purpose. It is the one setting in the system deliberately withheld from the
configuration layer.

Raising `neighbors` from 8 to 12 once broke the system in two independent ways
at the same time: the model became too large for OpenCV to read back, **and**
it shifted the LBPH distance scale from ~35 into the 81–107 band while the
acceptance threshold stayed at 58.0 — so every face in the system was silently
classified as unknown, with no error raised anywhere. Held-out accuracy was
0/60 while the application appeared to run normally.

The lesson generalises and is worth a sentence in the manuscript: **a parameter
that changes the magnitude of a score silently invalidates every threshold
tuned to the old scale.** Making this value editable in a `.env` file would let
a single configuration change reintroduce the outage.

### 1.4 The recognition threshold is 58.0 and is not tuned

58.0 is the value at which the system was measured to work. It is **not**
derived from any error-rate analysis. A unit test asserts it so it cannot drift
on a hunch. The only legitimate reason to change it is a threshold re-derived
from a DET curve on a proper open-set evaluation — planned, see §5.

### 1.5 Dataset folders are named `{student_id}` ✅ *resolved, Phase 5*

**This section described a limitation that no longer exists.** It is kept
because the original wording is quoted in earlier handovers, and because the
constraint it left behind is still real.

Folders were `dataset/{student_id}_{student_name}` and the model's label file
mapped each label to that folder name, which made a student's *display name*
part of the model artefact. Renaming a student in the database without renaming
their folder broke deletion, editing and recapture for them; the system failed
loudly rather than acting on the wrong folder, but it still failed.

The folder is now the student ID alone, `labels.txt` is `{label},{student_id}`,
and the display name is read from the `students` table at model load. Renaming
a student touches one row. Existing installations migrate with
`python scripts/rename_dataset_folders.py --apply` followed by a retrain; the
migration was applied to this deployment on 2026-08-16 and held-out accuracy
was re-measured at **60/60, average distance 34.95** — unchanged.

**One rule survives it: student IDs may not contain underscores.** The reason
changed rather than disappeared. It used to be that both readers split on the
first underscore; now it is that a machine which has not run the rename still
holds old-format folders, against which an ID containing `_` is ambiguous. Real
IDs look like `23-1-1-0559`.

### 1.6 Browser-based face capture requires HTTPS

Browsers expose the camera API (`getUserMedia`) only in a **secure context**:
an HTTPS origin, or `localhost`. There is no flag, no permission prompt and no
graceful degradation — on a plain-HTTP page served to another machine the call
is simply rejected.

This is a hard constraint rather than a recommendation, and it is the reason
TLS is a prerequisite for the planned move of enrolment into the browser rather
than a later hardening step.

### 1.7 MySQL-specific SQL

Queries use MySQL functions such as `CURDATE()`. The system is not portable to
another database engine without rewriting them.

---

## 2. Architectural and deployment limitations

### 2.1 Enrolment currently requires the operator to be at the server

Enrolment opens an OpenCV window **on the server machine**. If the browser is
on a different computer, the operator sees nothing and the request waits until
somebody at the server presses a key.

**Status: being replaced.** Enrolment is moving into the browser
(`getUserMedia` + upload). Do not document the current design as final; see §5.

### 2.2 Attendance uses the server's camera, by design

This is *not* the same limitation as 2.1 and should not be written as one. The
classroom camera is physically attached to the attendance station, and the live
video is streamed to whatever browser is watching. That is the intended
architecture for a fixed classroom installation, and it is not changing.

### 2.3 The web server is Flask's development server

`app.run()` binds the loopback interface. There is no production WSGI server
and no reverse proxy. The development server is single-process and explicitly
documented by Flask as unsuitable for production.

This has a knock-on effect worth stating precisely: **the login lockout counter
is held in memory**, so it is reset by a restart and would not be shared across
multiple worker processes. That is an accurate model of a single-process
deployment, and it stops being accurate the moment a real WSGI server with
multiple workers is introduced.

### 2.4 One recognition session at a time is not enforced

Recognition state is held in module-level variables shared between the camera
thread and the web server's request threads, without locking. Two simultaneous
viewers of the live feed share and corrupt one state machine, and whichever
stops first releases the camera for both.

In practice one operator runs one session, so this has not been hit. It is a
known defect awaiting the recognition-engine work, not a property of the
design.

### 2.5 Windows-only camera handling (today)

The camera backend uses a DirectShow-specific OpenCV flag and a Windows API
call for window placement. The system does not currently run on Linux or macOS.
The planned move of enrolment into the browser removes both, since the code
containing them is being deleted.

### 2.6 Development uses a self-signed certificate

**Decision, 2026-08-08:** TLS in deployment uses a proper certificate;
development uses a self-signed one.

Three practical points that belong in a setup appendix rather than being
rediscovered:

- **A certificate with no Subject Alternative Name is rejected outright by
  current browsers**, regardless of any exception the user tries to accept. If
  the system is reached by IP address on a local network, the SAN must include
  that IP as an IP entry — a common-name-only certificate will not work.
- **Once the browser exception is accepted, a self-signed certificate does
  provide a secure context**, so camera access works. The cost is an
  interstitial warning screen on first visit.
- **A locally-trusted development CA (for example `mkcert`) avoids the warning
  entirely** and generates correct SANs. This is worth the setup time for one
  specific reason: a browser security warning displayed on a projector during a
  defense invites a question about whether the system is secure, and the honest
  answer — "that warning is about a development certificate, not about the
  system" — costs time and credibility that the setup would have saved.

A self-signed certificate must never reach deployment. Certificate and key
files are excluded from version control; **a private key is a credential**, and
committing one is permanent.

---

## 3. Evaluation limitations — the section that matters most

The system reports **100% accuracy** on both of its evaluation methods. Neither
figure is a generalisation result, and both must be reported with what follows.
This is the area where an examiner is most likely to press.

### 3.1 What was actually measured

| Method | Result | What it demonstrates |
|---|---|---|
| In-sample verification | 300/300, average distance **0.00** | That the train → save → load → predict pipeline works. **Not accuracy.** |
| 80/20 held-out split | 60/60, average distance **34.95** | Recognition on images the model did not train on — with four significant caveats below. |

The in-sample figure is 100% *by construction*: LBPH stores one histogram per
training image, so every test image matches its own stored histogram exactly.
An average distance of 0.00 is the giveaway. It is a wiring check, and
presenting it as an accuracy result would be a straightforward error.

### 3.2 The four caveats on the held-out result

1. **Same-session data leakage.** All 100 images per student come from one
   continuous capture session. The 80/20 split therefore separates
   near-duplicate consecutive video frames, not genuinely independent samples.
   The "unseen" test images closely resemble the training images. This is a
   same-session split, not an out-of-sample estimate.
2. **Closed set — no impostors.** Every test image belongs to an enrolled
   student. **The false-acceptance rate was never measured.** For an attendance
   system that is the security-critical metric: it answers "how often does the
   system mark the wrong person present?", and this evaluation cannot say.
3. **Three identities.** A three-way classification problem. Discrimination
   difficulty grows sharply with the number of enrolled people, so the result
   does not extrapolate to a class.
4. **Threshold circularity.** The same 58.0 acceptance threshold used in
   production was used to score the evaluation, with no ROC or DET curve and no
   justified operating point.

### 3.3 What may honestly be claimed

That the recognition pipeline is correctly implemented, persists and reloads
its model, and correctly identifies all three enrolled students on held-out
frames from the same capture session at an average LBPH distance of 34.95
against an acceptance threshold of 58.0.

That is a real result and it is worth stating plainly. What it is not is a
measurement of how the system would perform on a class of thirty students, on a
different day, under different lighting, with people it has never seen
attempting to be recognised.

### 3.4 What a defensible evaluation would require

Capture across at least two separate sessions per student on different days and
under different lighting; a set of non-enrolled faces; and reporting **FAR,
FRR and EER** with a DET curve and a justified operating threshold. This is
planned — see §5.

---

## 4. Scale limitation

**The system cannot support a full class, and the limit is measurable.**

LBPH stores one histogram per training image, so the model grows linearly with
enrolment: **55 MB for 3 students, about 18.3 MB per student**.

The binding constraint is not disk space but OpenCV itself. Round-trip testing
showed that OpenCV's file storage reads a 0.572 GB model successfully and fails
on a 1.835 GB model with an assertion error — the write succeeds and the read
then fails. That places a hard ceiling somewhere between **roughly 31 and 100
students**; the exact point was not bisected, so **31 is the figure to plan
against**.

Two things make this worth stating carefully rather than burying:

- **Exceeding the ceiling does not degrade gracefully.** It reproduces a
  failure this system has already experienced: OpenCV writes a model it then
  cannot read back, and recognition stops working entirely.
- **The current configuration is what buys the headroom that exists.** At the
  previous parameter setting the model was 1.835 GB *at three students* and
  already unreadable.

**This is a limitation, not a defect.** Keeping this recognition approach was a
deliberate decision taken on 2026-08-08: the manuscript is written and the
thesis is at prefinal defense, and changing the recognition model would mean
rewriting it. A constant-size approach — where the model does not grow with
enrolment at all — would remove the ceiling entirely, and that is the honest
answer to "how would you fix this?"

---

## 5. Known and planned — do not document these as final

Four areas are actively changing. Anything written about them now will be
wrong by the defense.

| Area | Current state | Planned |
|---|---|---|
| Enrolment capture | OpenCV window on the server | Browser-based capture over HTTPS |
| Transport | Plain HTTP | HTTPS: self-signed in development, real certificate in deployment |
| Evaluation | Same-session, closed-set, N=3 | Multi-session recapture, impostor set, FAR/FRR/EER with a DET curve |
| Recognition threshold | 58.0, uncalibrated | Re-derived from the DET curve above |

**The dataset will be recaptured**, which has a consequence for the manuscript:
the accuracy figures in §3 are expected to *change*, and most likely to fall,
because the new evaluation is a harder and more honest one. A lower number
reported against a rigorous protocol is a stronger result than 100% against a
same-session split, and the write-up should be structured so that this reads as
the intended progression rather than a regression.

**Everyone appearing in the impostor set is a data subject** and requires the
same written consent as an enrolled student, worded for the actual purpose.
They are never enrolled, but their face is still captured and processed.

---

## 6. Functional gaps

> ⚠️ **Revised 2026-08-16.** This section was written during the Phase 0 audit
> and listed nine gaps as outstanding. **Eight of them have since been closed**,
> and it had not been updated — so a reader was being told the system could not
> record an absence, count a dashboard, or correct a mistake, all of which it
> does. The closed items are kept below rather than deleted, because the
> write-up needs the before as well as the after.

### 6.1 Still outstanding

| Gap | Effect |
|---|---|
| **Liveness is defeatable by a video replay** | The check is two fixed head poses with no depth, texture or blink component, so a phone playing a recording of an enrolled student passes it |
| No consent record in the schema | Consent is collected on paper (§0.1 of the UAT manual). Nothing in the database records that it was given, or allows it to be withdrawn |
| No encryption at rest for `dataset/` and `trainer/` | Face images and the templates derived from them sit on disk in the clear |
| No automated retention or erasure | Deleting a student is a manual, multi-step process; nothing expires on its own |
| Still the Flask development server | TLS is configured on it, which is not the same as a WSGI server behind a reverse proxy |

The first one deserves its own sentence in any section that claims
anti-spoofing: **the current liveness check raises the effort required to spoof
the system; it does not prevent it.** Overstating this is the kind of claim an
examiner can disprove in the room with a phone.

### 6.2 Closed since this section was written

Listed with the finding ID, so the write-up can trace each one.

| Gap as originally recorded | Closed by |
|---|---|
| No student↔subject relation in the schema — ending a session marked *every student in the database* absent | **FS-3**, migration 002 (`enrolments`) and the Class List screen |
| Absences are never saved — reports always showed zero | **FS-4**. The absent register is written when a session ends, scoped to the class list |
| The dashboard is not wired up — four hardcoded zeros | **FS-5** |
| No late policy — every recorded attendance is "Present" | **FS-8**, migration 005. ⚠️ **And genuinely only true from 2026-08-16:** the derivation was implemented in Phase 4 but the recognition path passed a hardcoded `"Present"`, so it could not run. See R2 in `tasks/review-phase-5.md` |
| No attendance correction screen | **FS-10**, migration 006 — with an audit row, so no correction is anonymous |
| Excel export ignores the report filters | **FS-11**. It also no longer leaves a copy of the register on disk (**PE-8**) |
| Model loading blocks application startup — roughly 9 seconds | **PE-4**. Nothing loads at import; the model is re-read only when the file on disk changes |
| Training runs inside the web request | Background job with a progress endpoint |

---

## 7. Corrections — claims from earlier drafts that must not be reused

An earlier version of the project documentation contained the following. Each
was checked against the code and the data.

| Earlier claim | Verdict |
|---|---|
| "In-sample: 100.00% (300/300), average distance 16.57" | **Unreproducible.** The harness contained a bug that made it score **zero images** while printing a clean summary table. |
| "Held-out: 100.00% (60/60), average distance 24.36" | **Unreproducible as stated.** It was measured under a configuration whose true result was **0/60**. |
| "Tuning to `neighbors=12` captures larger spatial facial structures" | **Incorrect, and inverted.** That change is what broke the system. |
| "Augmentation expands the dataset to 1,200 training images" | **Accurate but counterproductive.** It did — for four times the storage and four times the per-prediction cost, with no measured accuracy gain. |
| "Reduced identification latency from ~2.5 s to ~1.0 s" | **Unsubstantiated.** No timing harness existed to produce those numbers. |
| "+5–10 FPS from threaded camera acquisition" | **Unsubstantiated.** No FPS measurement existed. |

Two of these are more instructive than embarrassing, and the manuscript is
stronger for including them: an optimisation was documented as an improvement
when it was in fact the defect that took the system down, and an evaluation
harness reported a clean result while measuring nothing at all. Both were found
by re-running the measurements rather than by reading the code. That is a
defensible methodological point, and it is a better story than a correction
quietly dropped.

---

## 8. Where the numbers come from

Everything quantitative in this document is reproducible.

**A note on units, because it will otherwise cause a correction later.** File
sizes here are decimal (1 MB = 1,000,000 bytes), which is what the project has
used throughout. The model is **54,984,159 bytes** — that is 55 MB decimal, but
Windows Explorer will report it as **52.4 MB**, because Explorer labels binary
units (1,048,576 bytes) with the decimal symbol. Both describe the same file.
Quote the byte count where precision matters, and keep one convention across
the manuscript.

| Figure | Source |
|---|---|
| 100% in-sample, 300/300, distance 0.00 | `python eval_accuracy.py` |
| 100% held-out, 60/60, distance 34.95 | `python eval_heldout_accuracy.py` |
| Model 55 MB (54,984,159 bytes), 3 identities | `trainer/trainer.yml` after `python train_model.py` |
| ~18.3 MB per student | 55 MB ÷ 3 identities |
| Model load 9.2 s; 64.2 ms per prediction | Phase 0 benchmark, `docs/walkthrough.md` §3 |
| OpenCV read ceiling between 0.572 GB and 1.835 GB | Phase 0 round-trip test, `docs/walkthrough.md` §3 |
| Distances shift to 81–107 at `neighbors=12` | Same test |
| 205 automated tests | `pytest tests/ -q` |
| 35 application routes, all access-controlled | `tests/test_route_security.py` |

Fuller detail, including the comparison table that justifies the current
parameters: `docs/walkthrough.md` §§3–5. Privacy and data-protection specifics:
`docs/data_privacy.md`. The full audit with severity ratings:
`tasks/todo.md` §2.
