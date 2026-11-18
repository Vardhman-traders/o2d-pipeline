"""Face matching for attendance selfies, on this server (OpenCV: YuNet finds the face, SFace turns it into 128 numbers; nothing leaves the server).

  embed(image_bytes)        -> ("ok", [128 numbers]) | ("no_face", None) | ("bad_image", None) | ("unavailable", None)
  similarity(a, b)          -> cosine similarity, 1 = identical; the same person is usually 0.5 - 0.8, two people usually below 0.3
  check_enrolment(bytes)    -> (embedding or None, a sentence for the admin when the photo is not usable)

The two model files (about 39 MB) are not part of the code: they are downloaded once into data/models (or FACE_MODEL_DIR) and checked against
the fingerprints below. `python -m app.face_match download` fetches them (the Render build does this). When they cannot be had, or OpenCV is
missing, `embed` answers "unavailable" and the attendance page works as before - a face check never blocks anyone.
"""
import hashlib
import logging
import os
import sys
import threading
import urllib.request
from pathlib import Path

log = logging.getLogger("face_match")
ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = Path(os.environ.get("FACE_MODEL_DIR") or ROOT / "data" / "models")
BASE = "https://github.com/opencv/opencv_zoo/raw/main/models"
MODELS = {
    "yunet": ("yunet.onnx", f"{BASE}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
              "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    "sface": ("sface.onnx", f"{BASE}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
              "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
}
MAX_SIDE = 1024            # photos are shrunk to this before looking for a face (speed)
MIN_FACE_PX = 70           # a face smaller than this (in the shrunk photo) is too far away to be matched
DETECT_SCORE = 0.8
_lock = threading.Lock()   # OpenCV's detector / recogniser are not safe to share between threads
_loaded = None             # (cv2, numpy, detector, recogniser) once ready
_failed_until = 0.0        # after a failed load, do not retry for a while (a missing model should not slow down every punch)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_models() -> bool:
    """Fetch the model files that are missing or wrong. True when both are there and match."""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for name, url, sha in MODELS.values():
        path = MODEL_DIR / name
        if path.exists() and _sha256(path) == sha:
            continue
        log.info("downloading the face model %s", name)
        tmp = path.with_suffix(".part")
        try:
            with urllib.request.urlopen(url, timeout=120) as r, tmp.open("wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            if _sha256(tmp) != sha:
                tmp.unlink(missing_ok=True)
                log.error("the downloaded face model %s does not match its fingerprint", name)
                return False
            tmp.replace(path)
        except Exception:
            log.exception("could not download the face model %s", name)
            tmp.unlink(missing_ok=True)
            return False
    return True


def _load():
    """The detector and recogniser, loaded on first use. None when they cannot be had (OpenCV missing, no models, no network)."""
    global _loaded, _failed_until
    import time
    if _loaded:
        return _loaded
    if time.time() < _failed_until:
        return None
    try:
        import cv2
        import numpy as np
    except ImportError:
        _failed_until = time.time() + 3600
        log.warning("OpenCV is not installed: attendance face matching is off")
        return None
    if not download_models():
        _failed_until = time.time() + 600
        return None
    try:
        detector = cv2.FaceDetectorYN.create(str(MODEL_DIR / MODELS["yunet"][0]), "", (320, 320), DETECT_SCORE, 0.3, 50)
        recogniser = cv2.FaceRecognizerSF.create(str(MODEL_DIR / MODELS["sface"][0]), "")
    except Exception:
        log.exception("could not load the face models")
        _failed_until = time.time() + 600
        return None
    _loaded = (cv2, np, detector, recogniser)
    return _loaded


def available() -> bool:
    return _load() is not None


def _faces(image_bytes: bytes):
    """-> (state, img, faces): state 'ok' | 'bad_image' | 'unavailable'. faces = the detector's rows, biggest first (may be empty)."""
    with _lock:
        loaded = _load()
        if not loaded:
            return "unavailable", None, []
        cv2, np, detector, _ = loaded
        img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return "bad_image", None, []
        h, w = img.shape[:2]
        if max(h, w) > MAX_SIDE:
            scale = MAX_SIDE / max(h, w)
            img = cv2.resize(img, (int(w * scale), int(h * scale)))
            h, w = img.shape[:2]
        detector.setInputSize((w, h))
        _, found = detector.detect(img)
        faces = sorted([] if found is None else list(found), key=lambda r: float(r[2]) * float(r[3]), reverse=True)
        return "ok", img, faces


def embed(image_bytes: bytes) -> tuple[str, list | None]:
    """The face fingerprint (the biggest face in the photo)."""
    try:
        state, img, faces = _faces(image_bytes)
        if state != "ok":
            return state, None
        if not faces or float(faces[0][2]) < MIN_FACE_PX / 2:
            return "no_face", None
        with _lock:
            _, np, _, recogniser = _load()
            aligned = recogniser.alignCrop(img, faces[0])
            vec = recogniser.feature(aligned).flatten().astype(float)
        return "ok", [round(float(x), 6) for x in vec]
    except Exception:
        log.exception("face matching failed")
        return "unavailable", None


def similarity(a, b) -> float:
    """Cosine similarity of two fingerprints."""
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = sum(x * x for x in a) ** 0.5, sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def check_enrolment(image_bytes: bytes) -> tuple[list | None, str]:
    """For the admin adding a reference photo: (fingerprint, "") when it is usable, else (None, what to fix)."""
    try:
        state, img, faces = _faces(image_bytes)
    except Exception:
        log.exception("face matching failed")
        return None, "The photo could not be checked. Try again."
    if state == "unavailable":
        return None, "Face matching is not available on the server right now (the face models are not installed)."
    if state == "bad_image":
        return None, "That file is not a readable photo (use a JPEG, PNG or WebP picture)."
    if not faces:
        return None, "No face found. Use a clear, front-facing photo in good light."
    big = float(faces[0][2])
    if len(faces) > 1 and float(faces[1][2]) > 0.5 * big:
        return None, "More than one face in the photo. Use a photo of this person alone."
    if big < MIN_FACE_PX:
        return None, "The face is too small. Use a closer photo."
    if float(faces[0][14]) < 0.9:
        return None, "The face is not clear enough. Use a sharper, well lit, front-facing photo."
    state, vec = embed(image_bytes)
    return (vec, "") if vec else (None, "The photo could not be checked. Try again.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "download":
        logging.basicConfig(level=logging.INFO)
        ok = download_models()
        print("face models ready" if ok else "face models NOT available (attendance still works, without the face check)")
        sys.exit(0)
    print("usage: python -m app.face_match download")
