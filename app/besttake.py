"""Best Take: build one picture from a burst in which everyone's eyes are open.

Used by scripts/best_take.py and by the Review page.  Real pixels only: a head with closed eyes is
replaced by the same person's head from another frame where their eyes are open.  Reads photos, never
changes them; writes new files into a derived folder.
"""
import json
import os

import cv2
import numpy as np
from PIL import Image, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pass

MAX_SIDE = 4000          # work at most this big (long side)
MIN_INLIERS = 25         # frames that cannot be lined up with the base are not used
MAX_MOVE = 0.6           # a head more than this many head-widths from where it was is not the same person


def load(archive, rel):
    """Upright RGB array, shrunk to MAX_SIDE; the scale used is returned too."""
    with Image.open(os.path.join(archive, rel)) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        ex = im.getexif()
        ex[0x0112] = 1                                  # pixels are already upright: do not rotate twice
        exif = ex.tobytes()
        s = min(1.0, MAX_SIDE / max(im.size))
        if s < 1:
            im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
        return np.asarray(im), exif


def align(donor, base):
    """3x3 matrix taking donor pixel coordinates to base coordinates, or None."""
    def gray(a):
        g = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)
        k = 1200 / max(g.shape)
        return cv2.resize(g, None, fx=k, fy=k) if k < 1 else g, min(k, 1.0)
    gd, kd = gray(donor)
    gb, kb = gray(base)
    orb = cv2.ORB_create(4000)
    kpd, dd = orb.detectAndCompute(gd, None)
    kpb, db = orb.detectAndCompute(gb, None)
    if dd is None or db is None or len(kpd) < 20 or len(kpb) < 20:
        return None
    good = []
    for pair in cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(dd, db, k=2):
        if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance:
            good.append(pair[0])
    if len(good) < MIN_INLIERS:
        return None
    src = np.float32([kpd[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([kpb[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 3.0)
    if H is None or int(mask.sum()) < MIN_INLIERS:
        return None
    Sd, Sb = np.diag([kd, kd, 1.0]), np.diag([kb, kb, 1.0])
    return np.linalg.inv(Sb) @ H @ Sd


def box_px(b, w, h):
    return [b[0] * w, b[1] * h, b[2] * w, b[3] * h]


def warp_box(b, H):
    pts = np.float32([[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]]).reshape(-1, 1, 2)
    q = cv2.perspectiveTransform(pts, H).reshape(-1, 2)
    return [q[:, 0].min(), q[:, 1].min(), q[:, 0].max(), q[:, 1].max()]


def centre(b):
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def refine_shift(base, donor, b):
    """Small extra sideways/up-down shift so this one head sits exactly on the base head."""
    h, w = base.shape[:2]
    pad = (b[2] - b[0]) * 0.25
    x1, y1, x2, y2 = (int(max(0, b[0] - pad)), int(max(0, b[1] - pad)),
                      int(min(w, b[2] + pad)), int(min(h, b[3] + pad)))
    if x2 - x1 < 16 or y2 - y1 < 16:
        return 0.0, 0.0
    pb = cv2.cvtColor(base[y1:y2, x1:x2], cv2.COLOR_RGB2GRAY).astype(np.float32)
    pd = cv2.cvtColor(donor[y1:y2, x1:x2], cv2.COLOR_RGB2GRAY).astype(np.float32)
    (dx, dy), _ = cv2.phaseCorrelate(pb, pd, cv2.createHanningWindow(pb.shape[::-1], cv2.CV_32F))
    lim = (b[2] - b[0]) * 0.2
    return float(np.clip(dx, -lim, lim)), float(np.clip(dy, -lim, lim))


def blend_head(base, donor, b):
    """Blend the head inside box b from donor into base (both already in the same coordinates)."""
    h, w = base.shape[:2]
    bw, bh = b[2] - b[0], b[3] - b[1]
    dx, dy = refine_shift(base, donor, b)
    donor = cv2.warpAffine(donor, np.float32([[1, 0, -dx], [0, 1, -dy]]), (w, h), flags=cv2.INTER_LINEAR,
                           borderMode=cv2.BORDER_REPLICATE)
    cx, cy = (b[0] + b[2]) / 2, b[1] + bh * 0.40       # the head sits in the upper part of the box
    rx, ry = bw * 0.40, bh * 0.38
    x1, y1 = int(max(0, cx - rx - 8)), int(max(0, cy - ry - 8))
    x2, y2 = int(min(w, cx + rx + 8)), int(min(h, cy + ry + 8))
    patch = donor[y1:y2, x1:x2].copy()
    mask = np.zeros(patch.shape[:2], np.uint8)
    cv2.ellipse(mask, (int(cx - x1), int(cy - y1)), (int(rx), int(ry)), 0, 0, 360, 255, -1)
    centre_pt = ((x1 + x2) // 2, (y1 + y2) // 2)
    try:
        return cv2.seamlessClone(patch, base, mask, centre_pt, cv2.NORMAL_CLONE)
    except cv2.error:                                   # fall back to a soft-edged paste
        soft = cv2.GaussianBlur(mask, (0, 0), max(3, rx * 0.08)).astype(np.float32)[..., None] / 255
        out = base.copy()
        out[y1:y2, x1:x2] = (patch * soft + out[y1:y2, x1:x2] * (1 - soft)).astype(np.uint8)
        return out


def open_state(det):
    return [f for f in det if f["e"] == "closed"], [f for f in det if f["e"] == "open"]


def group_members(sim, gid):
    rows = sim.execute(
        "SELECT g.path, v.detail, h.sharp FROM sim_group g "
        "JOIN vision v ON v.path = g.path AND v.status = 'ok' AND v.detail IS NOT NULL "
        "LEFT JOIN hashes h ON h.path = g.path WHERE g.group_id = ?", (gid,)).fetchall()
    return [{"path": p, "faces": json.loads(d), "sharp": s or 0} for p, d, s in rows]


def make(archive, out_dir, members, log=print):
    for m in members:
        m["closed"] = sum(1 for f in m["faces"] if f["e"] == "closed")
    seen = sum(len(m["faces"]) for m in members)
    if seen >= 3 and sum(m["closed"] for m in members) > 0.7 * seen:
        log(f"Not trusting the eye data here: {sum(m['closed'] for m in members)} of {seen} faces are marked "
            "closed, which usually means the model could not see the eyes (sunglasses, looking down, tiny faces).")
        return None
    base_m = min(members, key=lambda m: (m["closed"], -m["sharp"]))
    base, exif = load(archive, base_m["path"])
    h, w = base.shape[:2]
    result, swaps = base.copy(), []
    need = [f for f in base_m["faces"] if f["e"] == "closed"]
    log(f"Base frame {base_m['path']}: {len(base_m['faces'])} faces, {len(need)} with closed eyes")
    if not need:
        log("Nothing to fix: every face in the best frame already has its eyes open.")
        return None
    warped = {}
    for m in members:
        if m is base_m:
            continue
        img, _ = load(archive, m["path"])
        if img.shape[:2] != (h, w):
            img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
        H = align(img, base)
        if H is None:
            log(f"  could not line up {m['path']}: not used")
            continue
        warped[m["path"]] = (cv2.warpPerspective(img, H, (w, h), flags=cv2.INTER_LINEAR,
                                                 borderMode=cv2.BORDER_REPLICATE), H, m)
    for face in need:
        bb = box_px(face["b"], w, h)
        bw = bb[2] - bb[0]
        best = None
        for path, (img, H, m) in warped.items():
            for f in m["faces"]:
                if f["e"] != "open":
                    continue
                db = warp_box(box_px(f["b"], w, h), H)
                (cx, cy), (dcx, dcy) = centre(bb), centre(db)
                dist = ((cx - dcx) ** 2 + (cy - dcy) ** 2) ** 0.5
                ratio = (db[2] - db[0]) / bw if bw else 0
                if dist < MAX_MOVE * bw and 0.7 < ratio < 1.4 and (best is None or dist < best[0]):
                    best = (dist, path, img)
        if best is None:
            log(f"  face at {int(bb[0])},{int(bb[1])}: no other frame has this person with eyes open")
            continue
        result = blend_head(result, best[2], bb)
        swaps.append({"box": [round(v, 4) for v in face["b"]], "from": best[1]})
        log(f"  face at {int(bb[0])},{int(bb[1])}: head taken from {best[1]}")
    if not swaps:
        log("No head could be swapped.")
        return None
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(base_m["path"]))[0]
    out = os.path.join(out_dir, f"{stem}_besttake.jpg")
    kw = {"exif": exif} if exif else {}
    Image.fromarray(result).save(out, "JPEG", quality=95, **kw)
    # side-by-side check picture
    left, right = base.copy(), result.copy()
    for s in swaps:
        b = [int(v) for v in box_px(s["box"], w, h)]
        cv2.rectangle(right, (b[0], b[1]), (b[2], b[3]), (255, 220, 0), max(3, w // 400))
    k = min(1.0, 1100 / w)
    pair = np.hstack([cv2.resize(left, None, fx=k, fy=k, interpolation=cv2.INTER_AREA),
                      cv2.resize(right, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)])
    cmp_path = os.path.join(out_dir, f"{stem}_compare.jpg")
    Image.fromarray(pair).save(cmp_path, "JPEG", quality=88)
    with open(os.path.join(out_dir, f"{stem}_besttake.json"), "w") as f:
        json.dump({"base": base_m["path"], "swaps": swaps, "made_from": [m["path"] for m in members]}, f, indent=1)
    log(f"Wrote {out}")
    return {"out": out, "compare": cmp_path, "base": base_m["path"], "swaps": swaps,
            "made_from": [m["path"] for m in members]}


