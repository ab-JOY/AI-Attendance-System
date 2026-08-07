import cv2

for i in range(5):

    cap = cv2.VideoCapture(1)

    if cap.isOpened():

        ret, frame = cap.read()

        if ret:
            print(f"Camera {i} works")

        cap.release()

    else:
        print(f"Camera {i} not found")