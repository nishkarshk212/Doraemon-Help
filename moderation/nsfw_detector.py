import os
import base64
import requests
import json
import logging
import subprocess
import hashlib
import re
from typing import Tuple, Optional

logger = logging.getLogger(__name__)

try:
    from PIL import Image
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False
    logger.warning("Pillow not installed - vision skin heuristic disabled.")

# Fallback API key if not found in environmental config.
DEFAULT_OPENROUTER_KEY = ""

# SHA-256 result cache: {sha256_hash: (is_nsfw, score, reason)}
_NSFW_HASH_CACHE = {}

# ─── Text / Caption NSFW Patterns (from NSFW-Detector) ───────────────────────
_TEXT_NSFW_PATTERNS = [
    # Explicit sexual terms
    (re.compile(r"\bporn(?:ography|o)?\b", re.IGNORECASE), 0.95, "pornography"),
    (re.compile(r"\bxxx\b", re.IGNORECASE), 0.92, "xxx"),
    (re.compile(r"\bnude[sd]?\b|\bnudity\b", re.IGNORECASE), 0.88, "nudity"),
    (re.compile(r"\bsex\s*(?:tape|video|clip|chat|call|scandal)\b", re.IGNORECASE), 0.92, "sex_video"),
    (re.compile(r"\bnaked\b", re.IGNORECASE), 0.82, "naked"),
    (re.compile(r"\bboobs?\b|\btits?\b", re.IGNORECASE), 0.85, "explicit_body_part"),
    (re.compile(r"\bd[i1]ck\b|\bcock\b|\bpenis\b|\bvagina\b|\bpussy\b", re.IGNORECASE), 0.92, "sexual_organ"),
    (re.compile(r"\bfuck(?:ing|er|s)?\b", re.IGNORECASE), 0.75, "profanity"),
    (re.compile(r"\bslut\b|\bwhore\b|\bescort\b|\bprostitut", re.IGNORECASE), 0.88, "solicitation"),
    (re.compile(r"\bonlyfans?\b", re.IGNORECASE), 0.80, "adult_link"),
    (re.compile(r"\bsend\s*(?:nudes?|pics?)\b", re.IGNORECASE), 0.90, "solicitation_nudes"),
    # Hinglish explicit sexual slurs
    (re.compile(r"(?<![a-z])(?:chutiya|chut|randi|landu?|gandu|bhen_chod|mader_chod|maaki|bhosdike?|bosadike?)(?![a-z])", re.IGNORECASE), 0.90, "hinglish_profanity"),
    # Severe gore/violence
    (re.compile(r"\bbeheading\b|\bmutilat|\bgore\b|\bbloodsh[ae]d\b", re.IGNORECASE), 0.92, "violence_gore"),
]

def get_openrouter_api_key():
    return os.getenv("OPENROUTER_API_KEY") or DEFAULT_OPENROUTER_KEY

def _sha256_file(file_path: str) -> str:
    """Calculate SHA-256 hash of a file for cache lookups."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()

def _skin_pixel_ratio(img) -> float:
    """
    Calculate fraction (0.0-1.0) of pixels that fall within a skin-tone RGB range.
    Fast, offline heuristic from NSFW-Detector.
    """
    try:
        rgb = img.convert("RGB").resize((128, 128))
        pixels = list(rgb.getdata())
        total = len(pixels)
        if total == 0:
            return 0.0
        skin_count = 0
        for r, g, b in pixels:
            if (r > 95 and g > 40 and b > 20 and
                    max(r, g, b) - min(r, g, b) > 15 and
                    abs(r - g) > 15 and r > g and r > b):
                skin_count += 1
        return skin_count / total
    except Exception as e:
        logger.warning(f"Error calculating skin ratio: {e}")
        return 0.0

def check_nsfw_text(text: str) -> Tuple[bool, float, Optional[str]]:
    """
    Check if a text string or caption contains explicit NSFW or severe prohibited patterns.
    Returns (is_nsfw, score, reason).
    """
    if not text:
        return False, 0.0, None

    cleaned_text = text.strip()
    for pattern, score, label in _TEXT_NSFW_PATTERNS:
        if pattern.search(cleaned_text):
            logger.info(f"NSFW text detected: pattern='{label}', score={score}")
            return True, score, f"text_{label}"

    return False, 0.0, None

def extract_frame_from_video(file_path: str) -> Optional[str]:
    """
    Extracts the first representative frame from a video or sticker (.webm, .mp4, .gif, .mkv) using ffmpeg or Pillow.
    Returns the path to the extracted image (.jpg), or None if it fails.
    """
    if not os.path.exists(file_path):
        logger.error(f"File not found: {file_path}")
        return None

    output_path = file_path + ".jpg"

    # Pillow fallback for GIF
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".gif" and _PIL_AVAILABLE:
        try:
            with Image.open(file_path) as im:
                im.seek(0)
                im.convert("RGB").save(output_path, "JPEG")
                return output_path
        except Exception as e:
            logger.warning(f"Pillow GIF frame extraction failed: {e}")

    # FFmpeg extraction
    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-ss", "00:00:01",
            "-i", file_path,
            "-vframes", "1",
            "-f", "image2",
            output_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            # Try again from beginning (00:00:00) for short stickers/clips
            cmd[2] = "00:00:00"
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return output_path
        else:
            return None
    except FileNotFoundError:
        logger.warning("ffmpeg not installed on system. Video/webm frame extraction skipped.")
        return None
    except Exception as e:
        logger.error(f"Unexpected error in ffmpeg extraction: {e}")
        return None

def extract_frame_from_webm(file_path):
    """Backward compatible alias for extract_frame_from_video."""
    return extract_frame_from_video(file_path)

def check_nsfw_via_openrouter(file_path: str, threshold: float = 0.7) -> Tuple[bool, Optional[float]]:
    """
    Checks if an image/sticker at file_path contains NSFW/adult content using OpenRouter Reranker API.
    Returns (is_nsfw: bool, score: float or None).
    """
    try:
        if not os.path.exists(file_path):
            logger.error(f"File not found: {file_path}")
            return False, None

        ext = os.path.splitext(file_path)[1].lower()
        frame_created = False
        if ext in [".webm", ".mp4", ".gif", ".mkv"]:
            frame_path = extract_frame_from_video(file_path)
            if not frame_path:
                logger.error(f"Failed to extract frame from {ext} file.")
                return False, None
            file_path_to_read = frame_path
            mime_type = "image/jpeg"
            frame_created = True
        elif ext == ".png":
            file_path_to_read = file_path
            mime_type = "image/png"
        elif ext == ".webp":
            file_path_to_read = file_path
            mime_type = "image/webp"
        else:
            file_path_to_read = file_path
            mime_type = "image/jpeg"

        with open(file_path_to_read, "rb") as f:
            encoded_image = base64.b64encode(f.read()).decode("utf-8")

        if frame_created and os.path.exists(file_path_to_read):
            try:
                os.remove(file_path_to_read)
            except Exception:
                pass

        api_key = get_openrouter_api_key()
        if not api_key:
            logger.info("OpenRouter API key not configured; skipping AI vision rerank.")
            return False, None

        data_uri = f"data:{mime_type};base64,{encoded_image}"
        query = "sexually explicit, nude, nsfw, pornographic, adult content, nudity, sexual organs"

        payload = {
            "model": "nvidia/llama-nemotron-rerank-vl-1b-v2:free",
            "query": query,
            "documents": [
                {"image": data_uri}
            ],
            "top_n": 1
        }

        response = requests.post(
            url="https://openrouter.ai/api/v1/rerank",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            data=json.dumps(payload),
            timeout=12
        )

        if response.status_code != 200:
            logger.error(f"OpenRouter Rerank request failed: Status {response.status_code}, Response: {response.text}")
            return False, None

        results = response.json()
        if "results" in results and len(results["results"]) > 0:
            result = results["results"][0]
            relevance_score = result.get("relevance_score", 0.0)
            logger.info(f"OpenRouter NSFW result - Score: {relevance_score} vs Threshold: {threshold}")
            return (relevance_score >= threshold), relevance_score

        return False, None

    except Exception as e:
        logger.error(f"Error checking NSFW status via OpenRouter: {e}")
        return False, None

def check_nsfw(
    file_path: Optional[str] = None,
    text: Optional[str] = None,
    threshold: float = 0.7
) -> Tuple[bool, float, str]:
    """
    Unified multi-layered NSFW evaluation combining:
      1. Text pattern scanner (sexual terms, Hinglish profanity, gore)
      2. SHA-256 deduplication cache
      3. Fast skin-pixel ratio heuristic
      4. OpenRouter AI reranker (vision deep verification)

    Returns:
      (is_nsfw: bool, score: float, reason: str)
    """
    # 1. Text Check
    if text:
        is_text_nsfw, text_score, text_reason = check_nsfw_text(text)
        if is_text_nsfw:
            return True, text_score, text_reason

    if not file_path or not os.path.exists(file_path):
        return False, 0.0, "no_media"

    # 2. SHA-256 Cache Check
    file_hash = None
    try:
        file_hash = _sha256_file(file_path)
        if file_hash in _NSFW_HASH_CACHE:
            cached_result = _NSFW_HASH_CACHE[file_hash]
            logger.info(f"NSFW cache hit for {file_hash[:10]}: {cached_result}")
            return cached_result
    except Exception as e:
        logger.warning(f"Failed to calculate file hash: {e}")

    # 3. Vision Analysis: Skin tone heuristic
    skin_score = 0.0
    frame_path = None
    file_to_inspect = file_path
    ext = os.path.splitext(file_path)[1].lower()

    if ext in [".webm", ".mp4", ".gif", ".mkv"]:
        frame_path = extract_frame_from_video(file_path)
        if frame_path:
            file_to_inspect = frame_path

    if _PIL_AVAILABLE and os.path.exists(file_to_inspect):
        try:
            with Image.open(file_to_inspect) as img:
                skin_score = _skin_pixel_ratio(img)
                logger.info(f"Skin-tone pixel ratio for {os.path.basename(file_path)}: {skin_score:.3f}")
        except Exception as e:
            logger.warning(f"Could not compute skin-tone ratio: {e}")

    # Very high skin ratio (> 0.55) is heavily correlated with full nudity/exposure
    if skin_score >= 0.58:
        result = (True, skin_score, "high_skin_ratio")
        if file_hash:
            _NSFW_HASH_CACHE[file_hash] = result
        if frame_path and os.path.exists(frame_path):
            try: os.remove(frame_path)
            except Exception: pass
        return result

    # 4. OpenRouter AI Verification
    is_ai_nsfw, ai_score = check_nsfw_via_openrouter(file_path, threshold=threshold)
    if ai_score is not None:
        if is_ai_nsfw:
            result = (True, ai_score, "openrouter_vision_nsfw")
        else:
            result = (False, ai_score, "openrouter_vision_clean")
    else:
        # Fallback if OpenRouter unavailable: flag if skin tone was moderately high (> 0.42)
        if skin_score >= 0.42:
            result = (True, skin_score, "skin_heuristic_fallback")
        else:
            result = (False, skin_score, "clean")

    # Clean up extracted frame
    if frame_path and os.path.exists(frame_path):
        try: os.remove(frame_path)
        except Exception: pass

    # Cache result
    if file_hash:
        # Keep cache under 1000 items
        if len(_NSFW_HASH_CACHE) > 1000:
            _NSFW_HASH_CACHE.clear()
        _NSFW_HASH_CACHE[file_hash] = result

    return result
