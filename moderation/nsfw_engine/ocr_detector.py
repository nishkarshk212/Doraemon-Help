"""
OCR text extraction from images using RapidOCR (ONNX).
Extracts text from image bytes, labels, and stickers without external binaries.
"""
from __future__ import annotations
import io
from typing import Optional
from pathlib import Path

from moderation.nsfw_engine.logger import get_logger

logger = get_logger("ocr_detector")

_RAPIDOCR_AVAILABLE = False
_ocr_engine = None

try:
    from rapidocr_onnxruntime import RapidOCR
    _ocr_engine = RapidOCR()
    _RAPIDOCR_AVAILABLE = True
    logger.info("RapidOCR (ONNX) loaded successfully.")
except Exception as e:
    logger.warning(f"RapidOCR not available: {e}")


def extract_text_from_image_bytes(image_bytes: bytes) -> Optional[str]:
    """
    Attempt to extract text from raw image bytes using RapidOCR.
    Returns extracted text string, or None if extraction fails/is unavailable.
    """
    if not image_bytes:
        return None

    if _RAPIDOCR_AVAILABLE and _ocr_engine is not None:
        try:
            payload = image_bytes
            try:
                from PIL import Image
                im = Image.open(io.BytesIO(image_bytes))
                if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info) or im.format == "WEBP":
                    rgba = im.convert("RGBA")
                    bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                    rgb_img = Image.alpha_composite(bg, rgba).convert("RGB")
                    buf = io.BytesIO()
                    rgb_img.save(buf, format="PNG")
                    payload = buf.getvalue()
            except Exception:
                pass

            result, _ = _ocr_engine(payload)
            if result:
                lines = [item[1] for item in result if len(item) > 1 and item[1]]
                text = " ".join(lines).strip()
                if text:
                    logger.debug(f"RapidOCR extracted: {text[:80]}")
                    return text
        except Exception as exc:
            logger.warning(f"RapidOCR extraction failed: {exc}")

    return None


def extract_text_from_image_file(path: str | Path) -> Optional[str]:
    """Convenience wrapper for file-path-based OCR."""
    try:
        with open(path, "rb") as f:
            return extract_text_from_image_bytes(f.read())
    except Exception as exc:
        logger.warning(f"Failed to read image file for OCR: {exc}")
        return None
