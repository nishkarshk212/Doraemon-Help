"""
Vision / image moderation detector.

Performs:
  1. Image hash (SHA-256) for cache-hit deduplication
  2. Skin-tone heuristic scoring on detected flesh-colored pixel ratio
  3. NudeNet ONNX deep-learning classifier (detects anatomy in B&W/grayscale/color)
  4. GIF and video frame-sampling via Pillow (GIF) and ffmpeg/OpenCV (video)
"""
from __future__ import annotations
import hashlib
import io
import os
import tempfile
from pathlib import Path
from typing import Optional

from moderation.nsfw_engine.schemas import CategoryScore, SeverityLevel
from moderation.nsfw_engine.logger import get_logger

logger = get_logger("vision_detector")

try:
    from PIL import Image, ImageOps
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False
    logger.warning("Pillow not installed — vision detection disabled.")

# ─── NudeNet ONNX classifier (optional but strongly recommended) ──────────────
_NUDENET_AVAILABLE = False
_nudenet_detector = None

try:
    from nudenet import NudeDetector  # type: ignore
    _nudenet_detector = NudeDetector()
    _NUDENET_AVAILABLE = True
    logger.info("NudeNet ONNX classifier loaded — B&W/grayscale sticker detection active.")
except Exception as _ne_err:
    logger.info(f"NudeNet not available ({_ne_err}); using skin-heuristic only.")

# NudeNet labels that indicate explicit sexual content
_NUDENET_NSFW_LABELS = {
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "ANUS_EXPOSED",
    "FEMALE_BREAST_COVERED",  # covered but still nsfw context
    "MALE_BREAST_EXPOSED",
}
_NUDENET_HIGH_LABELS = {
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "ANUS_EXPOSED",
}

# ─── MobileNetV2 ONNX classifier for alcohol, weapons, and drugs ──────────────
_MOBILENET_AVAILABLE = False
_mobilenet_session = None

try:
    _model_path = Path(__file__).parent / "models" / "mobilenetv2-7.onnx"
    if _model_path.exists():
        import onnxruntime as ort
        _mobilenet_session = ort.InferenceSession(str(_model_path))
        _MOBILENET_AVAILABLE = True
        logger.info("MobileNetV2 ONNX classifier loaded — alcoholic product & weapon vision detection active.")
except Exception as _m_err:
    logger.warning(f"MobileNetV2 classifier could not be loaded: {_m_err}")

# ImageNet class indices for alcohol, drugs, weapons
_ALCOHOL_CLASS_INDICES = {
    440: ("beer bottle", 0.92, SeverityLevel.HIGH),
    441: ("beer glass", 0.90, SeverityLevel.HIGH),
    503: ("cocktail shaker", 0.85, SeverityLevel.MEDIUM),
    572: ("goblet", 0.85, SeverityLevel.MEDIUM),
    901: ("whiskey jug", 0.92, SeverityLevel.HIGH),
    907: ("wine bottle", 0.92, SeverityLevel.HIGH),
    966: ("red wine", 0.90, SeverityLevel.HIGH),
    455: ("bottlecap", 0.82, SeverityLevel.MEDIUM),
    737: ("pop bottle", 0.80, SeverityLevel.LOW),
}

_WEAPON_CLASS_INDICES = {
    413: ("assault rifle", 0.95, SeverityLevel.CRITICAL),
    763: ("revolver", 0.92, SeverityLevel.HIGH),
    764: ("rifle", 0.92, SeverityLevel.HIGH),
}

_DRUG_CLASS_INDICES = {
    720: ("pill bottle", 0.88, SeverityLevel.HIGH),
    845: ("syringe", 0.90, SeverityLevel.HIGH),
}


# ─── Skin-tone heuristic ─────────────────────────────────────────────────────

def _skin_pixel_ratio(img) -> float:
    """
    Return fraction (0.0–1.0) of pixels that fall within a broad skin-tone HSV range.
    This is a fast, offline heuristic — not a deep-learning classifier.
    """
    try:
        rgb = img.convert("RGB").resize((128, 128))
        pixels = list(rgb.getdata())
        total = len(pixels)
        skin_count = 0
        for r, g, b in pixels:
            # Broad skin-tone range in RGB
            if (r > 95 and g > 40 and b > 20 and
                    max(r, g, b) - min(r, g, b) > 15 and
                    abs(r - g) > 15 and r > g and r > b):
                skin_count += 1
        return skin_count / total
    except Exception:
        return 0.0


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def analyze_image_bytes(
    image_bytes: bytes,
    enabled_categories: dict,
    threshold: float = 0.65,
) -> list[CategoryScore]:
    """
    Analyze raw image bytes for NSFW, alcoholic products, weapons, and drugs.
    Uses:
      - NudeNet ONNX (shape-based anatomy detection) + skin-tone heuristic
      - MobileNetV2 ONNX (detects beer bottles, wine bottles, glasses, weapons, drugs)
    Returns a list of CategoryScore objects.
    """
    if not _PIL_AVAILABLE:
        return []

    scores: list[CategoryScore] = []

    if not image_bytes:
        return scores

    # ── 1. Sexual NSFW Analysis ───────────────────────────────────────────────
    if enabled_categories.get("sexual_nsfw", True):
        best_conf = 0.0
        best_reason = ""
        best_sev = SeverityLevel.NONE

        if _NUDENET_AVAILABLE and _nudenet_detector is not None:
            try:
                suffix = ".jpg"
                if image_bytes[:4] == b"\x89PNG":
                    suffix = ".png"
                elif image_bytes[:6] in (b"GIF87a", b"GIF89a"):
                    suffix = ".gif"
                elif image_bytes[:4] == b"RIFF" or image_bytes[8:12] == b"WEBP":
                    suffix = ".webp"

                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp.write(image_bytes)
                    tmp_path = tmp.name

                try:
                    detections = _nudenet_detector.detect(tmp_path)
                finally:
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass

                if detections:
                    high_labels = [d for d in detections if d.get("class") in _NUDENET_HIGH_LABELS]
                    any_nsfw = [d for d in detections if d.get("class") in _NUDENET_NSFW_LABELS]

                    if high_labels:
                        top = max(high_labels, key=lambda d: d.get("score", 0))
                        raw_score = float(top.get("score", 0.0))
                        # Explicit exposed genitalia/breasts/buttocks with >=0.20 score are definitive NSFW
                        if raw_score >= 0.20:
                            conf = max(min(raw_score * 2.0, 0.99), 0.88)
                        else:
                            conf = raw_score * 1.5
                        label = top["class"].replace("_", " ").lower()
                        best_conf = max(best_conf, round(conf, 4))
                        best_reason = f"NudeNet: {label} ({conf:.0%})"
                        best_sev = SeverityLevel.HIGH
                    elif any_nsfw:
                        top = max(any_nsfw, key=lambda d: d.get("score", 0))
                        raw_score = float(top.get("score", 0.0))
                        if raw_score >= 0.22:
                            conf = max(min(raw_score * 1.8, 0.95), 0.75)
                        else:
                            conf = raw_score
                        label = top["class"].replace("_", " ").lower()
                        best_conf = max(best_conf, round(conf, 4))
                        best_reason = f"NudeNet: {label} ({conf:.0%})"
                        best_sev = SeverityLevel.MEDIUM

                # If raw pass was below threshold, test an autocontrast-enhanced version
                # to catch dull, faded, washed-out, or dark sticker imagery
                if best_conf < threshold:
                    try:
                        pil_img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
                        enhanced_pil = ImageOps.autocontrast(pil_img, cutoff=2)
                        buf = io.BytesIO()
                        enhanced_pil.save(buf, format="PNG")
                        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_enh:
                            tmp_enh.write(buf.getvalue())
                            tmp_enh_path = tmp_enh.name
                        try:
                            enh_detections = _nudenet_detector.detect(tmp_enh_path)
                        finally:
                            try:
                                os.unlink(tmp_enh_path)
                            except Exception:
                                pass
                        if enh_detections:
                            enh_high = [d for d in enh_detections if d.get("class") in _NUDENET_HIGH_LABELS]
                            enh_any = [d for d in enh_detections if d.get("class") in _NUDENET_NSFW_LABELS]
                            if enh_high:
                                top = max(enh_high, key=lambda d: d.get("score", 0))
                                raw_score = float(top.get("score", 0.0))
                                if raw_score >= 0.20:
                                    conf = max(min(raw_score * 2.0, 0.99), 0.88)
                                    if conf > best_conf:
                                        best_conf = round(conf, 4)
                                        best_reason = f"NudeNet (enhanced): {top['class'].replace('_', ' ').lower()} ({conf:.0%})"
                                        best_sev = SeverityLevel.HIGH
                            elif enh_any:
                                top = max(enh_any, key=lambda d: d.get("score", 0))
                                raw_score = float(top.get("score", 0.0))
                                if raw_score >= 0.22:
                                    conf = max(min(raw_score * 1.8, 0.95), 0.75)
                                    if conf > best_conf:
                                        best_conf = round(conf, 4)
                                        best_reason = f"NudeNet (enhanced): {top['class'].replace('_', ' ').lower()} ({conf:.0%})"
                                        best_sev = SeverityLevel.MEDIUM
                    except Exception as enh_err:
                        logger.debug(f"Dull image contrast pass skipped: {enh_err}")
            except Exception as exc:
                logger.warning(f"NudeNet analysis failed: {exc}")

        # Skin-tone heuristic
        try:
            img = Image.open(io.BytesIO(image_bytes))
            skin_ratio = _skin_pixel_ratio(img)
            if skin_ratio >= 0.55:
                skin_conf = min(0.60 + (skin_ratio - 0.55) * 2.0, 0.92)
                if skin_conf > best_conf:
                    best_conf = round(skin_conf, 4)
                    best_reason = f"High skin-pixel ratio: {skin_ratio:.2f}"
                    best_sev = SeverityLevel.HIGH if skin_conf >= 0.80 else SeverityLevel.MEDIUM
        except Exception as exc:
            logger.warning(f"Skin heuristic failed ({len(image_bytes)} bytes): {exc}")

        triggered = best_conf >= threshold
        scores.append(CategoryScore(
            category="sexual_nsfw",
            triggered=triggered,
            confidence=best_conf,
            severity=best_sev if triggered else SeverityLevel.NONE,
            reason=best_reason if triggered else "",
        ))

    # ── 2. MobileNetV2 Vision Classification (Alcohol, Weapons, Drugs) ────────
    check_alcohol = enabled_categories.get("drugs_alcohol", True)
    check_weapons = enabled_categories.get("weapons", True)

    if (check_alcohol or check_weapons) and _MOBILENET_AVAILABLE and _mobilenet_session is not None:
        try:
            import numpy as np
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB").resize((224, 224))
            img_data = np.array(img).astype("float32") / 255.0
            mean = np.array([0.485, 0.456, 0.406], dtype="float32")
            std = np.array([0.229, 0.224, 0.225], dtype="float32")
            img_data = (img_data - mean) / std
            img_data = img_data.transpose((2, 0, 1))
            img_data = np.expand_dims(img_data, axis=0)

            outputs = _mobilenet_session.run(None, {"data": img_data})[0][0]
            exp_scores = np.exp(outputs - np.max(outputs))
            probs = exp_scores / np.sum(exp_scores)
            top_indices = np.argsort(probs)[-10:][::-1]

            # Alcohol & Drug check
            if check_alcohol:
                for idx in top_indices:
                    prob = float(probs[idx])
                    if idx in _ALCOHOL_CLASS_INDICES and prob >= 0.08:
                        name, conf_weight, sev = _ALCOHOL_CLASS_INDICES[idx]
                        score_conf = max(conf_weight * min(prob * 2.5, 1.0), 0.72)
                        if score_conf >= threshold:
                            scores.append(CategoryScore(
                                category="drugs_alcohol",
                                triggered=True,
                                confidence=round(score_conf, 4),
                                severity=sev,
                                reason=f"Alcoholic product detected: {name} ({prob:.0%})",
                            ))
                            break
                    elif idx in _DRUG_CLASS_INDICES and prob >= 0.10:
                        name, conf_weight, sev = _DRUG_CLASS_INDICES[idx]
                        score_conf = max(conf_weight * min(prob * 2.0, 1.0), 0.72)
                        if score_conf >= threshold:
                            scores.append(CategoryScore(
                                category="drugs_alcohol",
                                triggered=True,
                                confidence=round(score_conf, 4),
                                severity=sev,
                                reason=f"Drug item detected: {name} ({prob:.0%})",
                            ))
                            break

            # Weapons check
            if check_weapons:
                for idx in top_indices:
                    prob = float(probs[idx])
                    if idx in _WEAPON_CLASS_INDICES and prob >= 0.10:
                        name, conf_weight, sev = _WEAPON_CLASS_INDICES[idx]
                        score_conf = max(conf_weight * min(prob * 2.0, 1.0), 0.75)
                        if score_conf >= threshold:
                            scores.append(CategoryScore(
                                category="weapons",
                                triggered=True,
                                confidence=round(score_conf, 4),
                                severity=sev,
                                reason=f"Weapon detected: {name} ({prob:.0%})",
                            ))
                            break
        except Exception as exc:
            logger.warning(f"MobileNet vision classification error: {exc}")

    return scores


def sample_gif_frames(gif_bytes: bytes, n_frames: int = 6) -> list[bytes]:
    """Extract up to n_frames evenly sampled from a GIF, returning JPEG bytes."""
    if not _PIL_AVAILABLE:
        return []
    frames: list[bytes] = []
    try:
        gif = Image.open(io.BytesIO(gif_bytes))
        total = getattr(gif, "n_frames", 1)
        indices = _even_indices(total, n_frames)
        for idx in indices:
            try:
                gif.seek(idx)
                buf = io.BytesIO()
                gif.convert("RGB").save(buf, format="JPEG")
                frames.append(buf.getvalue())
            except EOFError:
                break
    except Exception as exc:
        logger.warning(f"GIF frame sampling failed: {exc}")
    return frames


def sample_video_frames(video_path: str, n_frames: int = 10) -> list[bytes]:
    """
    Extract frames from a video using OpenCV (if available) or ffmpeg subprocess.
    Falls back gracefully with ffmpeg or empty list if neither is available.
    """
    frames: list[bytes] = []
    try:
        import cv2  # type: ignore
        cap = cv2.VideoCapture(video_path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total > 0:
            indices = _even_indices(total, n_frames)
            for idx in indices:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ret, frame = cap.read()
                if not ret:
                    continue
                _, buf = cv2.imencode(".jpg", frame)
                frames.append(buf.tobytes())
        else:
            # Crucial for .webm video stickers where frame count is unknown (-1 or 0)
            step_count = 0
            while len(frames) < n_frames:
                ret, frame = cap.read()
                if not ret:
                    break
                if step_count % 2 == 0:
                    _, buf = cv2.imencode(".jpg", frame)
                    frames.append(buf.tobytes())
                step_count += 1
        cap.release()
    except Exception as exc:
        logger.warning(f"OpenCV video frame sampling error: {exc}")

    # Fallback to ffmpeg subprocess if OpenCV yielded 0 frames
    if not frames:
        frames = _ffmpeg_sample(video_path, n_frames)

    return frames


def _ffmpeg_sample(video_path: str, n_frames: int) -> list[bytes]:
    import subprocess, shutil
    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg not available for video frame sampling.")
        return []
    frames = []
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            out_pattern = os.path.join(tmpdir, "frame%03d.jpg")
            subprocess.run(
                ["ffmpeg", "-i", video_path, "-vf", f"select=not(mod(n\\,10))",
                 "-vframes", str(n_frames), "-q:v", "2", out_pattern],
                capture_output=True, timeout=30,
            )
            for f in sorted(Path(tmpdir).glob("*.jpg"))[:n_frames]:
                frames.append(f.read_bytes())
    except Exception as exc:
        logger.warning(f"ffmpeg frame sampling failed: {exc}")
    return frames


def _even_indices(total: int, n: int) -> list[int]:
    if total <= n:
        return list(range(total))
    step = total // n
    return [step * i for i in range(n)]
