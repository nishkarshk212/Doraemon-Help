import time
from collections import OrderedDict
from typing import Any, Optional
from moderation.nsfw_engine.config import settings

class _TTLEntry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: Any, ttl: int) -> None:
        self.value = value
        self.expires_at = time.monotonic() + ttl

class LRUCache:
    """Thread-safe in-process LRU cache with per-entry TTL."""

    def __init__(self, max_entries: int = 5000, ttl: int = 7200) -> None:
        self._max = max_entries
        self._ttl = ttl
        self._store: OrderedDict[str, _TTLEntry] = OrderedDict()

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            return None
        if time.monotonic() > entry.expires_at:
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)
        return entry.value

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        effective_ttl = ttl if ttl is not None else self._ttl
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = _TTLEntry(value, effective_ttl)
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)

# Singleton cache instance
cache = LRUCache(
    max_entries=getattr(settings, "CACHE_MAX_ENTRIES", 5000),
    ttl=getattr(settings, "CACHE_TTL_SECONDS", 7200),
)

