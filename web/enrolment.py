"""
Browser enrolment and model retraining (CO-3, MA-2, FS-9, PE-5).

The replacement for `/capture_face`'s subprocess and its server-side OpenCV
window. The window opened on the *server desktop*, so an operator whose browser
was anywhere else saw nothing and the request hung until somebody walked over
and pressed ESC. That was CO-3, and it dragged CO-2 and MA-2 with it. The
decision to move enrolment into the browser is todo.md §7 Q2; Phase 5 deleted
the old path entirely.

**The browser supplies a camera and a screen. Nothing else moves.** Every
quality judgement - geometry, framing, pose, expression, blur, brightness -
runs on the server, on the same code the capture window used, through
`vision/enrolment.py`. Reimplementing the gates in JavaScript would recreate
MA-4, which Phase 3 existed to delete.

The flow, and why it is five routes rather than one:

    POST /enrol         -> renders the capture page (a normal form post)
    POST /enrol/start   -> opens the server-side session and staging folder
    POST /enrol/frame   -> one JPEG in, one verdict out, ~5 times a second
    POST /enrol/finish  -> promote the folder, THEN write the student row
    POST /enrol/cancel  -> throw the staging folder away

⚠️ getUserMedia needs a secure context. `http://localhost` counts, so enrolment
works on the server machine as shipped. Enrolling from *another* machine's
browser needs TLS configured - see `config/settings.py`.
"""

from __future__ import annotations

import logging

import mysql.connector
from flask import (
    Blueprint,
    flash,
    jsonify,
    render_template,
    request,
    session,
)

from infra import dataset_store
from infra.db import db_cursor
from infra.uploads import UploadRejected, decode_frame
from repositories import enrolments as enrolments_repo
from repositories import students as students_repo
from repositories import subjects as subjects_repo
from security.access import authenticated, json_api, role_required
from security.passwords import PasswordTooLongError, hash_password
from security.paths import (
    UnsafeStudentPathError,
    validate_student_id,
    validate_student_name,
)
from services.enrolment import DetectorClosed, EnrolmentSlot
from services.training import training_job
from vision.enrolment import DEFAULT_PLAN, MAX_IMAGES, EnrolmentComplete
from vision.pose import MIN_NATIVE_FRAME_WIDTH
from web.errors import error_page

logger = logging.getLogger(__name__)

enrolment_bp = Blueprint("enrolment", __name__)

# One capture at a time, process-wide. The slot owns the MediaPipe detector and
# the staging folder alongside the session, so cancelling, finishing and
# sweeping an abandoned capture are one thing rather than three call sites that
# each have to remember all three.
enrolment_slot = EnrolmentSlot()


# The key `record` carries the chosen class list under (US-10).
#
# ⚠️ `record` is deliberately opaque to `vision/` and `services/` - the capture
# session hands it back untouched at the end, which is what lets the database
# row be written *after* the images are safely on disk (FS-9). Putting the
# subject ids inside it means the choice survives the whole capture with no new
# plumbing through three layers. `enrol_finish()` pops it before the record
# reaches `students_repo.insert()`, so it never has to be a column.
SUBJECTS_KEY = "subject_ids"
PASSWORD_TOKEN_KEY = "password_token"


def _chosen_subject_ids(form):
    """
    The subjects ticked on the registration form, as ints.

    Non-numeric values are dropped rather than refused: this is a multi-select
    the browser fills in, so anything else is tampering rather than a typo, and
    the ids are checked against the `subjects` table before anything is written
    anyway. What *is* refused, later and loudly, is a subject that no longer
    exists.
    """
    return [
        int(value) for value in form.getlist(SUBJECTS_KEY)
        if value.strip().isdigit()
    ]


def enrolment_json(message, status=400, **extra):
    """The refusal envelope every JSON route in this blueprint uses."""
    body = {"success": False, "message": message}
    body.update(extra)
    return jsonify(body), status


@enrolment_bp.route('/enrol', methods=['POST'])
@role_required('admin')
def enrol():
    """
    Render the capture page for a new student or a recapture.

    Nothing is written here - not the student row, not a folder. The page opens
    the session itself, so a page that is loaded and then closed costs nothing.
    """
    mode = request.form.get('mode', 'new')

    try:
        student_id = validate_student_id(request.form.get('student_id', ''))
    except UnsafeStudentPathError as error:
        logger.warning("Rejected enrolment request: %s", error)
        return error_page(400, str(error))

    password_token = None

    if mode == 'recapture':
        with db_cursor(dictionary=True) as cursor:
            student = students_repo.identity(cursor, student_id)

        if student is None:
            return error_page(404, "That student ID was not found.")

        name = student['name']
        record = {}

    else:
        try:
            name = validate_student_name(request.form.get('name', ''))
        except UnsafeStudentPathError as error:
            logger.warning("Rejected enrolment request: %s", error)
            return error_page(400, str(error))

        with db_cursor(dictionary=True) as cursor:
            if students_repo.exists(cursor, student_id):
                return error_page(
                    409,
                    "A student with that ID is already enrolled. Use "
                    "Recapture Face from Manage Students instead.",
                )

        record = {
            'college_department': request.form.get('college_department', ''),
            'program': request.form.get('program', ''),
            'year_level': request.form.get('year_level', ''),
            'section': request.form.get('section', ''),
            # US-10. Nothing is written here - this rides along in the opaque
            # record and is applied at /enrol/finish, with the student row, in
            # one transaction.
            SUBJECTS_KEY: _chosen_subject_ids(request.form),
        }

        password = request.form.get('password', '')

        if password:
            if len(password) < 8:
                return error_page(400, "Password must be at least 8 characters.")

            try:
                password_hash = hash_password(password)
            except PasswordTooLongError as error:
                return error_page(400, str(error))

            password_token = enrolment_slot.store_password(password_hash)

    return render_template(
        'enrol.html',
        student_id=student_id,
        student_name=name,
        mode=mode,
        record=record,
        password_token=password_token,
        plan=DEFAULT_PLAN.describe(),
        total=MAX_IMAGES,
        # Passed rather than repeated in the page: the server refuses frames
        # from a camera narrower than this, and a number duplicated in
        # JavaScript is a number that drifts.
        min_native_width=MIN_NATIVE_FRAME_WIDTH,
    )


@enrolment_bp.route('/enrol/start', methods=['POST'])
@role_required('admin')
@json_api
def enrol_start():
    payload = request.get_json(silent=True) or {}

    if enrolment_slot.current() is not None:
        return enrolment_json(
            "Another enrolment is already in progress. Finish or cancel it "
            "first.",
            status=409,
        )

    try:
        student_id = validate_student_id(payload.get('student_id'))
        student_name = validate_student_name(payload.get('student_name'))
    except UnsafeStudentPathError as error:
        return enrolment_json(str(error))

    try:
        password_hash = enrolment_slot.take_password(
            payload.get(PASSWORD_TOKEN_KEY)
        )

        start_arguments = {
            "student_id": student_id,
            "student_name": student_name,
            "started_by": session.get('user'),
            "record": payload.get('record') or {},
        }

        if password_hash is not None:
            start_arguments["password_hash"] = password_hash

        capture_session = enrolment_slot.start(
            **start_arguments,
        )
    except (UnsafeStudentPathError, OSError):
        logger.exception("Could not open an enrolment staging folder")
        return enrolment_json("Could not start the capture.", status=500)

    if capture_session is None:
        return enrolment_json(
            "Another enrolment is already in progress.", status=409
        )

    logger.info(
        "Enrolment started for %s by %s", student_id, session.get('user')
    )

    return jsonify({
        "success": True,
        "message": "Capture started.",
        "progress": capture_session.progress().as_dict(),
    }), 201


@enrolment_bp.route('/enrol/frame', methods=['POST'])
@role_required('admin')
@json_api
def enrol_frame():
    """One uploaded JPEG, judged by the same gates the capture window used."""
    capture_session = enrolment_slot.current()

    if capture_session is None:
        return enrolment_json(
            "No capture is in progress. Start one first.", status=409
        )

    try:
        frame = decode_frame(request.get_data(), request.content_type)
    except UploadRejected as error:
        # Safe to relay: infra/uploads.py's messages describe the picture, not
        # the server, and a test asserts they cannot leak a path or a library.
        return enrolment_json(str(error))

    try:
        verdict = capture_session.offer_frame(frame)
    except EnrolmentComplete:
        return jsonify({
            "success": True,
            "progress": capture_session.progress().as_dict(),
        })
    except DetectorClosed:
        # The capture was cancelled while this frame was in flight. That is the
        # same answer as arriving one moment later, and not a server fault.
        return enrolment_json("No capture is in progress.", status=409)
    except Exception:
        logger.exception("Enrolment frame could not be processed")
        return enrolment_json("That frame could not be processed.", status=500)

    return jsonify({"success": True, "progress": verdict.as_dict()})


@enrolment_bp.route('/enrol/finish', methods=['POST'])
@role_required('admin')
@json_api
def enrol_finish():
    """
    Promote the images, then write the student row, then retrain.

    ⚠️ **The order is the finding.** `/capture_face` inserted the row first, so
    a cancelled capture left a student with no dataset - FS-9, and the cause of
    the Phase 0 outage. Here the row cannot exist unless a hundred images
    already do.
    """
    capture_session = enrolment_slot.current()

    if capture_session is None:
        return enrolment_json("No capture is in progress.", status=409)

    if not capture_session.done:
        return enrolment_json(
            f"Only {capture_session.captured} of {MAX_IMAGES} images have "
            "been captured.",
            status=409,
            progress=capture_session.progress().as_dict(),
        )

    student_id = capture_session.student_id
    student_name = capture_session.student_name

    try:
        dataset_store.promote(student_id, expected=MAX_IMAGES)
    except (dataset_store.DatasetStoreError, UnsafeStudentPathError, OSError):
        logger.exception("Could not promote the enrolment for %s", student_id)
        enrolment_slot.abandon()
        return enrolment_json(
            "The captured images could not be saved. Nothing was changed.",
            status=500,
        )

    # ⚠️ Popped, not read: `record` is passed straight to the INSERT, and the
    # class list is a relation rather than a column on `students`.
    record = dict(capture_session.record)
    chosen_subject_ids = record.pop(SUBJECTS_KEY, []) or []

    password_hash = getattr(capture_session, "password_hash", None)

    if password_hash is not None:
        record['password_hash'] = password_hash

    try:
        with db_cursor(dictionary=True, commit=True) as cursor:
            if students_repo.exists(cursor, student_id):
                # A recapture. The images are new; the record is not ours to
                # rewrite here - and neither is the class list. Recapture
                # replaces a face, not a timetable.
                students_repo.rename(cursor, student_id, student_name)
            else:
                students_repo.insert(cursor, student_id, student_name, record)

                if chosen_subject_ids:
                    # ⚠️ **One transaction with the row above** (US-10). A
                    # student who committed without their class list is
                    # recognised by the model and refused by the register,
                    # which is the whole finding; a partial write must not be
                    # able to produce it.
                    live = subjects_repo.existing_ids(
                        cursor, chosen_subject_ids
                    )

                    enrolments_repo.enrol_many(cursor, student_id, live)

                    missing = set(chosen_subject_ids) - set(live)

                    if missing:
                        # Deleted between the form and the end of the capture.
                        # Not fatal - the student and the classes that do exist
                        # are still correct - but it must not pass silently.
                        logger.warning(
                            "Enrolled %s into %d of %d chosen subjects; %s no "
                            "longer exist",
                            student_id, len(live), len(chosen_subject_ids),
                            sorted(missing),
                        )

            classes = enrolments_repo.count_for_student(cursor, student_id)

    except mysql.connector.Error:
        logger.exception("Enrolment images saved but the student row failed")
        enrolment_slot.release()
        return enrolment_json(
            "The images were saved but the student record could not be "
            "written. The images are on disk; retry from Manage Students.",
            status=500,
        )

    enrolment_slot.release()

    logger.info("Enrolment complete for %s; starting a retrain", student_id)
    training_job.start(started_by=session.get('user'))

    # ⚠️ **flash(), not the JSON message** (US-10). The page shows the message
    # below for about a second and a half and then navigates to Manage
    # Students, so a warning that matters is a warning nobody finishes reading.
    # A flash survives the redirect and is rendered by base.html on the page
    # they land on.
    if classes:
        flash(
            f"{student_name} was enrolled and added to {classes} "
            f"class{'es' if classes != 1 else ''}.",
            "success",
        )
    else:
        # The finding, said before the camera says it. A student with no class
        # list is recognised by the model and refused by the register, and
        # until this message existed the first anybody heard of it was "Not
        # Enrolled" on the attendance overlay.
        flash(
            f"{student_name} was enrolled for recognition but is not in any "
            "class yet, so attendance cannot be recorded for them. Add them "
            "to a class from Subjects → Class List.",
            "warning",
        )

    return jsonify({
        "success": True,
        "message": (
            "Enrolment complete. The model is being rebuilt; this student is "
            "recognised once it finishes."
        ),
    })


@enrolment_bp.route('/enrol/cancel', methods=['POST'])
@role_required('admin')
@json_api
def enrol_cancel():
    """
    Throw away an in-progress capture.

    Cancelling is free and leaves nothing behind: no student row was ever
    written, and the images only ever existed under `dataset_staging/`.
    """
    capture_session = enrolment_slot.current()

    if capture_session is None:
        return jsonify({"success": True, "message": "Nothing to cancel."})

    logger.info(
        "Enrolment cancelled for %s after %d image(s)",
        capture_session.student_id,
        capture_session.captured,
    )

    enrolment_slot.abandon()

    return jsonify({"success": True, "message": "Capture cancelled."})


@enrolment_bp.route('/enrol/status')
@authenticated
@json_api
def enrol_status():
    """Whether a capture is running, for a page that was reloaded."""
    capture_session = enrolment_slot.current()

    if capture_session is None:
        return jsonify({"running": False})

    return jsonify({
        "running": True,
        "student_id": capture_session.student_id,
        "started_by": capture_session.started_by,
        "progress": capture_session.progress().as_dict(),
    })


# ==========================================================
# MODEL RETRAINING (PE-5, US-2)
#
# This used to call train_model() inline and block the request until it
# finished. Phase 0 took training from minutes to roughly twenty seconds at
# three students, but the shape was still wrong: the browser has no way to show
# progress for a request that has not answered, any proxy in front of the app
# will time it out, and the cost grows with enrolment.
#
# The route starts a job and answers 202 immediately. /train_status is what the
# page polls.
#
# ⚠️ Still admin-only. Opening retraining to instructors is a decision about
# who is allowed to change the biometric model, not an oversight to correct in
# passing.
# ==========================================================


@enrolment_bp.route('/train_model', methods=['POST'])
@role_required('admin')
@json_api
def train_model_route():
    started = training_job.start(started_by=session.get('user'))

    if not started:
        # 409, not an error page: the caller is JavaScript, and "already
        # running" is a normal thing for a double-clicked button to hit.
        return jsonify({
            "success": False,
            "started": False,
            "message": "Training is already running.",
            "status": training_job.status().as_dict(),
        }), 409

    return jsonify({
        "success": True,
        "started": True,
        "message": "Training started.",
        "status": training_job.status().as_dict(),
    }), 202


# @json_api as well as @authenticated, and both are load-bearing. Without the
# JSON marker the access-control hook answers an expired session with an HTML
# redirect to the login page, and the polling JavaScript parses that as JSON
# and fails with a syntax error rather than saying "please sign in again".
@enrolment_bp.route('/train_status')
@authenticated
@json_api
def train_status():
    return jsonify(training_job.status().as_dict())
