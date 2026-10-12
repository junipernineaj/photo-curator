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


def detect(im, score=0.85):
    """im: PIL RGB image.  -> [(box, score, k)] with box = (x1, y1, x2, y2) as fractions of the image,
    k = ten numbers: two eyes, nose and the two mouth corners as fractions (x, y, x, y, ...), eyes
    ordered left to right in the picture.  Biggest face first."""
    a = np.ascontiguousarray(np.array(im.convert("RGB"))[:, :, ::-1])      # OpenCV wants BGR
    h, w = a.shape[:2]
    _, faces = _detector(w, h, score).detect(a)
    out = []
    for f in (faces if faces is not None else []):
        x, y, bw, bh = (float(v) for v in f[:4])
        b = (min(max(x / w, 0.0), 1.0), min(max(y / h, 0.0), 1.0),
             min(max((x + bw) / w, 0.0), 1.0), min(max((y + bh) / h, 0.0), 1.0))
        pts = [(float(f[4 + 2 * i]) / w, float(f[5 + 2 * i]) / h) for i in range(5)]
        pts[0], pts[1] = sorted(pts[:2])                            # left eye first, whatever the detector's naming
        pts[3], pts[4] = sorted(pts[3:5])
        k = [round(min(max(v, 0.0), 1.0), 4) for xy in pts for v in xy]
        if b[2] - b[0] > 0.002 and b[3] - b[1] > 0.002:
            out.append((b, float(f[-1]), k))
    out.sort(key=lambda t: -(t[0][2] - t[0][0]) * (t[0][3] - t[0][1]))
    return out


def geometry(k, w, h):
    """From the five landmarks: eye distance in pixels, how far the head is turned (0 = facing the camera,
    about 0.5 = well turned, more = profile), the tilt of the eye line in degrees, and the eye midpoint."""
    import math
    (ax, ay), (bx, by), (nx, ny) = [(k[i] * w, k[i + 1] * h) for i in (0, 2, 4)]
    d = math.hypot(bx - ax, by - ay) or 1e-6
    mx, my = (ax + bx) / 2, (ay + by) / 2
    return {"d": d, "yaw": (nx - mx) / d, "angle": math.degrees(math.atan2(by - ay, bx - ax)), "mid": (mx, my)}


def eye_band(im, k, g=None, width=448):
    """A level crop of just the two eyes (about 2.4 eye-distances wide), enlarged: the model sees the eyelids
    and irises, not a whole small face."""
    from PIL import Image
    w, h = im.size
    g = g or geometry(k, w, h)
    d, (mx, my) = g["d"], g["mid"]
    r = 1.7 * d                                                      # half-side of a square around the eyes
    box = (int(mx - r), int(my - r), int(mx + r), int(my + r))
    sq = im.crop(box)                                                # PIL pads outside the picture with black
    sq = sq.rotate(g["angle"], resample=Image.BICUBIC)               # level the eyes
    cw, ch = 1.25 * d, 0.5 * d                                       # half-width and half-height of the band
    c = sq.size[0] / 2
    band = sq.crop((int(c - cw), int(c - ch), int(c + cw), int(c + ch)))
    return band.resize((width, max(1, round(width * band.size[1] / max(1, band.size[0])))), Image.LANCZOS)
