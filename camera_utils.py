import cv2
import os

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selected_camera.txt")

def get_saved_camera_index():
    """Reads saved camera index from file, or returns None."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
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
        print(f"[CAMERA CONFIG] Saved default camera index: {index}")
    except Exception as e:
        print(f"[CAMERA CONFIG] Error saving camera index: {e}")

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
    print(f"Trying Camera Index {index}...")
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        print(f"Failed to open Camera {index}.")
        return None
    ret, frame = cap.read()
    if not ret or frame is None:
        print(f"Camera {index} opened but cannot read frame.")
        cap.release()
        return None
        
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"SUCCESS -> Opened Camera {index} ({width}x{height})")
    return cap

def open_best_camera(preferred_index=None):
    """Opens preferred camera, saved camera, or first available camera."""
    print("=" * 60)
    print("Searching cameras...")
    print("=" * 60)

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

    print("No camera detected.")
    return None