"""Where are the faces?  A small face detector (YuNet, in OpenCV) that runs on the CPU in a few
milliseconds, so the vision model is only asked about eyes in places that really are faces.
(Asked to "find every head", the vision model invents faces in scenery.)"""
import os
import threading

import numpy as np

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")       # hides a harmless 'Targets are not supported' notice, one per thread

MODEL = os.environ.get("YUNET_MODEL", os.path.join(os.path.dirname(os.path.abspath(__file__)), "models",
                                                    "face_detection_yunet_2023mar.onnx"))
_local = threading.local()


def _detector(w, h, score):
    import cv2
    d = getattr(_local, "det", None)
    if d is None:
        if not os.path.exists(MODEL):
            raise OSError(f"face detector model not found: {MODEL}")
        d = _local.det = cv2.FaceDetectorYN.create(MODEL, "", (w, h), score, 0.3, 5000)
    d.setInputSize((w, h))
    d.setScoreThreshold(score)
    return d


def detect(im, score=0.7):
    """im: PIL RGB image.  -> [(box, score)] with box = (x1, y1, x2, y2) as fractions of the image,
    biggest face first."""
    a = np.ascontiguousarray(np.array(im.convert("RGB"))[:, :, ::-1])      # OpenCV wants BGR
    h, w = a.shape[:2]
    _, faces = _detector(w, h, score).detect(a)
    out = []
    for f in (faces if faces is not None else []):
        x, y, bw, bh = (float(v) for v in f[:4])
        b = (min(max(x / w, 0.0), 1.0), min(max(y / h, 0.0), 1.0),
             min(max((x + bw) / w, 0.0), 1.0), min(max((y + bh) / h, 0.0), 1.0))
        if b[2] - b[0] > 0.002 and b[3] - b[1] > 0.002:
            out.append((b, float(f[-1])))
    out.sort(key=lambda t: -(t[0][2] - t[0][0]) * (t[0][3] - t[0][1]))
    return out
