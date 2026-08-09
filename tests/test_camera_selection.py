"""
A camera that produces no picture must be refused, not opened (CAM-1).

The defect these cover was found on the deployment machine on 2026-08-08. The
built-in webcam opens, reads successfully, and returns pure black frames -
mean 0.0, std dev 0.5, Laplacian variance 3.3 - and `selected_camera.txt` named
it. `open_camera_by_index()` checked only `ret and frame is not None`, so it
was accepted. Everything downstream then behaved perfectly: `start_camera()`
returned True, the video feed streamed black, MediaPipe found nothing, and the
operator got "No valid face detected" forever with no error in any log.

The real numbers from that machine are used as fixtures below, so these tests
are calibrated against the device that caused the bug rather than against an
invented one.

No camera required: `cv2.VideoCapture` is faked.
"""

from __future__ import annotations

import numpy as np
import pytest

import camera_utils
from camera_utils import (
    MIN_FRAME_BRIGHTNESS,
    MIN_FRAME_DETAIL,
    MIN_FRAME_STDDEV,
    PROBE_FRAMES,
    frame_carries_a_picture,
    open_best_camera,
    open_camera_by_index,
    probe_camera,
)

WIDTH, HEIGHT = 640, 480


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------


def black_frame():
    """The dead webcam on a quiet probe: mean 0.0, std dev 0.6, detail 5.7."""
    rng = np.random.default_rng(0)
    noise = rng.normal(0.0, 0.5, (HEIGHT, WIDTH)).clip(0, 255)
    return np.repeat(noise.astype(np.uint8)[:, :, None], 3, axis=2)


def noisy_black_frame(seed=0):
    """
    The same dead webcam on a noisy probe: mean 0.3, std dev 5.2, detail 138.7.

    This is the frame that defeated the first version of the gate, and it is
    the reason `MIN_FRAME_BRIGHTNESS` exists. Its variation and edge content
    both clear their thresholds - only the brightness floor rejects it.
    """
    rng = np.random.default_rng(seed)
    noise = rng.exponential(0.35, (HEIGHT, WIDTH))
    speckle = rng.random((HEIGHT, WIDTH)) < 0.004
    noise[speckle] += rng.integers(40, 120, speckle.sum())
    gray = noise.clip(0, 255).astype(np.uint8)
    return np.repeat(gray[:, :, None], 3, axis=2)


def flat_grey_frame(level=128):
    """A solid fill. Not dark, but carries exactly no information."""
    return np.full((HEIGHT, WIDTH, 3), level, dtype=np.uint8)


def scene_frame(seed=1, brightness=45):
    """A real picture: structure and edges, at the dim end of usable."""
    rng = np.random.default_rng(seed)
    frame = rng.integers(0, 90, (HEIGHT, WIDTH, 3), dtype=np.uint8)
    frame[100:300, 200:400] = brightness + 60
    return frame


def dim_scene_frame():
    """
    A classroom with the lights off. Must be accepted.

    This is the test that stops the gate being too strict: a bad picture is
    still a picture, and refusing to open the camera would be worse than
    showing a dark one.
    """
    rng = np.random.default_rng(7)
    frame = rng.integers(0, 24, (HEIGHT, WIDTH, 3), dtype=np.uint8)
    frame[200:260, 200:260] = 40
    return frame


# ---------------------------------------------------------------------------
# Fake capture
# ---------------------------------------------------------------------------


class FakeCapture:
    def __init__(self, frames, opened=True, read_failures=0):
        self._frames = frames
        self._index = 0
        self._opened = opened
        self._read_failures = read_failures
        self.released = False
        self.reads = 0

    def isOpened(self):  # noqa: N802 - matches the cv2 API being faked
        return self._opened

    def read(self):
        self.reads += 1

        if self._read_failures > 0:
            self._read_failures -= 1
            return False, None

        if not self._frames:
            return False, None

        frame = self._frames[min(self._index, len(self._frames) - 1)]
        self._index += 1
        return True, frame

    def get(self, _prop):
        return 640.0

    def release(self):
        self.released = True


def install_devices(monkeypatch, devices):
    """Replace cv2.VideoCapture with a fixed index -> FakeCapture map."""
    opened = {}

    def fake_video_capture(index, _backend=None):
        capture = devices.get(index) or FakeCapture([], opened=False)
        opened[index] = capture
        return capture

    monkeypatch.setattr(camera_utils.cv2, "VideoCapture", fake_video_capture)
    return opened


@pytest.fixture(autouse=True)
def no_saved_preference(monkeypatch, tmp_path):
    """
    Point the preference file at a temp path.

    Not tidiness: the real `selected_camera.txt` currently contains `0`, the
    dead device, and `save_camera_index()` writes to it. A test must not
    rewrite the operator's camera choice.
    """
    monkeypatch.setattr(
        camera_utils, "CONFIG_FILE", str(tmp_path / "selected_camera.txt")
    )


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


def test_the_dead_webcam_is_recognised_as_carrying_no_picture():
    carries, stats = frame_carries_a_picture(black_frame())

    assert carries is False
    assert stats["mean"] < 1.0
    assert stats["stddev"] < MIN_FRAME_STDDEV
    assert stats["detail"] < MIN_FRAME_DETAIL


def test_a_noisy_dead_sensor_is_rejected_on_brightness_alone():
    """
    The case that broke the first version of this gate.

    The dead webcam's noise floor is not stable. On one probe it looked like
    mean 0.0 / std dev 0.6 / detail 5.7; on another, mean 0.3 / std dev 5.2 /
    detail 138.7 - passing both the variation and the edge threshold. If this
    test ever fails because the brightness floor was removed as redundant, it
    is not redundant: it is the only check that catches this.
    """
    carries, stats = frame_carries_a_picture(noisy_black_frame())

    assert stats["stddev"] >= MIN_FRAME_STDDEV, (
        "the fixture no longer reproduces the noisy case it exists for"
    )
    assert stats["detail"] >= MIN_FRAME_DETAIL, (
        "the fixture no longer reproduces the noisy case it exists for"
    )
    assert stats["mean"] < MIN_FRAME_BRIGHTNESS
    assert carries is False, "a dead sensor's noise was accepted as a picture"


def test_a_probe_of_a_noisy_dead_sensor_is_rejected():
    """The same thing through the probe window, where medians are taken."""
    capture = FakeCapture([noisy_black_frame(i) for i in range(PROBE_FRAMES)])

    ok, reason, stats = probe_camera(capture, index=0)

    assert ok is False
    assert reason == "frames carry no picture"
    assert stats["mean"] < MIN_FRAME_BRIGHTNESS


def test_one_noise_burst_does_not_rescue_a_dead_camera():
    """
    Why the decision is a median and not a maximum.

    Measured on the real device: one frame in twelve had Laplacian variance
    1258, twice the *working* camera's 618. Judging on the best frame opened
    the dead camera; judging on the median does not.
    """
    frames = [black_frame() for _ in range(PROBE_FRAMES - 1)]
    frames.insert(PROBE_FRAMES // 2, noisy_black_frame(99))

    ok, _reason, _stats = probe_camera(FakeCapture(frames), index=0)

    assert ok is False, "a single noise burst was mistaken for a picture"


def test_a_flat_frame_carries_no_picture_however_bright_it_is():
    """Rejecting only *black* would miss a sensor stuck on white or grey."""
    for level in (0, 128, 255):
        carries, _stats = frame_carries_a_picture(flat_grey_frame(level))
        assert carries is False, f"a solid fill at {level} was accepted"


def test_a_real_scene_carries_a_picture():
    carries, stats = frame_carries_a_picture(scene_frame())

    assert carries is True
    assert stats["stddev"] >= MIN_FRAME_STDDEV


def test_a_dim_scene_is_still_a_picture():
    """
    The conservative half of the gate. A dark room must not be mistaken for a
    dead camera - that would turn a lighting problem into an outage.
    """
    carries, stats = frame_carries_a_picture(dim_scene_frame())

    assert carries is True, (
        f"a dim but real frame was rejected (mean {stats['mean']:.1f}, "
        f"std dev {stats['stddev']:.1f}, detail {stats['detail']:.1f})"
    )


def test_a_missing_frame_carries_no_picture():
    assert frame_carries_a_picture(None) == (False, None)
    assert frame_carries_a_picture(np.array([], dtype=np.uint8)) == (False, None)


def test_a_greyscale_frame_is_handled():
    """Some virtual devices hand back single-channel frames."""
    grey = np.full((HEIGHT, WIDTH), 12, dtype=np.uint8)
    carries, stats = frame_carries_a_picture(grey)

    assert carries is False
    assert stats is not None


# ---------------------------------------------------------------------------
# Probing a device
# ---------------------------------------------------------------------------


def test_probe_rejects_a_device_that_only_produces_black():
    capture = FakeCapture([black_frame()])

    ok, reason, stats = probe_camera(capture, index=0)

    assert ok is False
    assert reason == "frames carry no picture"
    assert stats is not None, "the numbers behind the rejection were not reported"


def test_probe_rejects_a_device_that_reads_nothing():
    capture = FakeCapture([])

    ok, reason, _stats = probe_camera(capture, index=3)

    assert ok is False
    assert reason == "no frames could be read"


def test_probe_accepts_a_working_device():
    capture = FakeCapture([scene_frame(i) for i in range(PROBE_FRAMES)])

    ok, reason, stats = probe_camera(capture, index=2)

    assert ok is True
    assert reason is None
    assert stats["detail"] >= MIN_FRAME_DETAIL


def test_probe_tolerates_a_camera_that_warms_up_slowly():
    """
    A device whose first frames are black but which then produces a picture
    must be accepted. Judging on frame one would reject most DirectShow
    cameras, which is why the probe looks at several and accepts the first
    that carries content.
    """
    frames = [black_frame(), black_frame(), black_frame()]
    frames += [scene_frame(i) for i in range(PROBE_FRAMES)]

    ok, _reason, _stats = probe_camera(FakeCapture(frames), index=1)

    assert ok is True


def test_probe_tolerates_intermittent_read_failures():
    capture = FakeCapture(
        [scene_frame(i) for i in range(PROBE_FRAMES)], read_failures=3
    )

    ok, _reason, _stats = probe_camera(capture, index=1)

    assert ok is True


def test_a_frozen_device_is_warned_about_but_still_opened(caplog):
    """
    Bit-identical frames usually mean a still image - the OBS "virtual camera
    not started" placeholder is exactly that. But a legitimate chain can
    present a static test pattern, and refusing it would be this module
    deciding something it cannot know. Warn, do not refuse.
    """
    frozen = scene_frame()
    capture = FakeCapture([frozen] * PROBE_FRAMES)

    with caplog.at_level("WARNING"):
        ok, _reason, _stats = probe_camera(capture, index=2)

    assert ok is True
    assert any("identical frames" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Opening one index
# ---------------------------------------------------------------------------


def test_open_returns_none_and_releases_a_black_camera(monkeypatch):
    """
    The exact CAM-1 case. The old code returned this camera.

    Releasing matters as much as refusing: a device left open is still held,
    and on Windows the next attempt to open it fails.
    """
    devices = {0: FakeCapture([black_frame()])}
    install_devices(monkeypatch, devices)

    assert open_camera_by_index(0) is None
    assert devices[0].released, "a rejected camera was left open"


def test_open_returns_the_capture_for_a_working_camera(monkeypatch):
    devices = {2: FakeCapture([scene_frame(i) for i in range(PROBE_FRAMES)])}
    install_devices(monkeypatch, devices)

    capture = open_camera_by_index(2)

    assert capture is devices[2]
    assert not capture.released


def test_open_releases_a_device_that_will_not_open(monkeypatch):
    devices = {1: FakeCapture([], opened=False)}
    install_devices(monkeypatch, devices)

    assert open_camera_by_index(1) is None
    assert devices[1].released


def test_a_rejection_says_why(monkeypatch, caplog):
    """
    Silence is what made CAM-1 expensive. A camera passed over has to say so,
    with the numbers, or the next person debugging this starts where I did.
    """
    install_devices(monkeypatch, {0: FakeCapture([black_frame()])})

    with caplog.at_level("WARNING"):
        open_camera_by_index(0)

    messages = " ".join(record.message for record in caplog.records)

    assert "Rejected camera 0" in messages
    assert "no picture" in messages
    assert "std dev" in messages


# ---------------------------------------------------------------------------
# Choosing between devices - the whole point
# ---------------------------------------------------------------------------


def test_the_scan_skips_the_dead_webcam_and_finds_the_working_camera(monkeypatch):
    """
    The deployment machine, reproduced: index 0 black, index 1 unopenable
    (held by another application), index 2 a real picture.

    The old code stopped at index 0 and never reached index 2.
    """
    devices = {
        0: FakeCapture([black_frame()]),
        1: FakeCapture([], opened=False),
        2: FakeCapture([scene_frame(i) for i in range(PROBE_FRAMES)]),
    }
    install_devices(monkeypatch, devices)

    capture = open_best_camera()

    assert capture is devices[2], "the scan did not reach the working camera"


def test_the_working_index_is_remembered(monkeypatch):
    devices = {
        0: FakeCapture([black_frame()]),
        2: FakeCapture([scene_frame(i) for i in range(PROBE_FRAMES)]),
    }
    install_devices(monkeypatch, devices)

    open_best_camera()

    assert camera_utils.get_saved_camera_index() == 2


def test_a_dead_index_is_never_remembered(monkeypatch):
    """
    `selected_camera.txt` currently holds `0` - the dead device - and the saved
    index is tried *first*. Writing a rejected index back would make the bug
    sticky across restarts.
    """
    install_devices(monkeypatch, {0: FakeCapture([black_frame()])})

    assert open_best_camera() is None
    assert camera_utils.get_saved_camera_index() is None


def test_a_stale_saved_preference_falls_through_to_the_scan(monkeypatch):
    """Exactly the state the machine is in: saved index 0, which is dead."""
    devices = {
        0: FakeCapture([black_frame()]),
        2: FakeCapture([scene_frame(i) for i in range(PROBE_FRAMES)]),
    }
    install_devices(monkeypatch, devices)
    camera_utils.save_camera_index(0)

    capture = open_best_camera()

    assert capture is devices[2]
    assert camera_utils.get_saved_camera_index() == 2, (
        "the stale preference was not replaced"
    )


def test_an_explicit_preference_wins(monkeypatch):
    devices = {
        1: FakeCapture([scene_frame(i) for i in range(PROBE_FRAMES)]),
        2: FakeCapture([scene_frame(i) for i in range(PROBE_FRAMES)]),
    }
    install_devices(monkeypatch, devices)

    assert open_best_camera(preferred_index=2) is devices[2]


def test_an_unusable_preferred_index_does_not_stop_the_search(monkeypatch):
    devices = {2: FakeCapture([scene_frame(i) for i in range(PROBE_FRAMES)])}
    install_devices(monkeypatch, devices)

    assert open_best_camera(preferred_index="not-a-number") is devices[2]


def test_no_usable_camera_returns_none_and_says_what_to_check(monkeypatch, caplog):
    devices = {
        0: FakeCapture([black_frame()]),
        1: FakeCapture([], opened=False),
    }
    install_devices(monkeypatch, devices)

    with caplog.at_level("ERROR"):
        assert open_best_camera() is None

    messages = " ".join(record.message for record in caplog.records)

    assert "DroidCam" in messages, "the log does not name the likely cause"


# ---------------------------------------------------------------------------
# A still image is a last resort, and is never remembered
# ---------------------------------------------------------------------------


def frozen_device():
    """The OBS Virtual Camera placeholder: a real picture, but never changing."""
    still = scene_frame(42)
    return FakeCapture([still] * PROBE_FRAMES)


def test_a_live_camera_is_preferred_over_a_still_image(monkeypatch):
    """
    Found on 2026-08-09 while checking whether the system could be
    demonstrated. The OBS Virtual Camera stays registered and serves a static
    "not started" placeholder even with OBS shut down. It carries a picture, so
    the content gate passes it - and it sat at a lower index than nothing else,
    so the scan took it and stopped.
    """
    devices = {
        0: FakeCapture([black_frame()]),
        1: frozen_device(),
        2: FakeCapture([scene_frame(i) for i in range(PROBE_FRAMES)]),
    }
    install_devices(monkeypatch, devices)

    capture = open_best_camera()

    assert capture is devices[2], "a still image was chosen over a live camera"
    assert devices[1].released, "the rejected still-image device was left open"


def test_a_still_image_is_never_saved_as_the_preference(monkeypatch):
    """
    The part that would have made this stick. Saving it means it is tried
    *first* on every subsequent run, ahead of the DroidCam device the operator
    actually meant to use - and a frozen frame produces no recognition with
    nothing obviously wrong.
    """
    install_devices(monkeypatch, {2: frozen_device()})

    assert open_best_camera() is not None, "the fallback was not used at all"
    assert camera_utils.get_saved_camera_index() is None, (
        "a still image was saved as the default camera"
    )


def test_a_still_image_is_used_when_nothing_else_works(monkeypatch):
    """
    Still a last resort, not a refusal. A legitimate virtual-camera chain can
    present a static test pattern, and this module cannot tell the difference.
    """
    devices = {
        0: FakeCapture([black_frame()]),
        3: frozen_device(),
    }
    install_devices(monkeypatch, devices)

    assert open_best_camera() is devices[3]


def test_a_saved_preference_that_has_gone_static_is_abandoned(monkeypatch):
    """OBS stopped, or a capture card lost its input, since the last run."""
    devices = {
        1: frozen_device(),
        2: FakeCapture([scene_frame(i) for i in range(PROBE_FRAMES)]),
    }
    install_devices(monkeypatch, devices)
    camera_utils.save_camera_index(1)

    capture = open_best_camera()

    assert capture is devices[2]
    assert camera_utils.get_saved_camera_index() == 2


def test_an_explicitly_requested_still_image_is_honoured(monkeypatch):
    """An index passed in by hand is a decision, not a guess."""
    devices = {1: frozen_device()}
    install_devices(monkeypatch, devices)

    assert open_best_camera(preferred_index=1) is devices[1]


def test_only_one_frozen_fallback_is_held_open(monkeypatch):
    """Two placeholder devices must not leak one capture between them."""
    devices = {1: frozen_device(), 2: frozen_device(), 3: frozen_device()}
    install_devices(monkeypatch, devices)

    capture = open_best_camera()

    held = [d for d in devices.values() if not d.released]

    assert len(held) == 1, f"{len(held)} captures left open"
    assert capture is held[0]


def test_get_available_cameras_lists_only_usable_ones(monkeypatch):
    devices = {
        0: FakeCapture([black_frame()]),
        1: FakeCapture([], opened=False),
        2: FakeCapture([scene_frame(i) for i in range(PROBE_FRAMES)]),
        3: FakeCapture([scene_frame(i) for i in range(PROBE_FRAMES)]),
    }
    install_devices(monkeypatch, devices)

    assert camera_utils.get_available_cameras(max_tested=5) == [2, 3]
