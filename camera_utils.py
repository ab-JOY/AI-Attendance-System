import logging
import os

import cv2

logger = logging.getLogger(__name__)

# Runtime state in a root-level text file is PO-3. Left as-is for now: moving
# it into configuration or the database is a later phase, and it is read by
# the enrolment subprocess as well as the web app.
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selected_camera.txt")

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
    """Saves camera index preference to file."""
    try:
        with open(CONFIG_FILE, "w") as f:
            f.write(str(index))
        logger.info("Saved default camera index: %s", index)
    except Exception:
        logger.warning("Could not save camera index %s", index, exc_info=True)

def get_available_cameras(max_tested=5):
    """Returns a list of working camera index integers."""
    available = []
    for index in range(max_tested):
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if cap.isOpened():
            ret, frame = cap.read()
            if ret and frame is not None:
                available.append(index)
            cap.release()
    return available

def open_camera_by_index(index):
    """Tries to open a specific camera index."""
    logger.debug("Trying camera index %s", index)
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        logger.debug("Failed to open camera %s", index)
        return None
    ret, frame = cap.read()
    if not ret or frame is None:
        logger.debug("Camera %s opened but cannot read a frame", index)
        cap.release()
        return None

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    logger.info("Opened camera %s (%dx%d)", index, width, height)
    return cap

def open_best_camera(preferred_index=None):
    """Opens preferred camera, saved camera, or first available camera."""
    logger.info("Searching for an available camera")

    # 1. Try preferred index if passed explicitly
    if preferred_index is not None:
        try:
            pref_idx = int(preferred_index)
            cap = open_camera_by_index(pref_idx)
            if cap is not None:
                return cap
        except (ValueError, TypeError):
            pass

    # 2. Try saved index from config
    saved_idx = get_saved_camera_index()
    if saved_idx is not None:
        cap = open_camera_by_index(saved_idx)
        if cap is not None:
            return cap

    # 3. Fallback: Scan 0..4 sequentially
    for index in range(5):
        cap = open_camera_by_index(index)
        if cap is not None:
            return cap

    logger.error("No camera detected")
    return None
