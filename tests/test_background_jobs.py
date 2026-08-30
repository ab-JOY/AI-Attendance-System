"""
Training runs off the request thread, and says what it is doing (PE-5, US-2).

Two layers, because they fail differently:

* `BackgroundJob` itself - one run at a time, a status a poller can read, and
  a worker that cannot die silently. Fakes only, milliseconds.
* The `/train_model` and `/train_status` routes - the status codes, and the
  two access-control markers that the polling JavaScript depends on.

The route tests drive `app.test_client()` and need no database: training is
replaced by a fake, and nothing else on these two routes touches MySQL.
"""

from __future__ import annotations

import dataclasses
import threading
import time

import pytest

from infra.jobs import FAILED, IDLE, RUNNING, SUCCEEDED, BackgroundJob, JobStatus

# ---------------------------------------------------------------------------
# BackgroundJob
# ---------------------------------------------------------------------------


def instant(ok=True, message="done"):
    def runner(report):
        report("Working")
        return ok, message

    return runner


def blocking(release, ok=True, message="done"):
    def runner(report):
        report("Working")
        release.wait(5.0)
        return ok, message

    return runner


def test_a_fresh_job_is_idle():
    job = BackgroundJob(name="t", runner=instant())

    status = job.status()

    assert status.state == IDLE
    assert not status.is_running
    assert status.message is None
    assert status.elapsed_seconds() is None


def test_a_completed_run_reports_success_and_the_message():
    job = BackgroundJob(name="t", runner=instant(True, "Training completed."))

    assert job.start() is True
    assert job.wait()

    status = job.status()

    assert status.state == SUCCEEDED
    assert status.message == "Training completed."
    assert status.elapsed_seconds() >= 0


def test_a_failed_run_reports_the_failure():
    """
    train_model() returns `(False, reason)` for a dataset problem rather than
    raising - an under-populated folder, no usable images. The operator has to
    see that, not a page that says nothing happened.
    """
    job = BackgroundJob(name="t", runner=instant(False, "No training images."))

    job.start()
    assert job.wait()

    status = job.status()

    assert status.state == FAILED
    assert status.message == "No training images."


def test_a_runner_that_raises_does_not_leave_the_job_running_forever():
    """
    The failure mode that makes a polling UI worse than no UI: a worker thread
    dies, the status stays "running", and the page spins until someone reloads.
    """
    def explodes(report):
        raise RuntimeError("cv2 blew up")

    job = BackgroundJob(name="t", runner=explodes)

    job.start()
    assert job.wait()

    status = job.status()

    assert status.state == FAILED
    assert not status.is_running
    assert "RuntimeError" in status.message


def test_the_stage_is_visible_while_the_run_is_going():
    release = threading.Event()

    def runner(report):
        report("Reading images for 3 student(s)")
        release.wait(5.0)
        return True, "done"

    job = BackgroundJob(name="t", runner=runner)
    job.start()

    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if job.status().stage == "Reading images for 3 student(s)":
                break
            time.sleep(0.01)

        status = job.status()
        assert status.is_running
        assert status.stage == "Reading images for 3 student(s)"
        assert status.elapsed_seconds() >= 0
    finally:
        release.set()
        job.wait()


def test_a_second_run_is_refused_while_one_is_going():
    """
    Two concurrent trainings would both write trainer/trainer.yml.
    write_model_atomically() makes each write safe on its own, so neither
    would corrupt the file - the loser's model would just silently replace the
    winner's, which is worse than an error because nothing reports it.
    """
    release = threading.Event()
    job = BackgroundJob(name="t", runner=blocking(release))

    assert job.start() is True

    try:
        assert job.start() is False, "a second run was allowed to start"
        assert job.status().is_running
    finally:
        release.set()
        job.wait()


def test_the_job_can_be_run_again_after_it_finishes():
    calls = []

    def runner(report):
        calls.append(1)
        return True, "done"

    job = BackgroundJob(name="t", runner=runner)

    job.start()
    job.wait()
    assert job.start() is True
    job.wait()

    assert len(calls) == 2


def test_only_one_of_many_racing_starts_wins():
    """Two operators, or one double-clicked button, on different threads."""
    release = threading.Event()
    job = BackgroundJob(name="t", runner=blocking(release))

    started = []
    ready = threading.Barrier(8)

    def contend():
        ready.wait()
        started.append(job.start())

    threads = [threading.Thread(target=contend) for _ in range(8)]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    release.set()
    job.wait()

    assert sum(1 for s in started if s) == 1, (
        f"{sum(1 for s in started if s)} runs started at once"
    )


def test_who_started_it_is_recorded():
    """An audit trail for a change to the biometric model (RE-5)."""
    job = BackgroundJob(name="t", runner=instant())

    job.start(started_by="admin")
    job.wait()

    assert job.status().started_by == "admin"


def test_the_status_dict_is_json_serialisable_and_complete():
    job = BackgroundJob(name="t", runner=instant(True, "ok"))
    job.start(started_by="admin")
    job.wait()

    payload = job.status().as_dict()

    assert set(payload) == {
        "state",
        "stage",
        "message",
        "running",
        "elapsed_seconds",
        "started_by",
    }

    import json

    json.loads(json.dumps(payload))


def test_a_status_snapshot_does_not_change_underneath_the_caller():
    """
    Frozen and copied out, so a poll that took a snapshot cannot see it mutate
    between reading `state` and reading `message`.
    """
    release = threading.Event()
    job = BackgroundJob(name="t", runner=blocking(release))
    job.start()

    snapshot = job.status()
    release.set()
    job.wait()

    assert snapshot.is_running
    assert job.status().state == SUCCEEDED
    assert snapshot.state == RUNNING, "the snapshot changed after it was taken"

    # Frozen dataclass: a route handed a status cannot edit the job through it.
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.state = "tampered"


def test_elapsed_time_stops_when_the_run_does():
    job = BackgroundJob(name="t", runner=instant())
    job.start()
    job.wait()

    first = job.status().elapsed_seconds()
    time.sleep(0.15)
    second = job.status().elapsed_seconds()

    assert first == second, "elapsed time kept counting after the job finished"


def test_reset_refuses_while_running():
    release = threading.Event()
    job = BackgroundJob(name="t", runner=blocking(release))
    job.start()

    try:
        with pytest.raises(RuntimeError):
            job.reset()
    finally:
        release.set()
        job.wait()


def test_job_status_defaults_are_a_valid_idle_record():
    status = JobStatus()

    assert status.state == IDLE
    assert status.as_dict()["running"] is False


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


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
def fake_training(monkeypatch):
    """Replace the real trainer so no model is written and nothing is slow."""
    # The job singleton moved out of app.py in Phase 5: three places start it,
    # so it belongs to services/ rather than to whichever blueprint got there
    # first. Both the module attribute and the blueprint's reference to it are
    # replaced - the route reads `training_job` from its own namespace.
    import services.training as training_module
    import web.enrolment as enrolment_module

    release = threading.Event()
    release.set()

    def runner(report):
        report("Working")
        release.wait(5.0)
        return True, "Training completed successfully."

    job = BackgroundJob(name="test-train", runner=runner)
    monkeypatch.setattr(training_module, "training_job", job)
    monkeypatch.setattr(enrolment_module, "training_job", job)

    yield job, release

    release.set()
    job.wait()


def sign_in(client, role="admin"):
    with client.session_transaction() as session:
        session["user"] = f"test-{role}"
        session["role"] = role


def test_starting_training_answers_202_not_a_blocked_request(client, fake_training):
    """
    PE-5's headline. The request used to stay open for the whole retrain; any
    proxy in front of the app would time it out, and the browser had nothing
    to show meanwhile.
    """
    job, _release = fake_training
    sign_in(client)

    response = client.post("/train_model")

    assert response.status_code == 202
    assert response.get_json()["started"] is True

    job.wait()


def test_a_second_start_answers_409(client, fake_training):
    job, release = fake_training
    release.clear()
    sign_in(client)

    try:
        assert client.post("/train_model").status_code == 202

        second = client.post("/train_model")

        assert second.status_code == 409
        assert second.get_json()["started"] is False
    finally:
        release.set()
        job.wait()


def test_the_status_endpoint_reports_the_run(client, fake_training):
    job, release = fake_training
    release.clear()
    sign_in(client)

    try:
        client.post("/train_model")

        payload = client.get("/train_status").get_json()

        assert payload["running"] is True
        assert payload["state"] == RUNNING
        assert payload["started_by"] == "test-admin"
    finally:
        release.set()
        job.wait()

    finished = client.get("/train_status").get_json()

    assert finished["running"] is False
    assert finished["state"] == SUCCEEDED
    assert "completed" in finished["message"]


def test_training_is_still_admin_only(client, fake_training):
    """
    Who may rebuild the biometric model is a decision, and it has not changed.
    Making training asynchronous must not quietly widen it.
    """
    sign_in(client, role="instructor")

    response = client.post("/train_model")

    assert response.status_code in (302, 403)


def test_the_status_endpoint_is_authenticated(client, fake_training):
    response = client.get("/train_status")

    assert response.status_code in (302, 401, 403)


def test_an_unauthenticated_status_poll_is_not_html(client, fake_training):
    """
    Both markers on /train_status are load-bearing. Without @json_api the
    access-control hook answers an expired session with an HTML redirect to
    the login page, and the polling JavaScript hands that to JSON.parse and
    dies with a syntax error instead of saying "please sign in again".
    """
    import app as app_module
    from security.access import classify

    view = app_module.app.view_functions["enrolment.train_status"]

    assert classify(view) is not None, "/train_status has no access-control marker"
    assert getattr(view, "_security_json_api", False), (
        "/train_status is not marked @json_api - a polling client will be "
        "handed an HTML login page to parse as JSON"
    )


def test_an_instructor_may_watch_training_they_did_not_start(client, fake_training):
    """
    /train_status is @authenticated rather than admin-only on purpose: an
    instructor waiting on a retrain needs to know whether it finished, even
    though only an admin can start one.
    """
    job, _release = fake_training
    sign_in(client, role="instructor")

    response = client.get("/train_status")

    assert response.status_code == 200
    assert response.get_json()["state"] == IDLE

    job.wait()
