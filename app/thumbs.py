"""On-demand thumbnail and preview generation, cached on disk.

Originals are only ever READ. Generated JPEGs go in the thumbs folder
(outside the repo and outside the originals).
"""
import io
import os
import subprocess
import tempfile

from PIL import Image, ImageOps

try:  # HEIC/HEIF support is optional but needed for iPhone photos
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pillow_heif = None

SIZES = {"thumb": 360, "preview": 1600}
RAW_EXT = {".cr2", ".cr3", ".dng", ".nef", ".arw", ".orf", ".rw2"}
QUALITY = {"thumb": 80, "preview": 88}


def _cache_file(thumbs_dir, kind, fid):
    d = os.path.join(thumbs_dir, kind, str(fid // 1000))
    return d, os.path.join(d, f"{fid}.jpg")


def _raw_preview(abs_path):
    """Pull the JPEG preview embedded in a RAW file with exiftool."""
    for tag in ("-JpgFromRaw", "-PreviewImage", "-ThumbnailImage"):
        try:
            res = subprocess.run(["exiftool", "-b", tag, abs_path],
                                 capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if res.returncode == 0 and len(res.stdout) > 1000:
            try:
                return Image.open(io.BytesIO(res.stdout))
            except Exception:
                continue
    return None


def _video_frame(abs_path):
    """Grab one frame from a video with ffmpeg."""
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "f.jpg")
        for seek in ("0.5", "0"):
            try:
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", seek, "-i", abs_path,
                                "-frames:v", "1", out],
                               capture_output=True, timeout=45)
            except (OSError, subprocess.TimeoutExpired):
                return None
            if os.path.exists(out) and os.path.getsize(out) > 0:
                with open(out, "rb") as f:
                    return Image.open(io.BytesIO(f.read()))
    return None


def _open(abs_path, ext, is_video):
    if is_video:
        return _video_frame(abs_path)
    if ext in RAW_EXT:
        return _raw_preview(abs_path)
    return Image.open(abs_path)


def get_or_make(thumbs_dir, fid, abs_path, ext, is_video, kind="thumb"):
    """Return the path of a cached JPEG, creating it if needed. None on failure."""
    d, out = _cache_file(thumbs_dir, kind, fid)
    if os.path.exists(out):
        return out
    if os.path.exists(out + ".fail"):
        return None
    os.makedirs(d, exist_ok=True)
    try:
        im = _open(abs_path, ext, is_video)
        if im is None:
            raise ValueError("no image could be read")
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        size = SIZES[kind]
        im.thumbnail((size, size))
        tmp = f"{out}.{os.getpid()}.tmp"
        im.convert("RGB").save(tmp, "JPEG", quality=QUALITY[kind], optimize=True)
        os.replace(tmp, out)
        return out
    except Exception:
        try:
            open(out + ".fail", "w").close()
        except OSError:
            pass
        return None
