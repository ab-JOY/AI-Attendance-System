import mediapipe as mp

print("MediaPipe:", mp.__version__)

mp_face_mesh = mp.solutions.face_mesh

face_mesh = mp_face_mesh.FaceMesh()

print("MediaPipe Face Mesh Loaded Successfully!")