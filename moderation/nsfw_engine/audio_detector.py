"""
Speech-to-text audio detector.

Uses OpenAI Whisper (via the `whisper` or `faster-whisper` package) when
STT_ENABLED=true. Falls back gracefully if unavailable.
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional

from moderation.nsfw_engine.config import settings
from moderation.nsfw_engine.logger import get_logger

logger = get_logger("audio_detector")

_WHISPER_AVAILABLE = False

if settings.STT_ENABLED:
    try:
        import whisper  # type: ignore
        _WHISPER_AVAILABLE = True
        _whisper_model = whisper.load_model("tiny")
        logger.info("Whisper STT model loaded (tiny).")
    except ImportError:
        try:
            from faster_whisper import WhisperModel  # type: ignore
            _whisper_model = WhisperModel("tiny", compute_type="int8")
            _WHISPER_AVAILABLE = True
            logger.info("Faster-Whisper STT model loaded (tiny).")
        except ImportError:
            logger.warning(
                "STT_ENABLED=true but neither 'whisper' nor 'faster-whisper' is installed. "
                "Install with: pip install openai-whisper OR pip install faster-whisper"
            )


def transcribe_audio(audio_path: str | Path) -> Optional[str]:
    """
    Transcribe an audio or video file to text.

    Returns the transcribed text, or None if STT is disabled/unavailable.
    """
    if not settings.STT_ENABLED or not _WHISPER_AVAILABLE:
        return None

    try:
        # Standard openai-whisper
        if hasattr(_whisper_model, "transcribe"):
            result = _whisper_model.transcribe(str(audio_path))
            return result.get("text", "").strip() or None
        else:
            # faster-whisper API
            segments, _ = _whisper_model.transcribe(str(audio_path))
            text = " ".join(seg.text for seg in segments).strip()
            return text or None
    except Exception as exc:
        logger.warning(f"STT transcription failed: {exc}")
        return None
