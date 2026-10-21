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
import secrets
from collections import Counter
from datetime import date

import mysql.connector
from flask import (
    Blueprint,
    flash,
    jsonify,
    render_template,
    request,
    session,
    url_for,
)

from infra import dataset_store
from infra.db import db_cursor
from infra.uploads import (
    UploadRejected,
    canonical_region,
    canonicalise,
    decode_native,
)
from repositories import academics as academics_repo
from repositories import enrolments as enrolments_repo
from repositories import students as students_repo
from repositories import subjects as subjects_repo
from security.access import (
    authenticated,
    instructor_scope,
    json_api,
    role_required,
)
from security.passwords import PasswordTooLongError, hash_password
from security.paths import (
    UnsafeStudentPathError,
    validate_student_id,
    validate_student_name,
)
from services.enrolment import DetectorClosed, EnrolmentSlot
from services.training import training_job
from vision.enrolment import (
    CONDITIONS,
    DEFAULT_PLAN,
    MAX_IMAGES,
    MAX_SESSIONS,
    AddedSession,
    EnrolmentComplete,
    added_session_slots,
    plan_for_slots,
)
from vision.pose import MIN_NATIVE_FRAME_WIDTH
from web.errors import error_page
from web.placement import parse_placement, section_id_for
from web.students import class_groups

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


# ==========================================================
# INSTRUCTOR CAPTURE
#
# An instructor captures the faces of their own classes with exactly the
# administrator's flow - the same routes, page, gates and retrain. What differs
# is who they may capture, and that is decided here rather than in the page:
#
#   - a student who already exists must be on one of their class lists, since
#     finishing a capture replaces that student's images (and their name);
#   - a new student must be placed in at least one class they teach, so they
#     cannot add anyone they will never take attendance for.
#
# ⚠️ **Checked at /enrol/start, not only at /enrol.** /enrol renders a page;
# /enrol/start takes the student ID and class list as a request body, and that
# is the call a forged request would make.
# ==========================================================

# The Flask-session key holding the token of the capture this login started.
CAPTURE_OWNER_KEY = "enrolment_owner"
NOT_YOUR_CAPTURE = "This capture was started by someone else."


def _subject_ids_from(record):
    """The class list a record carries, as ints, or None if it is not one."""
    values = record.get(SUBJECTS_KEY, []) if isinstance(record, dict) else None

    if not isinstance(values, list) or not all(
        isinstance(value, int) and not isinstance(value, bool)
        for value in values
    ):
        return None

    return values


def _instructor_refusal(student_id, subject_ids):
    """
    Why the signed-in instructor may not capture this student, or None.

    Always None for an administrator, without touching the database.
    """
    instructor_id = instructor_scope()

    if instructor_id is None:
        return None

    with db_cursor(dictionary=True) as cursor:
        if students_repo.exists(cursor, student_id):
            if enrolments_repo.is_in_class_of(cursor, student_id, instructor_id):
                return None

            return "You can only capture the faces of students in your own classes."

        if not subject_ids:
            return "Choose at least one of your classes for the new student."

        if not all(
            subjects_repo.is_taught_by(cursor, subject_id, instructor_id)
            for subject_id in subject_ids
        ):
            return "You can only add a student to classes you teach."

    return None


def _owns_capture(capture_session):
    """
    Whether this login may send frames to, finish or cancel the capture.

    An administrator may act on any capture, as before. An instructor only on
    the one they started: the slot is shared by the whole server, and another
    instructor's half-finished capture is not theirs to throw away.
    """
    if session.get('role') == 'admin':
        return True

    token = getattr(capture_session, "owner_token", None)

    return token is not None and session.get(CAPTURE_OWNER_KEY) == token


ADD_SESSION = "add_session"


def _next_session(student_id):
    """
    `((number, slots), None)` for the student's next session, or `(None, why not)`.

    Worked out from the dataset folder, not from the request (FS-20): the slots
    are the images the new session replaces, so a client that could name them
    could overwrite any image it liked.
    """
    try:
        manifest = dataset_store.read_manifest(student_id, MAX_IMAGES)
    except (dataset_store.DatasetStoreError, UnsafeStudentPathError):
        logger.warning("No session can be added for %s", student_id, exc_info=True)
        return None, (
            "This student has no complete set of face images to add to. "
            "Use Recapture Face instead."
        )

    number = len(manifest["sessions"]) + 1

    if number > MAX_SESSIONS:
        return None, (
            f"This student already has {MAX_SESSIONS} capture sessions. Use "
            "Recapture Face to start again from one."
        )

    return (number, added_session_slots(manifest["slots"])), None


def _done_url():
    """Where the capture page goes when it is over, for this role."""
    if session.get('role') == 'admin':
        return url_for('students.manage_students')

    return url_for('enrolment.class_capture')


@enrolment_bp.route('/class_capture')
@role_required('instructor')
def class_capture():
    """
    The instructor's Face Capture page: their classes and who is in them.

    Recapture for a student already listed, and Register & Capture for a new
    one, both post to /enrol like the administrator's pages do.
    """
    instructor_id = instructor_scope()

    try:
        with db_cursor(dictionary=True) as cursor:
            offerings = subjects_repo.for_selection(
                cursor, instructor_id=instructor_id
            )

            classes = [
                {**offering, "students": enrolments_repo.enrolled_in(
                    cursor, offering["id"]
                )}
                for offering in offerings
            ]

            programs = academics_repo.programs(cursor)

    except mysql.connector.Error:
        logger.exception("Could not load the Face Capture page")
        return error_page(500)

    return render_template(
        'class_capture.html',
        classes=classes,
        class_groups=class_groups(offerings),
        programs=programs,
        year_levels=academics_repo.YEAR_LEVELS,
    )


@enrolment_bp.route('/enrol', methods=['POST'])
@role_required('admin', 'instructor')
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
    plan = DEFAULT_PLAN
    session_number = None

    if mode in ('recapture', ADD_SESSION):
        with db_cursor(dictionary=True) as cursor:
            student = students_repo.identity(cursor, student_id)

        if student is None:
            return error_page(404, "That student ID was not found.")

        refusal = _instructor_refusal(student_id, [])

        if refusal is not None:
            return error_page(403, refusal)

        name = student['name']
        record = {}

        if mode == ADD_SESSION:
            next_session, problem = _next_session(student_id)

            if problem is not None:
                return error_page(409, problem)

            session_number, slots = next_session
            plan = plan_for_slots(slots)

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

        # Checked before the capture rather than after it: a placement that
        # cannot be saved is cheaper to hear about now than at the end of a
        # hundred images.
        placement, problem = parse_placement(request.form)

        if problem is not None:
            return error_page(400, problem)

        chosen_subject_ids = _chosen_subject_ids(request.form)
        refusal = _instructor_refusal(student_id, chosen_subject_ids)

        if refusal is not None:
            return error_page(403, refusal)

        record = {
            **placement,
            # US-10. Nothing is written here - this rides along in the opaque
            # record and is applied at /enrol/finish, with the student row, in
            # one transaction.
            SUBJECTS_KEY: chosen_subject_ids,
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
        plan=plan.describe(),
        total=plan.total,
        session_number=session_number,
        max_sessions=MAX_SESSIONS,
        conditions=CONDITIONS,
        # Passed rather than repeated in the page: the server refuses frames
        # from a camera narrower than this, and a number duplicated in
        # JavaScript is a number that drifts.
        min_native_width=MIN_NATIVE_FRAME_WIDTH,
        done_url=_done_url(),
    )


@enrolment_bp.route('/enrol/start', methods=['POST'])
@role_required('admin', 'instructor')
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

    record = payload.get('record') or {}

    if session.get('role') != 'admin':
        subject_ids = _subject_ids_from(record)

        if subject_ids is None:
            return enrolment_json("The class list could not be read.")

        refusal = _instructor_refusal(student_id, subject_ids)

        if refusal is not None:
            return enrolment_json(refusal, status=403)

    added_session = None

    if payload.get('mode') == ADD_SESSION:
        condition = payload.get('condition')

        if not isinstance(condition, str) or condition not in CONDITIONS:
            return enrolment_json("Choose what is different about this session.")

        next_session, problem = _next_session(student_id)

        if problem is not None:
            return enrolment_json(problem, status=409)

        number, slots = next_session
        added_session = AddedSession(number=number, condition=condition, slots=slots)

    try:
        password_hash = enrolment_slot.take_password(
            payload.get(PASSWORD_TOKEN_KEY)
        )

        start_arguments = {
            "student_id": student_id,
            "student_name": student_name,
            "started_by": session.get('user'),
            "record": record,
        }

        if password_hash is not None:
            start_arguments["password_hash"] = password_hash

        if added_session is not None:
            start_arguments["added_session"] = added_session

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

    capture_session.owner_token = secrets.token_urlsafe(16)
    session[CAPTURE_OWNER_KEY] = capture_session.owner_token

    logger.info(
        "Enrolment started for %s by %s", student_id, session.get('user')
    )

    return jsonify({
        "success": True,
        "message": "Capture started.",
        "progress": capture_session.progress().as_dict(),
    }), 201


def _note_upload(capture_session, width, size):
    """
    Count the width and size of one upload against its capture.

    The capture page sends smaller frames when replies are slow (a Cloudflare
    tunnel, a phone on mobile data), down to MIN_NATIVE_FRAME_WIDTH. The
    saved crops still pass every quality gate, but a crop cut from a 960-wide
    frame has less detail than one from 1920, and an accuracy figure has to be
    able to say which captures were which. Read from the decoded frame, so it
    is what arrived rather than what the browser claims.
    """
    uploads = getattr(capture_session, "uploads", None)

    if uploads is None:
        uploads = capture_session.uploads = {"widths": Counter(), "bytes": 0}

    uploads["widths"][width] += 1
    uploads["bytes"] += size


# Set by static/js/enrol.js when it sent only the face and greyed out the rest:
# "x,y,width,height" in the upload's own pixels.
SENT_REGION_HEADER = "X-Face-Region"


def _sent_region(native):
    """
    The header's region on the canonical frame, None for a whole frame, or
    False if the header is present and unusable.

    ⚠️ Unusable is refused, not ignored: ignoring it would judge a mostly-grey
    frame as if all of it were real, which is the one thing the region exists
    to prevent.
    """
    raw = request.headers.get(SENT_REGION_HEADER)

    if raw is None:
        return None

    try:
        region = tuple(int(part) for part in raw.split(","))
    except ValueError:
        return False

    if len(region) != 4 or region[2] <= 0 or region[3] <= 0:
        return False

    height, width = native.shape[:2]

    return canonical_region(region, (width, height)) or False


def _describe_uploads(capture_session):
    """One log-line summary of what a capture was built from."""
    uploads = getattr(capture_session, "uploads", None)

    if not uploads:
        return "no frames"

    frames = sum(uploads["widths"].values())
    widths = ", ".join(
        f"{width}px x{count}"
        for width, count in sorted(uploads["widths"].items(), reverse=True)
    )

    return f"{frames} frames ({widths}), {uploads['bytes'] // frames // 1024} KB average"


@enrolment_bp.route('/enrol/frame', methods=['POST'])
@role_required('admin', 'instructor')
@json_api
def enrol_frame():
    """One uploaded JPEG, judged by the same gates the capture window used."""
    capture_session = enrolment_slot.current()

    if capture_session is None:
        return enrolment_json(
            "No capture is in progress. Start one first.", status=409
        )

    if not _owns_capture(capture_session):
        return enrolment_json(NOT_YOUR_CAPTURE, status=403)

    body = request.get_data()

    try:
        native = decode_native(body, request.content_type)
    except UploadRejected as error:
        # Safe to relay: infra/uploads.py's messages describe the picture, not
        # the server, and a test asserts they cannot leak a path or a library.
        return enrolment_json(str(error))

    _note_upload(capture_session, native.shape[1], len(body))

    sent_region = _sent_region(native)

    if sent_region is False:
        return enrolment_json("The face region of that frame could not be read.")

    try:
        verdict = capture_session.offer_frame(
            canonicalise(native), sent_region=sent_region
        )
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
@role_required('admin', 'instructor')
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

    if not _owns_capture(capture_session):
        return enrolment_json(NOT_YOUR_CAPTURE, status=403)

    if not capture_session.done:
        return enrolment_json(
            f"Only {capture_session.captured} of {capture_session.plan.total} "
            "images have been captured.",
            status=409,
            progress=capture_session.progress().as_dict(),
        )

    student_id = capture_session.student_id
    student_name = capture_session.student_name

    # ⚠️ Read from the server's session object, never from this request: an
    # added session merges into the current images, everything else replaces
    # them, and the client does not get to choose which (lessons.md L28).
    added_session = capture_session.added_session

    try:
        if added_session is not None:
            # merge_session() refuses if the student's session count moved
            # since Start (another route finished meanwhile). The slots follow
            # from the count alone, so that is the whole check.
            dataset_store.merge_session(
                student_id,
                number=added_session.number,
                condition=added_session.condition,
                slots=added_session.slots,
                total=MAX_IMAGES,
                captured_on=date.today().isoformat(),
            )

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

    # ⚠️ Parsed again, not trusted. `record` went to the browser as JSON and
    # came back with /enrol/start, so what arrives here is a request body. A
    # recapture carries `{}` and is not placed - see the branch below.
    placement, placement_problem = parse_placement(record)

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
                # ⚠️ **Resolved inside this transaction.** A section created
                # here and a student row that then failed would otherwise
                # leave an empty section behind.
                #
                # An unusable placement is not fatal at this point: the images
                # are already on disk, so the student is written Unassigned -
                # which the list says out loud - rather than the capture being
                # thrown away over a field that takes ten seconds to fix.
                section_id = (
                    section_id_for(cursor, placement)
                    if placement is not None else None
                )

                if section_id is None:
                    logger.warning(
                        "Enrolling %s with no section: %s",
                        student_id,
                        placement_problem or "the chosen program no longer exists",
                    )

                record['section_id'] = section_id

                students_repo.insert(cursor, student_id, student_name, record)

                if chosen_subject_ids:
                    # ⚠️ **One transaction with the row above** (US-10). A
                    # student who committed without their class list is
                    # recognised by the model and refused by the register,
                    # which is the whole finding; a partial write must not be
                    # able to produce it.
                    #
                    # DM-7. Only the subjects of the student's own program:
                    # the form shows no others, and this is what refuses one
                    # that was named anyway. An Unassigned student has no
                    # program to compare with and is not narrowed.
                    live = subjects_repo.existing_ids(
                        cursor,
                        chosen_subject_ids,
                        program_id=(
                            placement["program_id"]
                            if section_id is not None else None
                        ),
                    )

                    enrolments_repo.enrol_many(cursor, student_id, live)

                    missing = set(chosen_subject_ids) - set(live)

                    if missing:
                        # Deleted between the form and the end of the capture,
                        # or not offered to this program. Not fatal - the
                        # student and the classes that are theirs are still
                        # correct - but it must not pass silently.
                        logger.warning(
                            "Enrolled %s into %d of %d chosen subjects; %s no "
                            "longer exist or are not offered to their program",
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

    logger.info(
        "Enrolment complete for %s (%s) from %s; starting a retrain",
        student_id,
        (
            f"session {added_session.number}, {added_session.condition}"
            if added_session is not None else "full capture"
        ),
        _describe_uploads(capture_session),
    )
    training_job.start(started_by=session.get('user'))

    # ⚠️ **flash(), not the JSON message** (US-10). The page shows the message
    # below for about a second and a half and then navigates to Manage
    # Students, so a warning that matters is a warning nobody finishes reading.
    # A flash survives the redirect and is rendered by base.html on the page
    # they land on.
    if added_session is not None and classes:
        flash(
            f"Session {added_session.number} "
            f"({CONDITIONS[added_session.condition].lower()}) was added for "
            f"{student_name}. They are recognised with it once the model is rebuilt.",
            "success",
        )
    elif classes:
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
@role_required('admin', 'instructor')
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

    if not _owns_capture(capture_session):
        return enrolment_json(NOT_YOUR_CAPTURE, status=403)

    logger.info(
        "Enrolment cancelled for %s after %d image(s), from %s",
        capture_session.student_id,
        capture_session.captured,
        _describe_uploads(capture_session),
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
