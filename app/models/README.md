face_detection_yunet_2023mar.onnx is YuNet, a small face detector (about 230 KB, runs on the CPU in OpenCV).
Source: https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet (MIT licence).
Used by app/facefind.py to say where the real faces are, so the vision model is only asked about eyes in real faces.
