# Handover — Phase 6a → Phase 6

**Sprint:** Phase 6a — the UAT's defects, in three rounds
**Date:** 2026-08-21
**Status:** ✅ **Eight defects across three rounds**, all fixed, tested and
mutation-tested. Each round was only reachable because the previous one landed:
A put the enrolment page on screen, which produced D/E/F; D put a *usable*
capture page on screen, which produced G/H. **A and D are confirmed on
hardware; the rest are not** — see §6.
**Read with:** [`handover-phase-5b.md`](handover-phase-5b.md) (still required,
still accurate except where §4 corrects it), [`lessons.md`](lessons.md) —
**L17-L24 are new and all eight came out of this sprint**

⚠️ **This checkout is not the one earlier handovers measured.** It is a fresh
copy at `AI-Attendance-System-main/`, **it is not a git repository**, and its
numbers differ from `handover-phase-5b.md` §2. Everything in §2 below was
re-measured today. Do not carry a figure from an earlier document ([L8](lessons.md)).

---

## 1. Read this first — what will bite you

### 1.1 ⚠️ There is no git here. `git checkout --` is not available as an undo

`git status` fails; `handover-phase-5b.md`'s commit table refers to a
repository this directory does not have. Two consequences:

- **Mutation testing must restore from a copy** ([L13](lessons.md)), which is
  what this sprint did. There is no other undo.
- **`tests/test_vision_pose.py` and `test_vision_enrolment.py` skip 8 cases**
  that read a pre-refactor blob with `git cat-file`. That is why the skip
  count is 50 here and 42 in `handover-phase-5b.md`. Nothing is wrong.

### 1.2 ⚠️ The dev tooling was not installed, and the suite had never been run here

`pytest` and `ruff` were both absent from `.venv`. Installed this sprint at
the versions `pyproject.toml` already pinned (`pytest==9.1.1`,
`pytest-cov==7.1.0`, `ruff==0.16.1`). If a future checkout looks "broken",
check this first.

### 1.3 ⚠️ **The deployment database was rebuilt from scratch on 2026-08-21**

Read straight off `logs/app.log`, not inferred:

```
10:01  Cannot start: No database server is answering on 127.0.0.1:3306
10:02  Cannot start: The database 'attendancesystem_db' does not exist
10:02  Applying migration 001_baseline … 007_absence_has_no_arrival_time
10:02  Seeded the default admin account (username 'admin', password 'admin')
```

So **`students`, `subjects`, `enrolments` and `attendance` are empty**, and the
admin password is back to the default.

**Recognition still runs**, and this is worth understanding rather than
assuming: `label_map` is built from `trainer/labels.txt`, **not** from the
database (`load_model_and_labels()`), and that file still holds the three
trained identities. `_attach_display_names()` fills names from `students` and
falls back to the ID when the row is gone. So a student is still *recognised* —
but `save_attendance()` will answer **"Not Enrolled"** for everyone until the
rows exist again.

⚠️ **You cannot test finding C without re-creating at least one student and one
enrolment.** Populating `enrolments` has now been the first handed-over item on
six consecutive handovers.

### 1.4 ⚠️ `dataset/` holds three duplicate folder pairs and one empty folder

`12345/` (0 images), and `23-1-1-0559_Chrizol D. Evangelista/` beside
`23-1-1-0559/`, twice more for `-0918` and `-0920`. **They are inert** —
`validate_student_id()` refuses a name with spaces and dots, so
`parse_dataset_folder()` returns None and `train_model.py` skips them; that is
why `trainer/labels.txt` has exactly three clean labels and `trainer.yml` is
55 MB (≈18.3 MB/student) rather than 110 MB.

Left alone deliberately. They are face images of identifiable students and
deleting them is the user's call, not an agent's.

### 1.5 ⚠️ `capture_dataset.py` is present here, and several documents say it was deleted

`handover-phase-5b.md` and `todo.md` CO-2 both state Phase 5 deleted it. In
this checkout it exists. Nothing imports it — `recognize_face.py` and
`vision/enrolment.py` are the only consumers of the gates it used to duplicate,
and `web/enrolment.py` never spawns it. It appears to be a leftover of how this
copy was made rather than a revert. **Do not build on it**, and do not "restore"
anything from it: MA-4's duplicate-gate finding is closed by `vision/`, and
that file is where the duplicates used to live.

### 1.6 ⚠️ A **stale copy of the whole project** is installed in `.venv`

`.venv/Lib/site-packages/` contains `vision/`, `infra/`, `recognize_face.py`,
`camera_utils.py` and `face_preprocessing.py` — a snapshot of this project as
it was at the start of this sprint, installed as `ai-attendance-system 0.1.0`.
Verified: every file this sprint edited **differs**, every file it did not
**matches byte for byte**.

**The app and the suite are safe.** Running `python app.py` from the project
root puts the root at `sys.path[0]`, and `tests/conftest.py` inserts it
explicitly, so the working tree wins in both.

⚠️ **A scratch script run from anywhere else gets the stale code**, silently
and with no error. It cost me a confusing `TypeError: get_head_pose() takes 1
positional argument but 5 were given` against a function that plainly took
five — because the *installed* one still took one. This is [L5](lessons.md),
import shadowing, with a whole project behind it rather than one module.

Run one-off scripts with the root on the path:

```bash
PYTHONPATH=. .venv/Scripts/python.exe some_script.py
```

Reinstalling (`pip install -e .`) would replace the copy with a link and remove
the trap permanently. Left alone this sprint: it changes how the project is
installed, and that is a decision rather than a fix.

### 1.7 ✅ The "MariaDB stops on its own" warning is still wrong

`handover-phase-5b.md` §1.6 settled this: XAMPP's MySQL does not auto-start
after a Windows restart. The 10:01 line in §1.3 above is another instance —
the user started it by hand at 10:02. Not a reliability finding, not for the
thesis.

---

## 2. Verified current state

Re-measured today, in this order, on this checkout.

| Check | Result |
|---|---|
| `ruff check .` | **clean** |
| `pytest tests/ -q` **before** this sprint | **1,155 passed, 50 skipped** |
| `pytest tests/ -q` **after** | **1,210 passed, 50 skipped** (+55) |
| `get_head_pose` on all 225 POSE images | **STRAIGHT 75/75, LEFT 45/45, RIGHT 45/45, UP 30/30, DOWN 14/30** — see §3c |
| `tests/test_recognition_loop_smoke.py` | **17 passed** (14 before), real faces, real MediaPipe, real LBPH |
| Routes | **48** — ⚠️ 46 before; this sprint added two |
| Schema version | `schema_migrations` at **007**, all seven applied 2026-08-21 |
| Live rows | students **0**, subjects **0**, enrolments **0**, attendance **0**, admin **1** (default password) |
| `trainer/labels.txt` | 3 identities — `23-1-1-0559`, `23-1-1-0918`, `23-1-1-0920` |
| `trainer/trainer.yml` | 54,984,159 bytes |
| `dataset/` folders | 7, of which **3 are usable** (§1.4) |
| `selected_camera.txt` | **does not exist** — no saved preference, which is finding B |
| Python | 3.11.5 |

### Re-establish the baseline

```bash
.venv/Scripts/python.exe -m pip install pytest==9.1.1 pytest-cov==7.1.0 ruff==0.16.1
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m pytest tests/ -q                          # 1210 / 50
.venv/Scripts/python.exe -m pytest tests/test_recognition_loop_smoke.py -q   # 17
```

⚠️ `eval_heldout_accuracy.py` was **not** re-run this sprint and its 60/60 is
not re-confirmed here. Do not quote it without re-measuring, and not then
without `docs/limitations.md` and `docs/walkthrough.md` §4–§5.

---

## 3. What changed, and why each one is not what it was reported as

Six defects in two rounds. **Every one was reported as one thing and turned
out to be another**, which is the pattern worth carrying forward more than any
individual fix.

Rounds 2 and 3 exist because of the rounds before them: fixing A put the
enrolment capture page in front of a person for the first time, and fixing D
made it usable enough to reach the *second* stage. Each fix bought the next
finding.

### ⭐ A — "camera is not opening on enrollment" → a template quoting bug

**`templates/enrol.html:19`** carried `data-record="{{ record | tojson }}"`.
`tojson` escapes `<`, `>`, `&` and `'` — **not `"`**. The first `"` of the JSON
closed the attribute, `getAttribute()` returned the single character `{`,
`JSON.parse` threw at `static/js/enrol.js:34`, and the IIFE died before
`openCamera(null)` on its last line. The page then showed every placeholder the
server had rendered, which is exactly the screenshot the user sent.

**Verified, not inferred:** rendered through the app's own Jinja environment and
parsed with `html.parser`.

**It only ever bit new enrolments.** Recapture passes `record = {}` — no quote —
so that half of the feature worked, which is how it reached a UAT.

Fix: single-quote the attribute. Plus the comment above it, which asserted the
opposite ("an attribute value needs no guarding at all") and is now [L17](lessons.md).

### ⭐ C — "the same verified student keeps getting an unknown verdict" → a gate in the wrong place

Two independent defects on one path. Neither is a recognition-accuracy problem;
see §5 for the three accuracy hypotheses that were measured and ruled out
first.

1. **Frontality gated whether a face was *tracked*.**
   `face_passes_geometry_gate()` runs in `_stream_frames()` **before**
   `tracker.assign()`, and a rejected frame hits a bare `continue`. So a
   student obeying "Turn LEFT" past 0.28 × face-width was not merely
   un-recognised — the frame was dropped, the box vanished, and the track's
   `last_seen` was never refreshed, so `expire_old_tracks()` deleted the track
   1.5 s later. They turned back to "Verifying 0/20", re-confirmed, drew a
   fresh random challenge, and repeated. **[L18](lessons.md).**

   Fixed by splitting `vision/validation.py` into
   `structure_rejection_reason()` and `frontality_reason()`.
   `rejection_reason()` is now their composition and behaves identically —
   pinned by `test_the_split_reassembles_into_the_original_gate`. Frontality
   moved from *"may I see you"* to *"may I decide about you"*: it gates
   confirmation and nothing else. **An attendance mark still requires a
   frontal face.**

2. **`process_confirmed_track()` treated "I read someone else" and "I could
   not read this face" as the same evidence.** Both incremented
   `mismatch_frames`, so two frames redrew the challenge and five destroyed the
   identity — 0.13 s and 0.33–0.7 s at 7–15 fps, on precisely the frames a
   requested head movement produces. **[L19](lessons.md).**

   The branch is split. A contradiction keeps the original counters exactly.
   An unreadable frame gets `unreadable_frames` against a new
   `max_unreadable_frames_before_clear = 25`, does **not** redraw the
   sequence, and keeps the student's name on screen.

3. **Neither failure wrote a line anywhere**, which is why the only account of
   this was a person watching a screen — FS-14 and US-3's shape again. Three
   WARNINGs added, one per transition rather than per frame, modelled on
   `describe_confirmation_block()`'s `distance` branch.

### B — "defaults to the laptop cam" → no UI for the server's camera

`open_best_camera()` scans upward from 0 and takes the first live device. That
is a correct default and was the only option: `selected_camera.txt` could only
be written by the scan itself, and no page said a second camera existed.

`GET /cameras` and `POST /cameras/select`, plus a picker on the attendance
page. **No plumbing into `session.start()` was needed** — it calls
`open_camera()` with no arguments, and `save_camera_index()` writes the file
`open_best_camera()` already consults second.

⚠️ **Not the same thing as the enrolment picker.** Enrolment runs on
`getUserMedia`, where the *browser* enumerates its own devices; recognition
opens the classroom camera with OpenCV on the server, which the browser cannot
see. Two pages, two pickers, no shared code — that is the architecture, not an
oversight.

Two behaviours worth knowing:
- **The scan is refused while a session is running.** Probing opens each device
  and would fight the running session for it.
- **`/cameras/select` re-scans to validate the index rather than parsing it.**
  CAM-1 is the finding that a camera can open, read and return black; the saved
  index is tried *first* on the next run, so writing an unproven one would put
  the dead device back exactly where CAM-1 removed it from.

---

---

## 3b. Round 2 — what fixing A revealed

⚠️ **Reported as three things. Two of them are one cause.**

> *"capture is too slow, camera view is overflowing the screen and capture
> direction is ambiguous"*

### ⭐ D — the capture page had no stylesheet at all

**Not one class on `templates/enrol.html` had a CSS rule.** Ten classes, zero
rules — `capture-wrapper`, `capture-stage`, `capture-overlay`,
`capture-instruction`, `capture-message`, `progress-track`, `progress-fill`,
`stage-list`, `camera-picker`, `camera-detail`.

`enrol.js` was already driving all of it. None of it rendered:

| the JS did this | with no CSS it |
|---|---|
| `drawBox()` painted the face rectangle onto `.capture-overlay` | drew onto a `<canvas>` **sibling** of the video which, with no `position:absolute`, stacked underneath as its own block. **The face box had never once appeared on the picture.** |
| `progressFill.style.width = "21%"` | set a width on a `<div>` with no height and no background |
| marked each stage `done` / `current` / `todo` | all three looked identical |
| assumed a mirrored preview — `drawBox()` says it mirrors coordinates *"to match the preview, which is flipped"* | **nothing flipped it.** Turning left made the picture appear to turn right |
| asked getUserMedia for 1920x1080 | the `<video>` laid out at intrinsic size and pushed the page off the viewport |

So *"camera view is overflowing"* and most of *"capture direction is
ambiguous"* are the same finding. **Nothing raised, and the suite was green
through all of it** — it checks structure, endpoints, escaping and access, and
a missing stylesheet is none of those. New [L20](lessons.md).

⚠️ **The tell was already in the template:** inline `style=` attributes on the
camera picker. Those were not a shortcut, they were the symptom — the only
place left to make one element look deliberate. They are gone; the page is
styled to the attendance page's own tokens so the two camera screens read as
one system.

### ⭐ E — capture was too slow, and 90% of it was one line

`EnrolmentSession._save()` called `_reset_hold()`, so a subject re-earned
`hold_frames` — six uploaded frames — **for every one of the 15 images in a
stage**, while standing still in a pose they had never left.

Measured, before touching anything:

| | |
|---|---|
| server work per frame | **15 ms** (median of 20, 1920x1080) |
| browser JPEG encode | 39 ms |
| upload tick | 200 ms |
| **floor for 100 images** | **114 s of flawless posing**, of which **102 s was the per-image hold** |

The 200 ms tick was justified by a comment reading *"the server spends roughly
50-150 ms on a frame"* — a figure carried from the **recognition** loop, which
runs LBPH on every tracked face. Enrolment does MediaPipe plus one alignment
and no matching at all. Nobody had measured the path the constant was pacing
([L9](lessons.md) again, now [L22](lessons.md)).

Three changes, one of them free:

| | before | after |
|---|---|---|
| hold | re-paid per image | **paid on stage entry** |
| `FRAME_INTERVAL_MS` | 200 | **120** |
| `CAPTURE_DELAY` | 0.80 s | **0.40 s** |
| **floor** | **114 s** | **42 s** (63% faster) |

⚠️ **`MAX_IMAGES` is deliberately still 100** — the user's decision (§8). That
is what keeps the model at ~18.3 MB/student, the LBPH scaling ceiling in
`todo.md` §3a unmoved, and every accuracy figure quotable without
re-measurement.

⚠️ **Nothing about what is saved was relaxed.** Every gate still runs on every
frame, and `_refuse()` still resets the hold on **any** failure, so a broken
pose must be settled again. That half has its own test — without it a subject
could drift between images and collect a worse dataset at full speed, which
would be invisible until accuracy moved.

### F — "capture direction is ambiguous"

Mostly D. Two things beyond it:

- **The mirror.** `get_head_pose()` decides from `nose.x - eye_centre.x` on the
  *un-mirrored* uploaded frame, so **LEFT means the subject's own left** — but
  that is only obvious if the picture behaves like a mirror. It now does
  (`transform:scaleX(-1)`, which `drawBox()` had always assumed), and a line
  under the instruction says so outright.
- **Prominence.** The instruction was body text below an unbounded video, i.e.
  off-screen. It is now the largest text on the page, directly under a bounded
  picture, with the nine-stage checklist beside it showing where you are.

### What guards D and E now

- `test_every_class_on_a_camera_page_has_a_rule` — parses `enrol.html` and
  `attendance.html`, asserts every class has a rule. ⚠️ **Scoped to those two
  on purpose**; six other templates carry nine unstyled classes between them
  (`back-btn`, `action-btn`, `export-btn`, `welcome-box`, `form-help`,
  `no-records`). Cosmetic, pre-existing, and named in §7 rather than quietly
  fixed or quietly ignored.
- `test_the_capture_overlay_is_positioned_over_the_video` — pins
  `position:absolute`, its containing block, and `#preview`'s bounded height by
  **value**. The class-coverage test above would pass on
  `.capture-overlay{color:red}`; this one would not.
- `test_the_hold_is_paid_once_per_stage_not_once_per_image` and
  `test_a_broken_pose_still_has_to_be_settled_again` — the speedup and the
  guard it must not have removed.

⚠️ **One of these was vacuous and a mutation run caught it**, which is the most
useful thing round 2 produced. The class-coverage test scanned the raw
stylesheet with a regex — and `style.css` *documents itself*, naming
`.capture-overlay` and `.capture-stage` in prose. Deleting a real rule left the
test **green**. That is [L12](lessons.md) — a grep matching the comment that
explains the code — happening **inside a test file whose own docstring opens
with "Everything here parses the HTML. Nothing greps it."** Comments and
declaration blocks are stripped before scanning now. New [L21](lessons.md).

---

## 3c. Round 3 — "it can't detect looking forward face"

⚠️ **Read this one even if you skip the rest.** It is the same bug the project
has now shipped three times, and the second time it was *written down and
deferred with an argument nobody checked*.

### ⭐ G — STRAIGHT was a statement about distance, not about a head

`get_head_pose()` compared yaw and pitch against fractions of the **frame**.
`pitch = nose.y - eye_centre.y` grows with the face, and STRAIGHT required it
between `PITCH_UP` (0.095) and `PITCH_DOWN` (0.110) — **a window 0.015 wide**.

Measured over the 300 real enrolment images, composited at a range of sizes,
asking what a face **looking directly at the camera** was called:

| face box | area ratio | verdict |
|---:|---:|---|
| 400 px | 0.05 | **UP** 15/15 |
| 500 px | 0.08 | STRAIGHT 13, UP 11 |
| 560 px | 0.10 | STRAIGHT 15, DOWN 8 |
| 600 px | 0.12 | **DOWN** 22/24 |
| 800 px | 0.21 | **DOWN** 23, RIGHT 1 |
| 1050 px | 0.36 | **DOWN** 22, RIGHT 2 |

Step back → UP. Lean in → DOWN. And STRAIGHT is the **first** stage, which
every later stage waits behind, so enrolment was unusable outside a ~15% band
of distance. The user's screenshot showed `detected RIGHT`, the large-face
corner of that table.

**`vision/pose.py` knew, and the containment argument was never checked.** Its
docstring carried a ⚠️ naming SE-12 and saying these thresholds had the same
shape of bug, then argued it was contained because *"the enrolment stages also
constrain distance"*. That covers **15 of the 100 images** — only CLOSE, MEDIUM
and FAR pin the ratio. The five POSE stages pin nothing, and `good_distance()`
admits a **45×** range of face area. A test even pinned the defect as a feature
(`assert 0.015 == PITCH_DOWN - PITCH_UP`, under *"a narrow band … spelled out
because it is surprising"*).

**Fix.** Every threshold is a fraction of face width or face height now, and
`get_head_pose()` **requires the face box**, so the frame-relative version is
unrepresentable rather than discouraged. Derived from the enrolment set:

| | old (of frame) | new (of face) |
|---|---|---|
| turn | `YAW_TURNED` 0.015 | `STRAIGHT_MAX_YAW_OF_WIDTH` **0.085** |
| tipped back | `PITCH_UP` 0.095 | `PITCH_UP_OF_HEIGHT` **0.200** |
| tipped forward | `PITCH_DOWN` 0.110 | `PITCH_DOWN_OF_HEIGHT` **0.262** |

Validated against every POSE image:

| stage | correct |
|---|---|
| STRAIGHT | **75/75** — and 100% at every face size from 400 to 1050 px |
| LEFT | 45/45 |
| RIGHT | 45/45 |
| UP | 30/30 |
| **DOWN** | **14/30** |

⚠️ **DOWN is a deliberate trade, not an oversight.** STRAIGHT runs to pitch
0.257 and DOWN starts at 0.234 — they **genuinely overlap**, because the
instruction is "look *slightly* down" and the subjects obliged. No single
threshold separates them. The line sits just above STRAIGHT's maximum so that
**all 75 STRAIGHT images pass**, since that is the reported defect and the
gate every other stage waits behind. The cost is that DOWN wants a more
definite movement than this dataset recorded; the subject is told so on screen
(`LOOK SLIGHTLY DOWN (detected STRAIGHT)`) and the stage still completes.
**If DOWN proves annoying in use, lowering `PITCH_DOWN_OF_HEIGHT` toward 0.258
trades it back — but every step toward DOWN is a step back toward the bug that
was just fixed.**

### ⭐ H — the browser was uploading a mirrored world

Found while fixing D. `grabFrame()` mirrored every uploaded frame, under:

> *"Undo the preview's mirror before uploading: the operator sees a mirror, the
> server must see the real orientation or LEFT and RIGHT are swapped."*

Right about the stakes, wrong about the mechanism. **`drawImage(video)` samples
the decoded frame; no CSS transform touches it.** There was no preview mirror
to undo — the flip *introduced* one. `LEFT` is `nose.x > eye_centre.x`, a turn
to the subject's own left as the camera sees it, so **a student told "TURN
SLIGHTLY LEFT" could only satisfy it by turning right**.

And it was not only the labels: **the saved crop was a mirror image of the
face.** Verified across all three paths — `capture_dataset.py` (the OpenCV
enrolment this replaced) and `recognize_face.py` mirror nothing; browser
enrolment was the only path that did. It would have trained the model on
mirrored faces and matched un-mirrored ones against them, on a face that is not
symmetric — and produced a dataset inconsistent with the three students already
enrolled.

⚠️ **`drawBox()` was the one place the mirroring was self-consistent, and it
was self-consistent for the wrong reason.** Its comment — *"Mirrored
horizontally to match the preview, which is flipped"* — was false twice over
(the preview had no CSS; the box arrived mirrored) and **the two errors
cancelled exactly there**. Fixing either alone would have broken the face box.
That is why it looked fine and nobody looked further. New [L24](lessons.md).

### What guards G

`test_the_pose_verdict_does_not_depend_on_how_close_the_face_is` — one frontal
head at 100, 250, 500, 800 and 1200 px face boxes, all of which must read
STRAIGHT. It replaces `test_straight_is_a_narrow_band`, which asserted the
defect. Mutation-tested: restoring the frame-relative arithmetic makes it fail
with *"a frontal head reads UP at a 100 px face box"*.

⚠️ **H has no automated guard.** There is no JavaScript test harness in this
project, and adding one is not a thing to do inside a bug-fix round. The
evidence is empirical instead: every LEFT-stage image in `dataset/` classifies
as LEFT, and those were captured by the un-mirrored path. **Confirming H is a
hardware check** — see §6.

## 4. Corrections to earlier documents

1. **`handover-phase-5b.md` §2's "1,143 passed, 42 skipped"** does not hold in
   this checkout. It is **1,155 / 50** at the branch point here, and the 8
   extra skips are the `git cat-file` differential tests (§1.1). Neither number
   is wrong; they are different trees.
2. **`handover-phase-5b.md` §2's "Routes 46 — unchanged"** is now **48**.
3. **`todo.md` CO-2 and `handover-phase-5b.md`** say `capture_dataset.py` was
   deleted. It is present here (§1.5).
4. **`handover-phase-5b.md` §2's live row counts** (students 4, subjects 1) are
   all **0** now — §1.3, and it is a database rebuild, not data loss from this
   sprint.
5. **`templates/enrol.html`'s own comment** claimed "an attribute value needs
   no guarding at all". Corrected in place, with the correction left visible.

---

## 5. What was ruled out by measurement before anything was changed

Recorded because three plausible, confident-sounding hypotheses about finding C
were all wrong, and the scripts that killed them cost minutes. **[L14](lessons.md)
in its own shape: the reported symptom is evidence about a defect and a
hypothesis about its cause.**

| Hypothesis | How it was measured | Result |
|---|---|---|
| LBPH cannot match a turned face | Held-out 80/20 **stratified within each enrolment stage** — same `LBPH_PARAMS`, same preprocessing, same threshold from `settings` | **LEFT 9/9, RIGHT 9/9 correct**, median distance 34.1 / 34.3 against frontal's 34.2. Pose does not break the recogniser |
| The demanded turn is outside the training set | MediaPipe over all 300 enrolment images, yaw converted to face-width units | Liveness needs ≥ 0.10w; enrolment LEFT/RIGHT sit at **0.146w / 0.135w**. Comfortably covered |
| The duplicate `{id}_{Name}` folders split each identity in two | `validate_student_id` + `labels.txt` | Inert (§1.4) |

Two numbers from that work are worth keeping:

- **The gate and the liveness challenge measure yaw from different landmarks**
  — the gate averages four points per eye, `get_face_yaw()` uses the outer
  corners. Measured ratio **gate ≈ 0.87 × liveness**, so the 0.28 ceiling is
  **≈ 0.32w** in liveness units. The usable band was 0.10w–0.32w.
- **Enrolment's "TURN *SLIGHTLY* LEFT" already reaches 80% of that ceiling.**
  The liveness prompt says only "Turn LEFT", with no upper bound and no
  feedback. Worth revisiting in Phase 6 even now the overshoot is survivable.

⚠️ The held-out figure carries the same-session leakage caveat as everything
else here. It is used above only to compare **poses against each other**, and
must not be quoted as accuracy.

---

## 6. Handed to you — six of eight fixes are unconfirmed on hardware

**This is the critical path and none of it is work an agent can do.** All
eight defects were found by a person looking at a screen, and most are
invisible to a test suite by construction — a dead browser IIFE, a missing
stylesheet, an unlogged state machine, a capture that was merely tedious, and a
mirrored upload with no JavaScript harness to catch it.

⚠️ **Three rounds are the argument for doing this promptly.** Each fix bought
the next finding: A put the page on screen (→ D, E, F), D made it usable enough
to reach the second stage (→ G, H). Two of the eight had been shipping since
browser enrolment was written. **Every one of these fixes is a hypothesis until
a person uses it**, and this sprint is three consecutive demonstrations of
that.

1. **Re-create the data.** ⚠️ Blocks item 5 entirely. At least one student row
   matching a `dataset/` folder, one subject, and one **enrolment** — the
   register writes "Not Enrolled" without it (§1.3). Also change the admin
   password back off `admin`.
2. ✅ **Finding A is confirmed.** The user reached "21 of 100 images" on a new
   enrolment, which is the only round-1 fix with field evidence. Round 2 (D, E,
   F) is what that run then reported.
3. ✅ **Findings D and E are confirmed.** The layout is bounded and the face
   box appears — the second screenshot of round 3 shows it, which is the first
   time that rectangle has ever been on screen.
4. **Findings G and H — one capture run, all the way to 100.** This is now the
   critical item.
   - **G:** "LOOK STRAIGHT" must accept you **at any distance** — step back to
     arm's length and lean right in, and it should stay STRAIGHT. That is the
     defect; the table in §3c is what it used to do.
   - ⚠️ **H, and this needs care because nothing automated covers it.** At the
     "TURN SLIGHTLY LEFT" stage, turn to **your own left**. It must be
     accepted. If turning *right* is what works, the upload is still mirrored
     and I have the sign backwards — say so, it is a one-line change.
   - ⚠️ **DOWN is expected to be fussier than the other stages** — see §3c for
     why, and it is a tunable trade rather than a defect. Tell me if it is bad
     enough to be worth trading back.
   - Time the run. The floor is **42 s**; a real one should land near a minute.
5. **Finding B — the camera picker.** `/attendance` should list both cameras
   with resolutions. Choose the USB webcam, start a session, confirm the stream
   is from it and that `selected_camera.txt` now holds that index. Then reload
   the page **with the session running** — the scan must refuse rather than
   fight for the device.
6. **Finding C — the liveness challenge.** With `LOG_LEVEL=DEBUG`:
   - Turn **hard**, past where it used to break. The box must stay, the name
     must stay, the prompt must persist.
   - Complete a challenge normally. Attendance written once; overlay and
     register agreeing on Present/Late.
   - ⚠️ **Have a second enrolled person step into the track mid-challenge.**
     The identity must still be dropped. This is the case the fix must not have
     weakened, and it is the only one that needs two people.
   - `logs/app.log` must now name every restart and every identity loss.
7. **`tasks/notes.txt`** is still uncommitted and still the user's.

Everything in `handover-phase-5.md` §6 that was not done still is not done.

---

## 7. What is NOT done

- **Nothing in Phase 6 proper.** `todo.md` §5's Phase 6 list — consent, the
  recapture, the impostor set, the DET curve, the LBPH scaling ceiling, the
  benchmark suite, SUS, `docs/iso25010_evaluation.md` — is untouched. The FAR
  measurement is still the cheapest first move and still does not need the
  recapture.
- **The liveness prompt still says "Turn LEFT"**, with no upper bound and no
  feedback about the band (§5). Overshooting is now survivable rather than
  fatal, so this is a UX improvement rather than a defect — but the *enrolment*
  instruction says "slightly" and the two should probably agree.
- **`docs/uat_manual.md` was not touched.** Deliberate: [L16](lessons.md) — a
  tester must not be told what was recently broken. The history belongs here.
- **`docs/limitations.md` §6 was not re-checked** against this sprint.
  `handover-phase-5b.md` §6 makes the case for doing that at the end of every
  phase; it was not done, and it is a real gap rather than an oversight I am
  hiding.
- **Nine unstyled classes remain in six other templates** — `back-btn`
  (correct_attendance, edit_subject), `action-btn` (manage_students),
  `export-btn` and `no-records` (reports), `welcome-box` (dashboard),
  `form-help` (correct_attendance). Found while scoping D's guard, all
  cosmetic, none reported. Named here rather than fixed silently or dropped
  silently; `test_every_class_on_a_camera_page_has_a_rule` covers only the two
  camera pages until somebody decides about these.
- **`get_head_pose()`'s DOWN/STRAIGHT overlap is unresolved and unresolvable
  with this data** — see §3c. Three subjects, all of whom took "slightly" at
  its word. Phase 6 calibration with more subjects is where this gets a real
  answer; what it has now is a derivation from every image the project holds,
  which is strictly more than the frame-normalised values ever had.
- **Nothing automated covers finding H** (the mirrored upload). There is no
  JavaScript test harness in this project at all — `tests/test_templates.py`
  parses markup, and that is the closest thing. Worth considering in Phase 6,
  because `enrol.js` is 480 lines of the enrolment path and two of this
  sprint's eight findings lived in it.
- **The liveness prompt still does not say how far to turn** (see below), and
  now that the capture page says "the picture is a mirror, left means *your*
  left", the recognition overlay is the only surface left that does not.
- **No sweep for route behaviour with the session machine in a non-default
  state.** Still the gap `review-phase-5.md` named. `tests/test_camera_routes.py`
  constructs a running session deliberately and both of its refusal cases
  matter — that is three files doing this by hand now, and still no ban.
- **`logs/app.log` still holds 3.1 MB of old test output.** The user's call.

---

## 8. Open decisions

**Nothing is blocked on a decision.** Two were taken with the user on
2026-08-21, both recorded because they shape the thesis:

1. ✅ **Frontality moves to the decision rather than being loosened.** The
   alternative on the table was to keep the architecture and merely reword the
   prompt and raise the counters — smaller, and it would have left the
   overshoot case (box disappears entirely) unfixed. The user chose the
   structural fix. The security property is unchanged: an attendance mark still
   requires a frontal face.
2. ✅ **Capture speed: halve the delay, keep 100 images.** Three levers were
   costed and put to the user (2026-08-21). The structural fix — the hold
   becoming a stage-entry cost — was taken as read. Beyond it: halving
   `CAPTURE_DELAY` gets 114 s → 42 s and **changes nothing the evaluation
   quotes**; cutting `MAX_IMAGES` to 60 would have got ~28 s but moves model
   size, the LBPH scaling ceiling in `todo.md` §3a, and accuracy. The user
   chose the middle. ⚠️ **If capture still feels slow in use, `MAX_IMAGES` is
   the remaining lever and it is a thesis decision, not a tuning one.**
3. ✅ **The camera scan runs on attendance-page load**, not behind a button.
   ⚠️ The cost is a probe of indices 0–4 on **every** visit to `/attendance`,
   which can stall for seconds if a device is held by another application
   (measured at 14.9 s, `SLOW_PROBE_SECONDS`). If that proves annoying in real
   use, moving it behind a button is a one-line change in
   `static/js/attendance.js` — the route already supports it.

`handover-phase-5b.md` §8, `handover-phase-5.md` §8 and
`handover-phase-4b.md` §8 all still stand.

---

## 9. Suggested first move

**Item 1 in §6, then item 4.** The data has to exist before any of this can be
confirmed, and finding C is the one the user actually reported twice.

Then Phase 6, where `handover-phase-5b.md` §9's advice is unchanged: the FAR
measurement from the Georgia Tech impostor set is the cheapest evidence in the
phase and does not wait on the recapture.

**And take §5 into it.** Three confident hypotheses about finding C were wrong,
and the thing that settled it in each case was a fifteen-line script measuring
the actual quantity. This project has now produced that failure at the
parameter level (L2), the evaluator level (L3), the benchmark level (L9), the
migration level (L11), the test level (L12), the schema level (L14), the
escaping level (L17) and the call-site level (L18).

Assume the next reader has no memory of this session.
