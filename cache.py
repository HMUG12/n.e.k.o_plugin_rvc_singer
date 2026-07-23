"""
缓存模块 — 管理本地缓存（歌曲、模型、状态等）

特性：
- TTL 过期判断
- 多类型缓存管理
"""

from datetime import datetime, timedelta
from typing import Any


class CachedData:
    """单条缓存数据容器"""

    def __init__(self, data: Any, ttl_minutes: int = 5):
        self.data = data
        self.timestamp = datetime.now()
        self.ttl = timedelta(minutes=ttl_minutes)

    def is_expired(self) -> bool:
        return datetime.now() - self.timestamp > self.ttl

    def get(self) -> Any | None:
        return self.data if not self.is_expired() else None


class CacheLayer:
    """缓存层管理器"""

    def __init__(self, default_ttl_minutes: int = 5):
        self.default_ttl = default_ttl_minutes
        self._cache: dict[str, CachedData] = {}

    def get(self, key: str) -> Any | None:
        if key not in self._cache:
            return None
        data = self._cache[key].get()
        if data is None:
            del self._cache[key]
            return None
        return data

    def set(self, key: str, data: Any, ttl_minutes: int = None) -> None:
        ttl = ttl_minutes if ttl_minutes is not None else self.default_ttl
        self._cache[key] = CachedData(data, ttl)

    def clear_all(self) -> None:
        self._cache.clear()
