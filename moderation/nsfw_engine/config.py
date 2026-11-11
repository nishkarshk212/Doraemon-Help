import os

class NSFWConfig:
    DEFAULT_THRESHOLD: float = 0.65
    DEFAULT_ACTION: str = "delete"
    DEFAULT_MAX_WARNINGS: int = 3
    OCR_ENABLED: bool = True
    STT_ENABLED: bool = False
    MAX_MEDIA_SIZE_MB: int = 30
    MEDIA_FRAME_SAMPLE_COUNT: int = 10
    CACHE_MAX_ENTRIES: int = 5000
    CACHE_TTL_SECONDS: int = 3600
    TEMP_MEDIA_DIR: str = "/tmp/doraemon_nsfw_media"

    enabled_categories_map = {
        "sexual_nsfw": True,
        "violence_gore": True,
        "weapons": True,
        "drugs_alcohol": True,
        "self_harm": True,
        "hate_extremism": True,
        "harassment_abuse": True,
    }

settings = NSFWConfig()
os.makedirs(settings.TEMP_MEDIA_DIR, exist_ok=True)
