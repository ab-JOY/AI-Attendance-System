import cv2
import os
from datetime import datetime
from db_connection import get_connection


# =========================
# GET ACTIVE SESSION
# =========================
def get_active_session():
    try:
        with open("active_session.txt", "r") as file:
            return file.read().strip()
    except FileNotFoundError:
        return None
    
# =========================
# LOAD FACE DETECTOR
# =========================
face_cascade = cv2.CascadeClassifier(
    "haarcascade/haarcascade_frontalface_default.xml"
)

# =========================
# LOAD TRAINED MODEL
# =========================
recognizer = cv2.face.LBPHFaceRecognizer_create()
recognizer.read("trainer.yml")

# =========================
# BUILD LABEL MAP
# =========================
dataset_path = "dataset"
label_map = {}

current_label = 0
for folder in os.listdir(dataset_path):
    folder_path = os.path.join(dataset_path, folder)

    if os.path.isdir(folder_path):
        label_map[current_label] = folder
        current_label += 1


# =========================
# CHECK IF ALREADY MARKED IN SESSION
# =========================
def already_marked_in_session(student_id, session_id):
    conn = get_connection()
    cursor = conn.cursor()

    sql = """
    SELECT * FROM attendance
    WHERE student_id = %s AND session_id = %s
    """

    cursor.execute(sql, (student_id, session_id))
    result = cursor.fetchone()

    cursor.close()
    conn.close()

    return result is not None

# =========================
# MARK ATTENDANCE TO MYSQL
# =========================
def mark_attendance(identity):
    session_id = get_active_session()

    if session_id is None:
        return "No Active Session"

    parts = identity.split("_", 1)

    student_id = parts[0]

    today = datetime.now().strftime("%Y-%m-%d")
    time_now = datetime.now().strftime("%I:%M:%S %p10")

    if already_marked_in_session(student_id, session_id):
        return "Already Marked"

    conn = get_connection()
    cursor = conn.cursor()

    sql = """
    INSERT INTO attendance (session_id, student_id, date, time, status)
    VALUES (%s, %s, %s, %s, %s)
    """

    values = (session_id, student_id, today, time_now, "Present")

    try:
        cursor.execute(sql, values)
        conn.commit()
        return "Attendance Recorded"

    except Exception as e:
        print("Full Database Error:", e)
        return "Database Error"

    finally:
        cursor.close()
        conn.close()


# =========================
# START CAMERA
# =========================
cap = cv2.VideoCapture(1)

while True:
    ret, frame = cap.read()

    if not ret:
        break
    frame = cv2.flip(frame,-1)

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    faces = face_cascade.detectMultiScale(
        gray,
        scaleFactor=1.1,
        minNeighbors=6,
        minSize=(80, 80)
    )

    for (x, y, w, h) in faces:
        face = gray[y:y+h, x:x+w]
        face = cv2.resize(face, (200, 200))

        label, confidence = recognizer.predict(face)

        if confidence < 55:
            identity = label_map.get(label, "Unknown")
            status_message = mark_attendance(identity)

            display_text = f"{identity} - {status_message}"
            color = (0, 255, 0)
        else:
            display_text = "Unknown"
            color = (0, 0, 255)

        cv2.rectangle(frame, (x, y), (x+w, y+h), color, 2)

        cv2.putText(
            frame,
            display_text,
            (x, y - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            color,
            2
        )

    cv2.imshow("MySQL Attendance System", frame)

    if cv2.waitKey(1) == 27:
        break

cap.release()
cv2.destroyAllWindows()