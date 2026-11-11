"""
Moderation Pipeline Orchestrator.

Combines text, vision, OCR, and audio detectors into a unified result.
Also handles caching by SHA-256 hash of raw media bytes.
"""
from __future__ import annotations
import hashlib
import os
import asyncio
import tempfile
from pathlib import Path
from typing import Optional

from moderation.nsfw_engine.schemas import ModerationResult, CategoryScore, SeverityLevel
from moderation.nsfw_engine.text_detector import analyze_text
from moderation.nsfw_engine.vision_detector import (
    analyze_image_bytes, sample_gif_frames, sample_video_frames, _sha256_bytes
)
from moderation.nsfw_engine.ocr_detector import extract_text_from_image_bytes
from moderation.nsfw_engine.audio_detector import transcribe_audio
from moderation.nsfw_engine.cache import cache
from moderation.nsfw_engine.logger import get_logger
from moderation.nsfw_engine.config import settings

logger = get_logger("pipeline")

# Content-type groups
IMAGE_TYPES = {"photo", "sticker", "image"}
VIDEO_TYPES = {"video", "video_note", "animation", "gif"}
AUDIO_TYPES = {"audio", "voice"}
DOCUMENT_TYPES = {"document"}


async def analyze_text_content(
    text: str,
    enabled_categories: dict,
    threshold: float = 0.65,
) -> ModerationResult:
    """Analyze plain text content."""
    loop = asyncio.get_event_loop()
    # Run blocking regex in thread pool
    scores, language = await loop.run_in_executor(
        None, analyze_text, text, enabled_categories, threshold
    )
    result = ModerationResult(
        categories=scores,
        detected_language=language,
        content_type="text",
    )
    return result.compute_aggregate()


async def analyze_media_bytes(
    media_bytes: bytes,
    content_type: str,
    enabled_categories: dict,
    threshold: float = 0.65,
    caption: Optional[str] = None,
) -> ModerationResult:
    """Analyze raw media bytes (image, GIF, video, audio, document)."""
    loop = asyncio.get_event_loop()
    normalized_type = _normalize_content_type(content_type)

    # ── Cache check (scoped by hash, type, threshold, enabled categories, caption) ──
    cat_sig = ",".join(sorted(k for k, v in enabled_categories.items() if v))
    cap_sig = hashlib.md5(caption.strip().encode("utf-8")).hexdigest()[:8] if caption and caption.strip() else ""
    cache_key = f"{_sha256_bytes(media_bytes)}:{normalized_type}:{threshold:.2f}:{cat_sig}:{cap_sig}"
    cached = cache.get(cache_key)
    if cached:
        logger.debug(f"Cache hit for media hash {cache_key[:16]}…")
        return cached

    all_scores: list[CategoryScore] = []
    detected_language = "unknown"
    notes = []

    if settings.TEMP_MEDIA_DIR:
        os.makedirs(settings.TEMP_MEDIA_DIR, exist_ok=True)

    async def _scan_frames(frame_list: list[bytes], label: str) -> None:
        nonlocal detected_language
        notes.append(f"{label}:{len(frame_list)}")
        for fb in frame_list:
            vision_scores = await loop.run_in_executor(
                None, analyze_image_bytes, fb, enabled_categories, threshold
            )
            _merge_scores(all_scores, vision_scores)
            # Early break if high-confidence violation found across frames
            if any(s.triggered and s.confidence >= 0.88 for s in all_scores):
                break

        # Run OCR on representative frames (first & middle) if nothing triggered yet
        if settings.OCR_ENABLED and frame_list and not any(s.triggered for s in all_scores):
            sample_ocr = [frame_list[0]]
            if len(frame_list) > 2:
                sample_ocr.append(frame_list[len(frame_list) // 2])
            for ocr_fb in sample_ocr:
                ocr_text = await loop.run_in_executor(
                    None, extract_text_from_image_bytes, ocr_fb
                )
                if ocr_text:
                    notes.append(f"ocr_extracted:{len(ocr_text)}chars")
                    text_scores, lang = await loop.run_in_executor(
                        None, analyze_text, ocr_text, enabled_categories, threshold
                    )
                    if detected_language == "unknown":
                        detected_language = lang
                    _merge_scores(all_scores, text_scores)

    # ── Image / Sticker ────────────────────────────────────────────────────
    if normalized_type in IMAGE_TYPES:
        # Check if animated WebP sticker/image (RIFF....ANIM)
        is_anim_webp = (
            len(media_bytes) > 64
            and media_bytes[:4] == b"RIFF"
            and b"WEBP" in media_bytes[:20]
            and b"ANIM" in media_bytes[:80]
        )
        if is_anim_webp:
            frame_bytes_list = await loop.run_in_executor(
                None, sample_gif_frames, media_bytes, settings.MEDIA_FRAME_SAMPLE_COUNT
            )
            if frame_bytes_list:
                await _scan_frames(frame_bytes_list, "anim_webp_frames")
        else:
            # Static photo or sticker vision analysis
            vision_scores = await loop.run_in_executor(
                None, analyze_image_bytes, media_bytes, enabled_categories, threshold
            )
            all_scores.extend(vision_scores)
            notes.append("vision_analysis")

            # OCR: extract embedded text
            if settings.OCR_ENABLED:
                ocr_text = await loop.run_in_executor(
                    None, extract_text_from_image_bytes, media_bytes
                )
                if ocr_text:
                    notes.append(f"ocr_extracted:{len(ocr_text)}chars")
                    text_scores, lang = await loop.run_in_executor(
                        None, analyze_text, ocr_text, enabled_categories, threshold
                    )
                    detected_language = lang
                    _merge_scores(all_scores, text_scores)

    # ── GIF / Animation ────────────────────────────────────────────────────
    elif normalized_type in {"animation", "gif"}:
        if media_bytes[:4] == b"GIF8":
            frame_bytes_list = await loop.run_in_executor(
                None, sample_gif_frames, media_bytes, settings.MEDIA_FRAME_SAMPLE_COUNT
            )
            await _scan_frames(frame_bytes_list, "gif_frames")
        else:
            # Telegram animation (MP4 or WEBM)
            suffix = ".webm" if media_bytes.startswith(b"\x1a\x45\xdf\xa3") else ".mp4"
            with tempfile.NamedTemporaryFile(
                suffix=suffix, dir=settings.TEMP_MEDIA_DIR, delete=False
            ) as tmp:
                tmp.write(media_bytes)
                tmp_path = tmp.name
            try:
                frame_bytes_list = await loop.run_in_executor(
                    None, sample_video_frames, tmp_path, settings.MEDIA_FRAME_SAMPLE_COUNT
                )
                await _scan_frames(frame_bytes_list, "animation_frames")
            finally:
                _secure_delete(tmp_path)

    # ── Video ──────────────────────────────────────────────────────────────
    elif normalized_type in {"video", "video_note"}:
        suffix = ".webm" if media_bytes.startswith(b"\x1a\x45\xdf\xa3") else ".mp4"
        with tempfile.NamedTemporaryFile(
            suffix=suffix, dir=settings.TEMP_MEDIA_DIR, delete=False
        ) as tmp:
            tmp.write(media_bytes)
            tmp_path = tmp.name
        try:
            frame_bytes_list = await loop.run_in_executor(
                None, sample_video_frames, tmp_path, settings.MEDIA_FRAME_SAMPLE_COUNT
            )
            await _scan_frames(frame_bytes_list, "video_frames")

            # STT on video
            if settings.STT_ENABLED:
                transcript = await loop.run_in_executor(None, transcribe_audio, tmp_path)
                if transcript:
                    notes.append(f"stt_transcript:{len(transcript)}chars")
                    text_scores, lang = await loop.run_in_executor(
                        None, analyze_text, transcript, enabled_categories, threshold
                    )
                    detected_language = lang
                    _merge_scores(all_scores, text_scores)
        finally:
            _secure_delete(tmp_path)

    # ── Audio / Voice ──────────────────────────────────────────────────────
    elif normalized_type in AUDIO_TYPES:
        if settings.STT_ENABLED:
            with tempfile.NamedTemporaryFile(
                suffix=".ogg", dir=settings.TEMP_MEDIA_DIR, delete=False
            ) as tmp:
                tmp.write(media_bytes)
                tmp_path = tmp.name
            try:
                transcript = await loop.run_in_executor(None, transcribe_audio, tmp_path)
                if transcript:
                    notes.append(f"stt_transcript:{len(transcript)}chars")
                    text_scores, lang = await loop.run_in_executor(
                        None, analyze_text, transcript, enabled_categories, threshold
                    )
                    detected_language = lang
                    _merge_scores(all_scores, text_scores)
            finally:
                _secure_delete(tmp_path)

    # ── Analyze caption if provided ────────────────────────────────────────
    if caption and caption.strip():
        caption_scores, cap_lang = await loop.run_in_executor(
            None, analyze_text, caption, enabled_categories, threshold
        )
        if detected_language == "unknown":
            detected_language = cap_lang
        _merge_scores(all_scores, caption_scores)
        notes.append("caption_analyzed")

    # Fill in any missing categories with disabled/zero scores
    present = {s.category for s in all_scores}
    for cat in ["sexual_nsfw", "violence_gore", "weapons", "drugs_alcohol",
                "self_harm", "hate_extremism", "harassment_abuse"]:
        if cat not in present:
            all_scores.append(CategoryScore(
                category=cat, triggered=False, confidence=0.0,
                severity=SeverityLevel.NONE, reason=""
            ))

    result = ModerationResult(
        categories=all_scores,
        detected_language=detected_language,
        content_type=normalized_type,
        processing_notes=notes,
    )
    result.compute_aggregate()

    # Cache result
    cache.set(cache_key, result)
    return result


def _normalize_content_type(ct: str) -> str:
    ct = ct.lower()
    mapping = {
        "photo": "photo",
        "image": "photo",
        "sticker": "sticker",
        "animation": "gif",
        "gif": "gif",
        "video": "video",
        "video_note": "video_note",
        "voice": "voice",
        "audio": "audio",
        "document": "document",
    }
    return mapping.get(ct, ct)


def _merge_scores(existing: list[CategoryScore], new_scores: list[CategoryScore]) -> None:
    """Merge new_scores into existing by keeping the highest confidence per category."""
    existing_map = {s.category: s for s in existing}
    for new in new_scores:
        if new.category not in existing_map:
            existing.append(new)
            existing_map[new.category] = new
        else:
            old = existing_map[new.category]
            if new.confidence > old.confidence:
                # Replace in list
                idx = existing.index(old)
                existing[idx] = new
                existing_map[new.category] = new


def _secure_delete(path: str) -> None:
    """Securely remove a temp file."""
    try:
        if os.path.exists(path):
            # Overwrite with zeros then delete
            size = os.path.getsize(path)
            with open(path, "wb") as f:
                f.write(b"\x00" * size)
            os.remove(path)
    except Exception as exc:
        logger.warning(f"Secure delete failed for {path}: {exc}")
        try:
            os.remove(path)
        except Exception:
            pass
