"""
End-to-end verification tests for Doraemon Help Bot NSFW detection:
- Static images & transparent WebP stickers
- GIF & MP4 animations
- Video stickers (.webm) & Animated stickers (.tgs via thumbnail)
- Oversized video/document thumbnail fallback
- Threshold-scoped cache isolation
- Neural model availability (NudeNet, MobileNetV2, RapidOCR, OpenCV)
"""
import asyncio
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

from PIL import Image, ImageDraw

from moderation.nsfw_engine.config import settings
from moderation.nsfw_engine.pipeline import analyze_media_bytes
from moderation.nsfw_engine.vision_detector import (
    analyze_image_bytes,
    sample_gif_frames,
    sample_video_frames,
    _NUDENET_AVAILABLE,
    _MOBILENET_AVAILABLE,
)
from moderation.nsfw_engine.ocr_detector import _RAPIDOCR_AVAILABLE
from features.nsfw_panel import _analyze_message_target, get_nsfw_panel_keyboard, render_nsfw_panel_text

ENABLED = dict(settings.enabled_categories_map)


def _make_skin_image_bytes(fmt="JPEG", mode="RGB", size=(300, 300)) -> bytes:
    img = Image.new(mode, size, color=(205, 145, 105, 255) if "A" in mode else (205, 145, 105))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _make_green_image_bytes(fmt="JPEG", size=(300, 300)) -> bytes:
    img = Image.new("RGB", size, color=(30, 180, 60))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _make_gif_bytes(n_frames=6, skin_last_frame=True) -> bytes:
    frames = []
    for i in range(n_frames):
        color = (205, 145, 105) if (skin_last_frame and i == n_frames - 1) else (30, 180, 60)
        frames.append(Image.new("RGB", (200, 200), color=color))
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], loop=0, duration=100)
    return buf.getvalue()


class TestNSFWVisionAndHandlers(unittest.TestCase):
    def test_ml_engines_loaded(self):
        self.assertTrue(_NUDENET_AVAILABLE, "NudeNet should be installed and loaded")
        self.assertTrue(_MOBILENET_AVAILABLE, "MobileNetV2 ONNX should be loaded")
        self.assertTrue(_RAPIDOCR_AVAILABLE, "RapidOCR ONNX should be loaded")
        import cv2  # noqa: F401

    def test_skin_and_clean_images(self):
        skin = _make_skin_image_bytes()
        scores = analyze_image_bytes(skin, ENABLED, threshold=0.60)
        nsfw = next(s for s in scores if s.category == "sexual_nsfw")
        self.assertTrue(nsfw.triggered)
        self.assertGreaterEqual(nsfw.confidence, 0.85)

        clean = _make_green_image_bytes()
        scores_clean = analyze_image_bytes(clean, ENABLED, threshold=0.60)
        nsfw_clean = next(s for s in scores_clean if s.category == "sexual_nsfw")
        self.assertFalse(nsfw_clean.triggered)

    def test_transparent_webp_sticker(self):
        # Create a 512x512 RGBA sticker with skin circle and transparent border
        img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((40, 40, 472, 472), fill=(205, 145, 105, 255))
        buf = io.BytesIO()
        img.save(buf, format="WEBP")
        webp_bytes = buf.getvalue()

        res = asyncio.run(analyze_media_bytes(webp_bytes, "sticker", ENABLED, threshold=0.45))
        self.assertTrue(res.flagged, "RGBA skin WebP sticker should be flagged")

    def test_gif_multi_frame_catches_late_frame(self):
        gif_bytes = _make_gif_bytes(n_frames=6, skin_last_frame=True)
        res = asyncio.run(analyze_media_bytes(gif_bytes, "animation", ENABLED, threshold=0.65))
        self.assertTrue(res.flagged, "GIF with NSFW late frame must be detected")

    def test_webm_and_mp4_sampling(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg not installed")
        with tempfile.TemporaryDirectory() as tmpdir:
            img_path = os.path.join(tmpdir, "frame.png")
            Image.new("RGB", (240, 240), (205, 145, 105)).save(img_path)
            webm_path = os.path.join(tmpdir, "sticker.webm")
            subprocess.run(
                ["ffmpeg", "-y", "-loop", "1", "-i", img_path, "-t", "1", "-c:v", "libvpx-vp9", webm_path],
                capture_output=True, check=True
            )
            frames = sample_video_frames(webm_path, n_frames=5)
            self.assertGreater(len(frames), 0, "Should extract frames from .webm video sticker")
            with open(webm_path, "rb") as f:
                webm_bytes = f.read()
            res = asyncio.run(analyze_media_bytes(webm_bytes, "video", ENABLED, threshold=0.45))
            self.assertTrue(res.flagged)

    def test_cache_respects_threshold_changes(self):
        # Skin score on mixed image ~ 0.68 -> passes 0.90 threshold, fails 0.45 threshold
        img = Image.new("RGB", (200, 200), (30, 180, 60))
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, 199, 130), fill=(205, 145, 105))  # 65% skin
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        data = buf.getvalue()

        res_strict = asyncio.run(analyze_media_bytes(data, "photo", ENABLED, threshold=0.90))
        self.assertFalse(res_strict.flagged)

        res_sensitive = asyncio.run(analyze_media_bytes(data, "photo", ENABLED, threshold=0.45))
        self.assertTrue(res_sensitive.flagged, "Cache must not return stale result across different thresholds")

    def test_sticker_and_oversized_video_message_handlers(self):
        skin_webp = _make_skin_image_bytes(fmt="WEBP", mode="RGBA")
        skin_jpg = _make_skin_image_bytes(fmt="JPEG", mode="RGB")

        async def _download_map(mapping):
            async def get_file(fid):
                f = MagicMock()
                f.download_as_bytearray = AsyncMock(return_value=bytearray(mapping[fid]))
                return f
            ctx = MagicMock()
            ctx.bot = MagicMock()
            ctx.bot.get_file = AsyncMock(side_effect=get_file)
            return ctx

        # 1. Animated (.tgs) sticker -> uses thumbnail!
        tgs_msg = MagicMock(
            photo=None, animation=None, video=None, video_note=None, document=None, text=None, caption=None,
            sticker=MagicMock(
                file_id="tgs_id", file_size=5000, is_animated=True, is_video=False,
                thumbnail=MagicMock(file_id="tgs_thumb", file_size=2000), set_name="anim_pack"
            )
        )
        ctx_tgs = asyncio.run(_download_map({"tgs_thumb": skin_webp, "tgs_id": b"not_an_image"}))
        res_tgs = asyncio.run(_analyze_message_target(tgs_msg, ctx_tgs, ENABLED, threshold=0.65))
        self.assertIsNotNone(res_tgs)
        self.assertTrue(res_tgs.flagged, "Animated .tgs sticker must be analyzed via thumbnail")

        # 2. Oversized video (>20MB) -> skips full download cleanly & checks thumbnail!
        big_vid_msg = MagicMock(
            photo=None, sticker=None, animation=None, video_note=None, document=None, text=None, caption=None,
            video=MagicMock(
                file_id="big_vid", file_size=25_000_000,
                thumbnail=MagicMock(file_id="vid_thumb", file_size=3000)
            )
        )
        ctx_vid = asyncio.run(_download_map({"vid_thumb": skin_jpg}))
        res_vid = asyncio.run(_analyze_message_target(big_vid_msg, ctx_vid, ENABLED, threshold=0.65))
        self.assertIsNotNone(res_vid)
        self.assertTrue(res_vid.flagged, "Oversized >20MB video must still be checked via thumbnail")

    def test_content_type_toggles_and_panel(self):
        # Panel keyboard & text rendering with custom toggles
        settings_map = {
            "nsfw_filter_enabled": True,
            "nsfw_detect_text": False,
            "nsfw_detect_sticker": True,
            "nsfw_detect_media": False,
        }
        panel_text = render_nsfw_panel_text("Test Group", settings_map)
        self.assertIn("Text (🔴)", panel_text)
        self.assertIn("Sticker (🟢)", panel_text)
        self.assertIn("Media (🔴)", panel_text)

        kb = get_nsfw_panel_keyboard(-100123456, settings_map)
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertIn("nsfw_toggle_type:text:-100123456", callbacks)
        self.assertIn("nsfw_toggle_type:sticker:-100123456", callbacks)
        self.assertIn("nsfw_toggle_type:media:-100123456", callbacks)

        # Text disabled skips plain text NSFW
        text_msg = MagicMock(
            photo=None, sticker=None, animation=None, video=None, video_note=None, document=None,
            text="pornography xxx", caption=None
        )
        ctx = MagicMock()
        self.assertIsNone(asyncio.run(_analyze_message_target(text_msg, ctx, ENABLED, 0.65, detect_text=False)))
        res_text_on = asyncio.run(_analyze_message_target(text_msg, ctx, ENABLED, 0.65, detect_text=True))
        self.assertTrue(res_text_on is not None and res_text_on.flagged)

        # Sticker disabled skips sticker
        stk_msg = MagicMock(
            photo=None, animation=None, video=None, video_note=None, document=None, text=None, caption=None,
            sticker=MagicMock(file_id="s1", is_animated=False, is_video=False, thumbnail=None)
        )
        self.assertIsNone(asyncio.run(_analyze_message_target(stk_msg, ctx, ENABLED, 0.65, detect_sticker=False)))

        # Media disabled skips photo pixels (with clean/no caption)
        photo_msg = MagicMock(
            sticker=None, animation=None, video=None, video_note=None, document=None, text=None, caption="",
            photo=[MagicMock(file_id="p1", file_size=5000)]
        )
        self.assertIsNone(asyncio.run(_analyze_message_target(photo_msg, ctx, ENABLED, 0.65, detect_media=False)))


if __name__ == "__main__":
    unittest.main()

