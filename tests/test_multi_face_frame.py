"""
Several students in one frame: no lag from their number, no swapped names.

Asked for by the user: verify five students at once "without lagging and
mixing their names". Two things in `recognise_frame()` stood in the way.

1. **Lag.** Each face paid a full LBPH predict, one after another: ~170 ms a
   face on the 245 MB model, so ~800 ms a frame with five in shot, and five
   times as long to fill everyone's confirmation window. The predicts now run
   side by side (`predict_identities()`).
2. **Mixed names.** Everything ran in MediaPipe's listing order: tracks were
   matched one face at a time (see `test_vision_tracking.py` for that half),
   and a new track listed before the one holding student X was not blocked
   from reading as X - nor was one adopting X's finished mark.

No camera, no MediaPipe, no model, no database. Identifiers are fake (L6).
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np
import pytest

import recognize_face
from vision.geometry import FaceBox
from vision.tracking import FaceTracker

ALICE = "SEC-TEST-NOBODY"
ALICE_NAME = "Nobody"
CONFIG = recognize_face.TRACK_CONFIG

BOXES = {
    "newcomer": FaceBox(100, 100, 200, 200),
    "alice": FaceBox(500, 100, 600, 200),
}


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Everything between the frame and the decision, made transparent."""
    monkeypatch.setattr(
        recognize_face, "get_face_box", lambda landmarks, *_: BOXES[landmarks]
    )
    monkeypatch.setattr(
        recognize_face, "face_passes_geometry_gate", lambda *_: True
    )
    monkeypatch.setattr(recognize_face, "face_is_frontal", lambda *_: True)
    monkeypatch.setattr(recognize_face, "align_face", lambda _f, lm: lm)
    monkeypatch.setattr(recognize_face, "get_face_quality_issue", lambda _f: None)
    monkeypatch.setattr(recognize_face, "preprocess_for_lbph", lambda f: f)


class AlwaysAlice:
    """LBPH reading every face as Alice - a neighbour misread as her."""

    def predict(self, _face):
        return 0, 10.0


def test_a_newcomer_listed_first_cannot_take_a_name_already_on_screen(
    stub_pipeline, monkeypatch
):
    """
    Alice is marked and on screen. The face beside her is misread as Alice
    and MediaPipe happens to list it first.

    Before: the newcomer resolved first, found Alice in today's register and
    adopted her mark - two boxes reading "Nobody". Now Alice's track claims
    her name first, and the adopt branch respects the claim.
    """
    tracker = FaceTracker(config=CONFIG)
    alice_track, newcomer_track = tracker.assign_all(
        [BOXES["alice"], BOXES["newcomer"]]
    )
    tracker.state_for(alice_track).adopt_completed_identity(
        ALICE, ALICE_NAME, "Present"
    )

    for _ in range(CONFIG.real_face_confirm_frames):
        tracker.state_for(alice_track).note_valid_frame()
        tracker.state_for(newcomer_track).note_valid_frame()

    context = SimpleNamespace(
        tracker=tracker,
        recognizer=AlwaysAlice(),
        label_map={0: {"student_id": ALICE, "name": ALICE_NAME}},
        recognized={ALICE: "Present"},
    )
    monkeypatch.setattr(
        recognize_face,
        "detect_faces",
        lambda *_: SimpleNamespace(multi_face_landmarks=["newcomer", "alice"]),
    )

    overlays = recognize_face.recognise_frame(
        context, np.zeros((480, 640, 3), np.uint8)
    )
    by_track = {overlay.track_id: overlay for overlay in overlays}

    assert by_track[alice_track].student_name == ALICE_NAME
    assert by_track[newcomer_track].student_name != ALICE_NAME
    assert by_track[newcomer_track].status == "Duplicate identity blocked"
    assert not tracker.state_for(newcomer_track).confirmed


def test_the_predictions_of_one_frame_run_side_by_side(stub_pipeline):
    """
    Three faces' predicts must be in flight at once. Each waits on a barrier
    for the other two; run one after another, the first would time out.
    """
    in_flight = threading.Barrier(3, timeout=5)

    class WaitsForTheOthers:
        def predict(self, face):
            in_flight.wait()
            return face, 10.0

    context = SimpleNamespace(recognizer=WaitsForTheOthers())
    faces = [
        (label, SimpleNamespace(confirmed=False, attendance_settled=False), label)
        for label in (7, 8, 9)
    ]

    predictions = recognize_face.predict_identities(context, object(), faces)

    assert predictions == [(7, 10.0, None), (8, 10.0, None), (9, 10.0, None)]


def test_preprocessing_stays_on_the_calling_thread(stub_pipeline, monkeypatch):
    """
    ⚠️ `preprocess_for_lbph()` applies one shared CLAHE object, which keeps
    scratch buffers on itself. Only the predict may leave this thread.
    """
    threads = []

    def recording_preprocess(face):
        threads.append(threading.current_thread())
        return face

    monkeypatch.setattr(recognize_face, "preprocess_for_lbph", recording_preprocess)

    class Echo:
        def predict(self, face):
            return face, 10.0

    faces = [
        (label, SimpleNamespace(confirmed=False, attendance_settled=False), label)
        for label in range(5)
    ]

    recognize_face.predict_identities(
        SimpleNamespace(recognizer=Echo()), object(), faces
    )

    assert threads == [threading.current_thread()] * 5


def test_a_failed_predict_is_that_face_only(stub_pipeline):
    """One face's exception becomes its own issue, not the frame's."""

    class FailsOnTwo:
        def predict(self, face):
            if face == 2:
                raise RuntimeError("boom")
            return face, 10.0

    faces = [
        (label, SimpleNamespace(confirmed=False, attendance_settled=False), label)
        for label in (1, 2, 3)
    ]

    predictions = recognize_face.predict_identities(
        SimpleNamespace(recognizer=FailsOnTwo()), object(), faces
    )

    assert predictions == [
        (1, 10.0, None),
        (-1, 999.0, "Face alignment failed"),
        (3, 10.0, None),
    ]
