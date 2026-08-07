import cv2

# Open webcam (Camera 0)
cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)

if not cap.isOpened():
    print("ERROR: Cannot open webcam.")
    exit()

# Set resolution (optional)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

print("Webcam opened successfully.")
print("Press ESC to exit.")

while True:
    ret, frame = cap.read()

    if not ret:
        print("Failed to read frame.")
        break

    cv2.imshow("Webcam Test", frame)

    key = cv2.waitKey(1) & 0xFF
    if key == 27:  # ESC
        break

cap.release()
cv2.destroyAllWindows()