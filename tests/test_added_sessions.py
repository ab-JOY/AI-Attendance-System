"""
Added capture sessions (FS-20): the same student under another light, or with
or without glasses, without growing the model.

Three layers, tested where each decision is made:

- **The slot rule** (`vision/enrolment.py`): session k takes the slots divisible
  by k, so after k sessions each holds about 100/k, every stage keeps its
  count, and each slot is retaken under its own stage's instruction.
- **The merge** (`infra/dataset_store.py`): kept images are copied into staging,
  the manifest records the new session, and the student's current images are
  never touched until the existing promotion swaps the folder.
- **The routes** (`web/enrolment.py`): the session number comes from the
  dataset folder, never from the request (lessons.md L28).

No student identifier is real (lessons.md L6), and every test is pointed at
tmp_path rather than the configured dataset directory.
"""

from __future__ import annotations

import contextlib
import json
from collections import Counter

import numpy as np
import pytest

from config.settings import settings
from infra import dataset_store
from security.paths import (
    student_dataset_path,
    student_image_path,
    student_staging_path,
)
from vision.enrolment import (
    CONDITIONS,
    DEFAULT_PLAN,
    MAX_IMAGES,
    MAX_SESSIONS,
    AddedSession,
    EnrolmentHooks,
    EnrolmentSession,
    added_session_slots,
    plan_for_slots,
)

STUDENT = "SEC-TEST-NOBODY"
NAME = "Test Student"
TODAY = "2026-10-20"


# ==============================
# The slot rule
# ==============================


def session_slots(number):
    """The slots session `number` captures, for a student who has had every earlier one."""
    return added_session_slots(owners_after(number - 1))


def owners_after(sessions):
    """Which session owns each slot after `sessions` sessions, by the rule."""
    owners = [1] * MAX_IMAGES

    for _ in range(2, sessions + 1):
        number = max(owners) + 1

        for slot in added_session_slots(owners):
            owners[slot - 1] = number

    return owners


@pytest.mark.parametrize(
    ("number", "expected"),
    [(2, 50), (3, 34), (4, 24)],
)
def test_an_added_session_captures_only_the_slots_it_keeps(number, expected):
    assert len(session_slots(number)) == expected


def test_a_session_past_the_cap_is_refused():
    with pytest.raises(ValueError):
        added_session_slots(owners_after(MAX_SESSIONS))


@pytest.mark.parametrize(
    ("sessions", "shares"),
    [
        (2, {1: 50, 2: 50}),
        (3, {1: 33, 2: 33, 3: 34}),
        (4, {1: 25, 2: 25, 3: 26, 4: 24}),
    ],
)
def test_sessions_share_the_hundred_evenly(sessions, shares):
    """
    The case the first design failed: "every slot divisible by k" left four
    sessions at 33/17/25/25, because the multiples of four are all even.
    """
    assert Counter(owners_after(sessions)) == shares


@pytest.mark.parametrize("sessions", [2, 3, 4])
def test_every_session_appears_in_every_pose_stage(sessions):
    """
    A session missing from a pose would leave that pose under one condition.

    The three distance stages hold five images each, too few to promise four
    sessions a place in every one, so they are held to two sessions.
    """
    owners = owners_after(sessions)
    by_stage = {}

    for slot, owner in enumerate(owners, start=1):
        by_stage.setdefault(DEFAULT_PLAN.stage_for(slot - 1).name, set()).add(owner)

    for stage in DEFAULT_PLAN.stages:
        if stage.images >= 10:
            assert by_stage[stage.name] == set(range(1, sessions + 1)), stage.name
        else:
            assert len(by_stage[stage.name]) >= min(sessions, 2), stage.name


@pytest.mark.parametrize("number", [2, 3, 4])
def test_each_slot_is_retaken_under_its_own_stage(number):
    """The nth image of the session is asked for with the stage its slot belongs to."""
    slots = session_slots(number)
    plan = plan_for_slots(slots)

    assert plan.total == len(slots)

    for position, slot in enumerate(slots):
        assert plan.stage_for(position).name == DEFAULT_PLAN.stage_for(slot - 1).name


def test_the_shortened_plan_keeps_every_instruction():
    plan = plan_for_slots(session_slots(2))

    assert [stage.instruction for stage in plan.stages] == [
        stage.instruction for stage in DEFAULT_PLAN.stages
    ]


def test_a_session_saves_into_its_own_slots():
    """The capture count is not the file name for an added session."""
    from tests.test_vision_enrolment import Clock, build_face, canonical_frame

    clock = Clock()
    saved = []
    slots = session_slots(2)
    rng = np.random.default_rng(1234)

    session = EnrolmentSession(
        student_id=STUDENT,
        student_name=NAME,
        plan=plan_for_slots(slots),
        slots=slots,
        hooks=EnrolmentHooks(
            detect=lambda frame: [build_face()],
            align=lambda frame, landmarks: rng.integers(40, 210, size=(200, 200), dtype=np.uint8),
            save=lambda slot, image: saved.append(slot),
            now=clock,
        ),
    )

    while len(saved) < 4:
        clock.advance()
        session.offer_frame(canonical_frame())

    assert saved == [2, 4, 6, 8]


def test_slots_that_do_not_match_the_plan_are_refused():
    with pytest.raises(ValueError):
        EnrolmentSession(
            student_id=STUDENT,
            student_name=NAME,
            plan=DEFAULT_PLAN,
            slots=session_slots(2),
            hooks=EnrolmentHooks(detect=None, align=None, save=None),
        )


# ==============================
# The merge
# ==============================


@pytest.fixture
def store(tmp_path, monkeypatch):
    dataset = tmp_path / "dataset"
    staging = tmp_path / "dataset_staging"
    dataset.mkdir()
    staging.mkdir()

    monkeypatch.setattr(settings, "dataset_dir", dataset)
    monkeypatch.setattr(settings, "dataset_staging_dir", staging)

    return dataset, staging


def marked(session_number):
    """A flat image whose pixel value says which session took it."""
    return np.full((200, 200), 40 * session_number, dtype=np.uint8)


def session_of(path):
    """Read the mark back. JPEG moves a flat value by a level or two at most."""
    import cv2

    return round(float(cv2.imread(str(path), cv2.IMREAD_GRAYSCALE).mean()) / 40)


def enrol_first_session():
    staging = dataset_store.begin(STUDENT)

    for slot in range(1, MAX_IMAGES + 1):
        dataset_store.save_image(staging, slot, MAX_IMAGES, marked(1))

    dataset_store.promote(STUDENT, expected=MAX_IMAGES)


def add_session(number, condition="lighting"):
    staging = dataset_store.begin(STUDENT)
    slots = session_slots(number)

    for slot in slots:
        dataset_store.save_image(staging, slot, MAX_IMAGES, marked(number))

    dataset_store.merge_session(
        STUDENT, number=number, condition=condition, slots=slots,
        total=MAX_IMAGES, captured_on=TODAY,
    )
    dataset_store.promote(STUDENT, expected=MAX_IMAGES)


def on_disk():
    folder = student_dataset_path(STUDENT)
    return [
        session_of(student_image_path(folder, slot, MAX_IMAGES))
        for slot in range(1, MAX_IMAGES + 1)
    ]


def test_a_folder_with_no_manifest_is_one_session(store):
    enrol_first_session()

    manifest = dataset_store.read_manifest(STUDENT, MAX_IMAGES)

    assert len(manifest["sessions"]) == 1
    assert manifest["slots"] == [1] * MAX_IMAGES


def test_an_incomplete_dataset_cannot_take_a_session(store):
    staging = dataset_store.begin(STUDENT)
    dataset_store.save_image(staging, 1, MAX_IMAGES, marked(1))
    student_dataset_path(STUDENT).mkdir()

    with pytest.raises(dataset_store.DatasetStoreError):
        dataset_store.read_manifest(STUDENT, MAX_IMAGES)


@pytest.mark.parametrize("sessions", [2, 3, 4])
def test_merged_images_come_from_the_session_the_rule_names(store, sessions):
    enrol_first_session()

    for number in range(2, sessions + 1):
        add_session(number)

    expected = owners_after(sessions)

    assert dataset_store.count_images(student_dataset_path(STUDENT)) == MAX_IMAGES
    assert on_disk() == expected

    manifest = dataset_store.read_manifest(STUDENT, MAX_IMAGES)
    assert manifest["slots"] == expected
    assert [s["number"] for s in manifest["sessions"]] == list(range(1, sessions + 1))


def test_the_manifest_records_the_condition_and_date(store):
    enrol_first_session()
    add_session(2, condition="glasses")

    added = dataset_store.read_manifest(STUDENT, MAX_IMAGES)["sessions"][1]

    assert added == {"number": 2, "condition": "glasses", "captured_on": TODAY}


def test_a_session_out_of_order_is_refused(store):
    enrol_first_session()

    staging = dataset_store.begin(STUDENT)
    slots = session_slots(3)
    for slot in slots:
        dataset_store.save_image(staging, slot, MAX_IMAGES, marked(3))

    with pytest.raises(dataset_store.DatasetStoreError):
        dataset_store.merge_session(
            STUDENT, number=3, condition="lighting", slots=slots,
            total=MAX_IMAGES, captured_on=TODAY,
        )


def test_a_session_missing_an_image_is_refused(store):
    enrol_first_session()

    staging = dataset_store.begin(STUDENT)
    slots = session_slots(2)
    for slot in slots[:-1]:
        dataset_store.save_image(staging, slot, MAX_IMAGES, marked(2))

    with pytest.raises(dataset_store.DatasetStoreError):
        dataset_store.merge_session(
            STUDENT, number=2, condition="lighting", slots=slots,
            total=MAX_IMAGES, captured_on=TODAY,
        )


def test_a_failed_merge_leaves_the_current_images_alone(store):
    """FS-15 for added sessions: nothing in dataset/ moves until promotion."""
    enrol_first_session()

    staging = dataset_store.begin(STUDENT)
    slots = session_slots(2)
    for slot in slots[:10]:
        dataset_store.save_image(staging, slot, MAX_IMAGES, marked(2))

    with pytest.raises(dataset_store.DatasetStoreError):
        dataset_store.merge_session(
            STUDENT, number=2, condition="lighting", slots=slots,
            total=MAX_IMAGES, captured_on=TODAY,
        )

    assert on_disk() == [1] * MAX_IMAGES
    assert not (student_dataset_path(STUDENT) / dataset_store.MANIFEST_NAME).exists()


def test_a_recapture_resets_to_one_session(store):
    """Promotion replaces the whole folder, so the old manifest goes with it."""
    enrol_first_session()
    add_session(2)

    enrol_first_session()

    assert len(dataset_store.read_manifest(STUDENT, MAX_IMAGES)["sessions"]) == 1
    assert on_disk() == [1] * MAX_IMAGES


@pytest.mark.parametrize(
    "contents",
    [
        "not json",
        json.dumps({"sessions": [{"number": 1}], "slots": [1] * 99}),
        json.dumps({"sessions": [{"number": 2}], "slots": [2] * MAX_IMAGES}),
        json.dumps({"sessions": [{"number": 1}], "slots": [7] * MAX_IMAGES}),
    ],
)
def test_a_damaged_manifest_is_refused(store, contents):
    enrol_first_session()
    (student_dataset_path(STUDENT) / dataset_store.MANIFEST_NAME).write_text(contents)

    with pytest.raises(dataset_store.DatasetStoreError):
        dataset_store.read_manifest(STUDENT, MAX_IMAGES)


@pytest.mark.slow
def test_training_reads_the_images_and_ignores_the_manifest(store, monkeypatch, caplog):
    import train_model

    enrol_first_session()
    add_session(2)

    dataset, _ = store
    trainer = dataset.parent / "trainer"
    trainer.mkdir()

    monkeypatch.setattr(train_model, "DATASET_DIR", str(dataset))
    monkeypatch.setattr(train_model, "TRAINER_DIR", str(trainer))
    monkeypatch.setattr(train_model, "MODELS_DIR", str(trainer / "students"))
    monkeypatch.setattr(train_model, "LABELS_FILE", str(trainer / "labels.txt"))

    with caplog.at_level("INFO"):
        success, _ = train_model.train_model()

    assert success is True
    assert "usable images: 100, skipped: 0" in caplog.text


# ==============================
# The routes
# ==============================


@pytest.fixture(scope="module")
def flask_app():
    import app as app_module

    app_module.app.config["TESTING"] = True
    return app_module.app


@pytest.fixture
def client(flask_app):
    flask_app.config["WTF_CSRF_ENABLED"] = False
    yield flask_app.test_client()
    flask_app.config["WTF_CSRF_ENABLED"] = True


@pytest.fixture
def no_running_enrolment():
    import web.enrolment as enrolment_module

    yield enrolment_module

    enrolment_module.enrolment_slot.abandon()


@contextlib.contextmanager
def fake_cursor(*_args, **_kwargs):
    yield object()


@pytest.fixture
def school(monkeypatch, store, no_running_enrolment):
    """One existing student, in one class, and a retrain that does nothing."""
    module = no_running_enrolment

    monkeypatch.setattr(module, "db_cursor", fake_cursor)
    monkeypatch.setattr(module.students_repo, "exists", lambda _c, sid: sid == STUDENT)
    monkeypatch.setattr(
        module.students_repo, "identity",
        lambda _c, sid: {"student_id": sid, "name": NAME} if sid == STUDENT else None,
    )
    monkeypatch.setattr(module.students_repo, "rename", lambda *_a: None)
    monkeypatch.setattr(module.enrolments_repo, "count_for_student", lambda *_a: 1)
    monkeypatch.setattr(module.training_job, "start", lambda **_k: None)

    return module


def sign_in(client):
    with client.session_transaction() as session:
        session["user"] = "test-admin"
        session["role"] = "admin"


def start(client, **extra):
    return client.post(
        "/enrol/start",
        json={"student_id": STUDENT, "student_name": NAME, "record": {}, **extra},
    )


def complete_the_capture(module, number):
    """Stand in for the camera: write the session's images and mark it done."""
    capture = module.enrolment_slot.current()
    staging = dataset_store.begin(STUDENT)

    for slot in session_slots(number):
        dataset_store.save_image(staging, slot, MAX_IMAGES, marked(number))

    capture._captured = capture.plan.total


@pytest.mark.parametrize("condition", [None, "", "sunglasses", 3, ["lighting"]])
def test_an_added_session_must_say_what_is_different(client, school, condition):
    enrol_first_session()
    sign_in(client)

    response = start(client, mode="add_session", condition=condition)

    assert response.status_code == 400
    assert school.enrolment_slot.current() is None


def test_a_student_with_no_images_cannot_take_a_session(client, school):
    sign_in(client)

    response = start(client, mode="add_session", condition="lighting")

    assert response.status_code == 409
    assert school.enrolment_slot.current() is None


def test_an_added_session_captures_its_share(client, school):
    enrol_first_session()
    sign_in(client)

    response = start(client, mode="add_session", condition="lighting")

    assert response.status_code == 201
    assert response.get_json()["progress"]["total"] == 50
    assert school.enrolment_slot.current().added_session == AddedSession(
        2, "lighting", session_slots(2)
    )


def test_the_client_cannot_choose_the_session_number(client, school):
    """The number decides which images are replaced, so it is the server's."""
    enrol_first_session()
    sign_in(client)

    response = start(
        client, mode="add_session", condition="lighting",
        number=4, session_number=4, slots=list(range(1, 101)),
    )

    assert response.status_code == 201
    capture = school.enrolment_slot.current()
    assert capture.added_session.number == 2
    assert capture.slots == session_slots(2)


def test_a_student_at_the_cap_is_sent_to_recapture(client, school):
    enrol_first_session()
    for number in range(2, MAX_SESSIONS + 1):
        add_session(number)
    sign_in(client)

    response = start(client, mode="add_session", condition="lighting")

    assert response.status_code == 409
    assert "Recapture" in response.get_json()["message"]


def test_finishing_an_added_session_merges_rather_than_replaces(client, school):
    enrol_first_session()
    sign_in(client)

    assert start(client, mode="add_session", condition="glasses").status_code == 201
    complete_the_capture(school, 2)

    response = client.post("/enrol/finish")

    assert response.status_code == 200, response.get_json()
    assert on_disk() == owners_after(2)
    assert dataset_store.read_manifest(STUDENT, MAX_IMAGES)["sessions"][1]["condition"] == "glasses"


def test_an_added_session_is_refused_if_the_images_changed_meanwhile(client, school):
    """
    Another session finishing between Start and Finish moves the session count,
    so this one's number and slots no longer follow. `merge_session()` refuses
    it, and the images that landed meanwhile are kept.
    """
    enrol_first_session()
    sign_in(client)

    assert start(client, mode="add_session", condition="lighting").status_code == 201
    complete_the_capture(school, 2)

    # Another session lands through another route while this one runs.
    capture = school.enrolment_slot.registry.finish()
    add_session(2, condition="room")
    school.enrolment_slot.registry.start(capture)
    complete_the_capture(school, 2)

    response = client.post("/enrol/finish")

    assert response.status_code == 500
    assert on_disk() == owners_after(2)
    sessions = dataset_store.read_manifest(STUDENT, MAX_IMAGES)["sessions"]
    assert [s["condition"] for s in sessions] == [None, "room"]


def test_finishing_a_recapture_still_replaces_everything(client, school):
    enrol_first_session()
    add_session(2)
    sign_in(client)

    assert start(client).status_code == 201
    capture = school.enrolment_slot.current()
    assert capture.added_session is None
    assert capture.plan.total == MAX_IMAGES

    staging = student_staging_path(STUDENT)
    for slot in range(1, MAX_IMAGES + 1):
        dataset_store.save_image(staging, slot, MAX_IMAGES, marked(3))
    capture._captured = MAX_IMAGES

    assert client.post("/enrol/finish").status_code == 200
    assert on_disk() == [3] * MAX_IMAGES
    assert len(dataset_store.read_manifest(STUDENT, MAX_IMAGES)["sessions"]) == 1


def test_the_capture_page_offers_every_condition(client, school):
    enrol_first_session()
    sign_in(client)

    response = client.post("/enrol", data={"mode": "add_session", "student_id": STUDENT})
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Session <strong>2</strong>" in page
    for key, label in CONDITIONS.items():
        assert f'value="{key}"' in page
        assert label in page
