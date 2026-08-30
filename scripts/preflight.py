"""
Read-only readiness check for a deployed machine (D1, D2, B3).

    python scripts/preflight.py             # everything
    python scripts/preflight.py --deploy    # only the two retrain checks
    python scripts/preflight.py --subject 3 # also check that subject's schedule

Written for a demo machine that **retrains on every version update**, where the
two failure modes that matter are both silent:

* **D1 - the roster shrank.** `train_model()` skips a student folder with too
  few usable images, keeps training everybody else, and returns success. The
  CLI now exits 3 for that (config/exit_codes.py), but a model can also go
  stale for reasons no exit code sees - a retrain that was never run, a deploy
  that skipped the step, a folder added after the last training.
* **D2 - the model is older than the data.** Model writes are atomic with a
  `.bak` rotation, deliberately, so a failed retrain cannot destroy a working
  model. The cost of that safety is that a failed retrain is *survivable*: the
  application starts, recognises against the previous model, and looks
  entirely healthy while its idea of the roster is one release out of date.

Both are invisible to a person clicking through the UI, because every screen
reads the **database**, and the database is not what recognition uses.

⚠️ **This script only reads.** No INSERT, no UPDATE, no file is written, and
the camera is opened only if `--cameras` is passed. It is safe to run against a
live machine at any time, including minutes before a demo.

Exit status is the point of it: **0** if every check passed, **1** if any
failed, so a deploy can end with this and stop when it is not true.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Run as a script from the repository root, so the root has to be importable.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import settings  # noqa: E402
from infra.db import db_cursor  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


class Result:
    """One check: whether it held, and what to do if it did not."""

    def __init__(self, name, status, detail="", fix=""):
        self.name = name
        self.status = status
        self.detail = detail
        self.fix = fix

    @property
    def failed(self):
        return self.status == FAIL


# ---------------------------------------------------------------------------
# The model, read from disk
#
# Deliberately parsed here rather than imported from recognize_face: this must
# work on a machine where the model is unloadable, which is one of the things
# it is checking for. Reading two small text-ish files cannot fail the way
# cv2.face.LBPHFaceRecognizer_create().read() can.
# ---------------------------------------------------------------------------


def read_model_labels():
    """The student IDs in labels.txt, or None if it is not readable."""
    try:
        lines = settings.labels_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    ids = set()

    for line in lines:
        line = line.strip()

        if not line or "," not in line:
            continue

        _label, _, student_id = line.partition(",")
        ids.add(student_id.strip())

    return ids


def dataset_folders():
    """Student IDs that have a dataset folder with at least one file in it."""
    root = settings.dataset_dir

    if not root.is_dir():
        return set()

    found = set()

    for child in sorted(root.iterdir()):
        # `_migrated_duplicates` is the training run's own quarantine folder
        # and is skipped there too; counting it here would report a student
        # who does not exist.
        if not child.is_dir() or child.name.startswith("_"):
            continue

        if any(item.is_file() for item in child.iterdir()):
            found.add(child.name)

    return found


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_model_covers_every_student(cursor):
    """D1: a student in the database who is not in the model is invisible."""
    labels = read_model_labels()

    if labels is None:
        return Result(
            "model is readable",
            FAIL,
            f"{settings.labels_file} could not be read",
            "Run: python train_model.py",
        )

    cursor.execute("SELECT student_id, name FROM students ORDER BY student_id")
    students = cursor.fetchall()

    missing = [
        f"{row['student_id']} ({row['name']})"
        for row in students
        if row["student_id"] not in labels
    ]

    if missing:
        return Result(
            "every student is in the model",
            FAIL,
            f"{len(missing)} of {len(students)} missing: " + ", ".join(missing),
            "These students cannot be recognised. Check the last retrain for "
            "'Skipped (too few images...)', recapture them, and train again.",
        )

    return Result(
        "every student is in the model",
        PASS,
        f"{len(students)} student(s), all present in labels.txt",
    )


def check_no_stray_identities(cursor):
    """The mirror of the above: a label with no student row is a test leftover."""
    labels = read_model_labels()

    if labels is None:
        return Result("no stray identities in the model", SKIP, "model unreadable")

    cursor.execute("SELECT student_id FROM students")
    known = {row["student_id"] for row in cursor.fetchall()}

    stray = sorted(labels - known)

    if stray:
        return Result(
            "no stray identities in the model",
            FAIL,
            "trained but not a student: " + ", ".join(stray),
            "Recognition will show these by raw ID and refuse to record them. "
            "Remove their dataset folder and retrain. On a machine that "
            "retrains every release, a stray folder comes back every release.",
        )

    return Result("no stray identities in the model", PASS, "labels.txt matches students")


def check_model_is_not_stale():
    """D2: a model older than the data it should have been built from."""
    try:
        model_mtime = settings.trainer_file.stat().st_mtime
    except OSError:
        return Result(
            "model is newer than the dataset",
            FAIL,
            f"{settings.trainer_file} is missing",
            "Run: python train_model.py",
        )

    newest_name = None
    newest_mtime = 0.0

    for folder in sorted(settings.dataset_dir.glob("*")):
        if not folder.is_dir() or folder.name.startswith("_"):
            continue

        for item in folder.iterdir():
            if item.is_file() and item.stat().st_mtime > newest_mtime:
                newest_mtime = item.stat().st_mtime
                newest_name = folder.name

    if newest_name is None:
        return Result("model is newer than the dataset", SKIP, "no dataset images")

    if newest_mtime > model_mtime:
        gap = timedelta(seconds=int(newest_mtime - model_mtime))

        return Result(
            "model is newer than the dataset",
            FAIL,
            f"{newest_name} has images {gap} newer than trainer.yml",
            "The model predates the face data, so the last retrain either "
            "failed or never ran. The previous model is still in place and "
            "the application will look healthy. Run: python train_model.py",
        )

    return Result(
        "model is newer than the dataset",
        PASS,
        "trainer.yml is newer than every dataset image",
    )


def check_every_student_is_in_a_class(cursor):
    """B3: the register is scoped to the class list, so no class means no record."""
    cursor.execute(
        """
        SELECT s.student_id, s.name
        FROM students s
        LEFT JOIN enrolments e ON e.student_id = s.student_id
        GROUP BY s.student_id, s.name
        HAVING COUNT(e.subject_id) = 0
        ORDER BY s.student_id
        """
    )
    # ⚠️ COUNT(e.subject_id), never COUNT(*). A LEFT JOIN with no match still
    # supplies one row with NULLs, so COUNT(*) reports 1 for a student in no
    # class at all - the exact opposite of what this asks. Same trap as the
    # Classes column in repositories/students.py.
    orphans = cursor.fetchall()

    if orphans:
        names = ", ".join(f"{row['student_id']} ({row['name']})" for row in orphans)

        return Result(
            "every student is in a class list",
            FAIL,
            f"{len(orphans)} in no class: {names}",
            "Recognition will refuse them with 'Not in this class' and the "
            "register will be blank. Add them via Class List for the subject.",
        )

    return Result("every student is in a class list", PASS, "no student is classless")


def check_demo_content(cursor):
    """Instructors and attendance history: what makes a demo showable."""
    cursor.execute("SELECT COUNT(*) AS n FROM instructors")
    instructors = cursor.fetchone()["n"]

    cursor.execute("SELECT COUNT(*) AS n FROM attendance")
    records = cursor.fetchone()["n"]

    problems = []

    if instructors == 0:
        problems.append("no instructor account (role separation cannot be shown)")

    if records == 0:
        problems.append("no attendance rows (Reports and the export are empty)")

    if problems:
        return Result(
            "there is something to demonstrate",
            FAIL,
            "; ".join(problems),
            "Add one instructor, and run a full session once so the reports "
            "have history in them.",
        )

    return Result(
        "there is something to demonstrate",
        PASS,
        f"{instructors} instructor(s), {records} attendance row(s)",
    )


def check_subject_schedule(cursor, subject_id):
    """R3: no grace period, so a start time in the past marks everyone Late."""
    cursor.execute(
        "SELECT subject_code, time_in FROM subjects WHERE id = %s", (subject_id,)
    )
    row = cursor.fetchone()

    if row is None:
        return Result(
            "the demo subject starts later today",
            FAIL,
            f"no subject with id {subject_id}",
            "Pass --subject with an id that exists.",
        )

    scheduled = row["time_in"]

    if scheduled is None:
        return Result(
            "the demo subject starts later today",
            PASS,
            f"{row['subject_code']} has no scheduled start, so nobody is Late",
        )

    if isinstance(scheduled, timedelta):
        scheduled = (datetime.min + scheduled).time()

    if scheduled <= datetime.now().time():
        return Result(
            "the demo subject starts later today",
            FAIL,
            f"{row['subject_code']} started at {scheduled}, which has passed",
            "derive_status() has no grace period, so every student recognised "
            "from now on is recorded as Late. Move the scheduled start.",
        )

    return Result(
        "the demo subject starts later today",
        PASS,
        f"{row['subject_code']} starts at {scheduled}",
    )


def check_cameras():
    """B4: a frozen device is a still image that looks like a working session."""
    from camera_utils import describe_available_cameras

    cameras = describe_available_cameras()
    live = [camera for camera in cameras if not camera["frozen"]]

    if not cameras:
        return Result(
            "a live camera is available",
            FAIL,
            "no camera produced a picture",
            "Attach a working camera. A session cannot recognise anybody.",
        )

    if not live:
        return Result(
            "a live camera is available",
            FAIL,
            f"{len(cameras)} device(s), all frozen (still images)",
            "start_camera() will take a frozen device and report success, so "
            "the session looks normal and recognises nobody.",
        )

    return Result(
        "a live camera is available",
        PASS,
        f"{len(live)} live device(s) at index " + ", ".join(str(c["index"]) for c in live),
    )


# ---------------------------------------------------------------------------
# Running them
# ---------------------------------------------------------------------------


def run_checks(deploy_only=False, subject_id=None, cameras=False):
    """Every check, in order of how badly its failure hurts."""
    results = []

    with db_cursor(dictionary=True) as cursor:
        results.append(check_model_covers_every_student(cursor))
        results.append(check_model_is_not_stale())

        if not deploy_only:
            results.append(check_no_stray_identities(cursor))
            results.append(check_every_student_is_in_a_class(cursor))
            results.append(check_demo_content(cursor))

            if subject_id is not None:
                results.append(check_subject_schedule(cursor, subject_id))

    if cameras and not deploy_only:
        results.append(check_cameras())

    return results


def report(results):
    """Print the results and return the process exit status."""
    width = max(len(result.name) for result in results)

    print()
    print("PREFLIGHT")
    print("-" * (width + 40))

    for result in results:
        print(f"  [{result.status}] {result.name.ljust(width)}  {result.detail}")

    failures = [result for result in results if result.failed]

    print("-" * (width + 40))

    if not failures:
        print(f"  {len(results)} check(s) passed.")
        print()
        return 0

    print(f"  {len(failures)} of {len(results)} check(s) FAILED.")
    print()

    for result in failures:
        print(f"  {result.name}:")
        print(f"    {result.fix}")
        print()

    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only readiness checks for a deployed machine."
    )
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="Only the two checks a version update must not pass without.",
    )
    parser.add_argument(
        "--subject",
        type=int,
        default=None,
        help="Also check that this subject's scheduled start has not passed.",
    )
    parser.add_argument(
        "--cameras",
        action="store_true",
        help="Also scan for a live camera. Slow, and opens the device.",
    )

    args = parser.parse_args(argv)

    results = run_checks(
        deploy_only=args.deploy,
        subject_id=args.subject,
        cameras=args.cameras,
    )

    return report(results)


if __name__ == "__main__":
    sys.exit(main())
