"""
Finding and opening a camera that actually produces a picture (CAM-1).

**CAM-1, found 2026-08-08 on the deployment machine.** `open_camera_by_index()`
accepted any device that returned a non-`None` frame. The built-in webcam
(`HD User Facing`, index 0) opens successfully, reads successfully, and returns
**pure black frames** - and `selected_camera.txt` named it. So `start_camera()`
returned True, `/video_feed` streamed black, MediaPipe found no faces, and the
operator sat in front of "No valid face detected" indefinitely with nothing
logged anywhere. This is the project's recurring failure mode: code that looks
like it works. See PE-0, the `test_accuracy.py` zero-image bug, and the
`settings` shadowing in lessons.md L5.

**Judge on the median of a probe window, never on the best frame.** The first
version of this gate took the most detailed frame out of eight, reasoning that
one good frame proves a working camera. Run against the actual dead webcam, it
opened it. Measured over twelve frames per device:

| | index 0 - dead built-in | index 2 - a picture |
|---|---|---|
| mean | min 0.00, **median 0.00**, max 2.90 | 45.59 throughout |
| std dev | min 0.48, **median 0.60**, max 16.78 | 32.10 throughout |
| Laplacian variance | min 3.94, **median 5.68**, max **1258.49** | 617.78 throughout |

A dead sensor emits occasional noise bursts, and one of those had *twice* the
Laplacian variance of the real picture. Any statistic over the best frame is
therefore measuring the noise floor's worst moment.

**And the noise floor is not stable, which is why brightness is the check that
matters.** A second probe of the same dead device on the same machine gave
median std dev **5.2** and median detail **138.7** - both comfortably over the
variation and edge thresholds. Only the brightness floor rejected it, because
the median mean was 0.3. So of the three thresholds below, two are for flat
frames (a sensor stuck on grey or white, which brightness alone would pass) and
the brightness floor is the one that catches a dead sensor. Neither covers the
other; both are needed.

**The gate is deliberately conservative** - it rejects output carrying *no
information*, not output that is merely dark. A classroom with the lights off
is a bad picture; a dead sensor is not a picture at all, and only the second is
worth refusing. So a device is rejected when, across the probe window, either:

* the median frame is essentially unlit (`mean` below `MIN_FRAME_BRIGHTNESS`),
  or
* the median frame is both near-uniform (`std dev` below `MIN_FRAME_STDDEV`)
  and edgeless (`Laplacian variance` below `MIN_FRAME_DETAIL`).

Using the median also gives warm-up tolerance for free: a camera whose first
frames are black while auto-exposure settles still has a good median.

**A frozen device is warned about, not rejected.** Bit-identical frames across
a probe usually mean a static image source rather than a camera - the OBS
placeholder above is exactly that. But a legitimate virtual-camera chain can
present a still test pattern, and refusing to open it would be this module
deciding something it cannot actually know. So it is logged loudly and opened.
"""

import contextlib
import logging
import os
import time
from statistics import median

import cv2

logger = logging.getLogger(__name__)

# Runtime state in a root-level text file is PO-3. Left as-is for now: moving
# it into configuration or the database is a later phase, and it is read by
# the enrolment subprocess as well as the web app.
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selected_camera.txt")


# =====================================================
# WHAT COUNTS AS A PICTURE (CAM-1)
#
# Three thresholds, all compared against the *median* frame of a probe window.
# The measured table in the module docstring is where they come from, and every
# one of them sits in a gap of at least a factor of ten between the dead device
# and the working one - which is why a gate this crude is safe.
# =====================================================

# How many frames to look at before judging. A camera that has just opened
# often hands back a stale or half-exposed buffer, and DirectShow devices in
# particular need several reads before auto-exposure settles. An odd count so
# the median is an actual observation rather than an average of two.
PROBE_FRAMES = 9

# Is there any light at all? Measured: 0.00 on the dead device, 45.59 on the
# working one. This is not an image-quality threshold - whether a *face* is
# bright enough is RECOGNITION_QUALITY's job, at min_brightness 40. This only
# asks whether the sensor is producing anything, and 4.0 is far below any
# frame that could ever yield a usable face crop.
MIN_FRAME_BRIGHTNESS = 4.0

# Is there any variation, and any edge? Measured: 0.60 and 5.68 on the dead
# device, 32.10 and 617.78 on the working one. Both must fail for a rejection,
# so a dim-but-real scene - which has plenty of both - is never refused.
MIN_FRAME_STDDEV = 2.0
MIN_FRAME_DETAIL = 10.0

# Opening a busy DirectShow device can block for a long time - measured at
# 14.9 s for DroidCam while OBS held it, which is most of the cost of scanning
# for a camera at all. Worth a line in the log, because from the operator's
# side it looks like the application has hung.
SLOW_PROBE_SECONDS = 5.0


def frame_statistics(frame):
    """Brightness, variation and edge content of one frame, or None."""
    if frame is None or getattr(frame, "size", 0) == 0:
        return None

    if frame.ndim == 3:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    else:
        gray = frame

    return {
        "mean": float(gray.mean()),
        "stddev": float(gray.std()),
        "detail": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
    }


def statistics_carry_a_picture(statistics):
    """
    Do these numbers describe a picture? See the thresholds above.

    Separate from the measuring so the rule can be tested against the recorded
    numbers from the real devices, without needing either device present.
    """
    if statistics is None:
        return False

    if statistics["mean"] < MIN_FRAME_BRIGHTNESS:
        return False

    return (
        statistics["stddev"] >= MIN_FRAME_STDDEV
        or statistics["detail"] >= MIN_FRAME_DETAIL
    )


def frame_carries_a_picture(frame):
    """`(carries_picture, statistics)` for one frame."""
    statistics = frame_statistics(frame)

    return statistics_carry_a_picture(statistics), statistics


def probe_camera(cap, index=None):
    """
    Read a few frames and decide whether this device is showing anything.

    Returns `(ok, reason, statistics)`. `reason` is None on success and a short
    human-readable phrase otherwise, so the caller can log *why* a camera was
    passed over instead of only that it was.
    """
    samples = []
    distinct_frames = set()

    # The whole window is read, not just up to the first good frame. Two
    # reasons: the decision is a median, and "are these frames all identical?"
    # cannot be answered from one frame.
    for _ in range(PROBE_FRAMES):
        ret, frame = cap.read()

        if not ret or frame is None:
            continue

        statistics = frame_statistics(frame)

        if statistics is None:
            continue

        samples.append(statistics)

        with contextlib.suppress(AttributeError, TypeError):
            distinct_frames.add(hash(frame.tobytes()))

    if not samples:
        return False, "no frames could be read", None

    typical = {
        key: median(sample[key] for sample in samples)
        for key in ("mean", "stddev", "detail")
    }

    if not statistics_carry_a_picture(typical):
        return False, "frames carry no picture", typical

    if len(distinct_frames) == 1 and len(samples) > 1:
        logger.warning(
            "Camera %s returned %d identical frames - this is probably a still "
            "image or a stopped virtual camera, not a live feed. Usable only "
            "if nothing else is.",
            index,
            len(samples),
        )
        typical["frozen"] = True

    return True, None, typical


def get_saved_camera_index():
    """Reads saved camera index from file, or returns None."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                idx = int(f.read().strip())
                return idx
        except Exception:
            pass
    return None


def save_camera_index(index):
    """
    Saves camera index preference to file.

    Only ever called with an index that has already passed `probe_camera()`, so
    the stored preference cannot be the dead device CAM-1 describes. That
    matters because the saved index is tried *first* on the next run.
    """
    try:
        with open(CONFIG_FILE, "w") as f:
            f.write(str(index))
        logger.info("Saved default camera index: %s", index)
    except Exception:
        logger.warning("Could not save camera index %s", index, exc_info=True)


def get_available_cameras(max_tested=5):
    """Returns a list of camera indices that produce a usable picture."""
    available = []

    for index in range(max_tested):
        cap = open_camera_by_index(index)

        if cap is not None:
            available.append(index)
            cap.release()

    return available


def open_camera_by_index(index):
    """
    Open one camera index, or None if it will not give us a picture.

    Three ways to fail, and all three are logged with the reason: the device
    will not open, it opens but no frame can be read, or it reads frames that
    carry no picture. That last one is CAM-1 and used to be a success.
    """
    cap, _statistics = open_and_probe(index)
    return cap


def open_and_probe(index):
    """
    `(capture, statistics)` - the same open, with the probe's numbers kept.

    `open_best_camera()` needs to know *how* good a device is, not only that it
    is usable, so it can prefer a live camera over a still image. The public
    single-index helper above keeps its simpler signature.
    """
    logger.debug("Trying camera index %s", index)

    started_at = time.monotonic()
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    elapsed = time.monotonic() - started_at

    if elapsed >= SLOW_PROBE_SECONDS:
        logger.warning(
            "Camera %s took %.1f s to respond. A device that is already in "
            "use by another application blocks like this - close whatever "
            "holds it if this is the camera you meant to use.",
            index,
            elapsed,
        )

    if not cap.isOpened():
        logger.debug("Camera %s could not be opened", index)
        cap.release()
        return None, None

    ok, reason, statistics = probe_camera(cap, index=index)

    if not ok:
        logger.warning(
            "Rejected camera %s: %s%s",
            index,
            reason,
            ""
            if statistics is None
            else (
                f" (mean {statistics['mean']:.1f},"
                f" std dev {statistics['stddev']:.1f},"
                f" detail {statistics['detail']:.1f})"
            ),
        )
        cap.release()
        return None, statistics

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    logger.info(
        "Opened camera %s (%dx%d, mean %.1f, std dev %.1f, detail %.1f%s)",
        index,
        width,
        height,
        statistics["mean"],
        statistics["stddev"],
        statistics["detail"],
        ", FROZEN" if statistics.get("frozen") else "",
    )

    return cap, statistics


def open_best_camera(preferred_index=None, max_tested=5):
    """
    Open the best camera available: preferred, saved, then scan.

    The scan is the reason CAM-1 mattered rather than being a curiosity. It
    used to accept index 0 - the dead built-in webcam on this machine - and stop
    there, so no later index was ever reached. With the content gate in place
    the dead device is passed over and the scan finds the one that works.

    **A frozen device is a last resort, and is never remembered.** Found on
    2026-08-09 while checking whether the system could be demonstrated: with
    the DroidCam client closed, the scan reached the OBS Virtual Camera - which
    stays registered and serves a static "not started" placeholder even with
    OBS shut down - accepted it, and *saved it as the preference*. That would
    have made a still image the permanently preferred camera, tried ahead of
    DroidCam on every subsequent run, and the operator would have got a frozen
    frame and no recognition with nothing obviously wrong.

    So a frozen device is held as a fallback while the scan keeps looking, and
    is used only if nothing live turns up. It is still opened rather than
    refused - a legitimate virtual-camera chain can present a static test
    pattern, and this module cannot know - but it does not get to be sticky.
    """
    logger.info("Searching for a camera that produces a picture")

    # 1. Explicit request from the caller. Honoured even if frozen: an index
    #    passed in by hand is a decision, not a guess.
    if preferred_index is not None:
        try:
            pref_idx = int(preferred_index)
        except (ValueError, TypeError):
            logger.warning(
                "Ignoring unusable preferred camera index %r", preferred_index
            )
        else:
            cap, _statistics = open_and_probe(pref_idx)

            if cap is not None:
                logger.info("Using camera %s (requested)", pref_idx)
                return cap

    # 2. The saved preference. Never frozen, because step 3 does not save one.
    saved_idx = get_saved_camera_index()

    if saved_idx is not None:
        cap, statistics = open_and_probe(saved_idx)

        if cap is not None and not statistics.get("frozen"):
            logger.info("Using camera %s (saved preference)", saved_idx)
            return cap

        if cap is not None:
            # The saved device has since gone static - OBS stopped, a capture
            # card lost its input. Prefer anything live over it.
            cap.release()
            logger.warning(
                "Saved camera %s is frozen now, looking for a live one",
                saved_idx,
            )
        else:
            logger.warning(
                "Saved camera %s is no longer usable, scanning for another",
                saved_idx,
            )

    # 3. Scan, keeping any frozen device as a fallback rather than taking it.
    frozen_fallback = None
    frozen_index = None

    for index in range(max_tested):
        cap, statistics = open_and_probe(index)

        if cap is None:
            continue

        if statistics.get("frozen"):
            if frozen_fallback is None:
                frozen_fallback = cap
                frozen_index = index
            else:
                cap.release()
            continue

        logger.info("Using camera %s (found by scan)", index)
        save_camera_index(index)

        if frozen_fallback is not None:
            frozen_fallback.release()

        return cap

    if frozen_fallback is not None:
        logger.warning(
            "Using camera %s, which appears to be a still image rather than a "
            "live feed - nothing else on this machine produced a picture. Not "
            "saving it as the default. If a phone camera is meant to be in "
            "use, start the DroidCam client and try again.",
            frozen_index,
        )
        return frozen_fallback

    logger.error(
        "No camera produced a picture. Checked indices 0-%d. If a phone camera "
        "is being used over DroidCam, make sure the client is connected and "
        "that no other application (OBS, Teams, Zoom) is holding the device.",
        max_tested - 1,
    )
    return None
