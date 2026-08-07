import cv2

for i in range(5):
    print(f"Testing Camera {i}")

    cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)

    if cap.isOpened():

        ret, frame = cap.read()

        if ret:
            cv2.imshow(f"Camera {i}", frame)
            print(f"Camera {i} works")
            cv2.waitKey(3000)

        cap.release()
        cv2.destroyAllWindows()