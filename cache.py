"""
缓存模块 — 管理本地缓存（歌曲、模型、状态等）

特性：
- TTL 过期判断（time.monotonic + >= 比较，避免 sub-microsecond race）
- 协程安全（asyncio.Lock 保护 dict 复合操作）
- 区分"未命中"与"data is None"（_MISSING 哨兵）
- 容量上限（可选 FIFO 淘汰，防止内存泄漏）
- 浅拷贝 data 防止顶层突变污染缓存
- 类型/范围校验（ttl_minutes 必须为非负 int）

P0 修复记录：
- A1 协程安全：get/set/delete/clear_all 全部加 asyncio.Lock
- A2 None 歧义：_MISSING 哨兵替代 None 返回
- A3 TTL=0 永不过期：time.monotonic + >= 严格比较
- A4 类型混乱：统一为 ttl_minutes (int) 入口，内部转秒
- A5 静默容错：ttl_minutes 必须非负 int
- A6 内存上限：可选 max_size 参数，FIFO 淘汰
- A7 缺 API：新增 delete / delete_prefix / ttl_remaining
- A11 is entry 死代码：删除（每次 set 都新建 CachedData，is 永远不等）
"""

import asyncio
import copy
import time
from typing import Any, Final

# 哨兵：用于区分"键不存在 / 已过期"与"data 本身是 None"
# 用 object() 而非自定义类，让"is _MISSING"成为唯一可靠判断方式
_MISSING: Final = object()


class CachedData:
    """单条缓存数据容器"""

    __slots__ = ("created_at", "data", "ttl_seconds")

    def __init__(self, data: Any, ttl_minutes: int = 5):
        if not isinstance(ttl_minutes, int) or isinstance(ttl_minutes, bool):
            raise TypeError(
                f"ttl_minutes must be int, got {type(ttl_minutes).__name__}"
            )
        if ttl_minutes < 0:
            raise ValueError(f"ttl_minutes must be >= 0, got {ttl_minutes}")

        # 浅拷贝：防止外部直接 `cache_data.append(...)` 污染缓存
        # 注：嵌套对象（如 list 里的 dict）的内部突变无法阻止，调用方应遵守"只读"约定
        try:
            self.data = copy.copy(data)
        except Exception:
            # 不可拷贝的对象（如 file handle、生成器），存原始引用
            self.data = data

        # 用 monotonic 而非 datetime.now()：不受 wall-clock 调整影响，更适合测 elapsed
        self.created_at = time.monotonic()
        self.ttl_seconds = ttl_minutes * 60

    def is_expired(self) -> bool:
        # 严格 >=：TTL=0 时立即过期（不存在 sub-microsecond 漏网）
        return (time.monotonic() - self.created_at) >= self.ttl_seconds

    def ttl_remaining(self) -> float:
        """剩余 TTL（秒），已过期返回 0"""
        return max(0.0, self.ttl_seconds - (time.monotonic() - self.created_at))

    def __repr__(self) -> str:
        return (
            f"CachedData(data={self.data!r:.80}, "
            f"ttl={self.ttl_seconds}s, remaining={self.ttl_remaining():.1f}s)"
        )


class CacheLayer:
    """缓存层管理器（asyncio 协程安全）

    用法：
        cache = CacheLayer(default_ttl_minutes=5, max_size=128)

        # 写入
        await cache.set("songs", [...])

        # 读取：用 _MISSING 区分"未命中"与"data is None"
        songs = await cache.get("songs")
        if songs is _MISSING:
            songs = await fetch_from_api()
            await cache.set("songs", songs)

        # 选择性失效：上传歌曲后立即让缓存失效
        await cache.delete("songs")

        # 前缀失效：重连时只清连接相关缓存
        await cache.delete_prefix("http_")
    """

    def __init__(self, default_ttl_minutes: int = 5, max_size: int | None = None):
        if not isinstance(default_ttl_minutes, int) or isinstance(default_ttl_minutes, bool):
            raise TypeError(
                f"default_ttl_minutes must be int, got {type(default_ttl_minutes).__name__}"
            )
        if default_ttl_minutes < 0:
            raise ValueError(
                f"default_ttl_minutes must be >= 0, got {default_ttl_minutes}"
            )
        if max_size is not None and (not isinstance(max_size, int) or max_size <= 0):
            raise ValueError(f"max_size must be positive int, got {max_size!r}")

        self.default_ttl_minutes = default_ttl_minutes
        self._cache: dict[str, CachedData] = {}
        self._lock = asyncio.Lock()  # 协程间互斥，保护 dict 复合操作
        self._max_size = max_size     # None = 无上限

    async def get(self, key: str) -> Any:
        """获取缓存值

        Returns:
            缓存值（可能为 None），或 _MISSING 哨兵（key 不存在 / 已过期）
        """
        if not isinstance(key, str):
            raise TypeError(f"key must be str, got {type(key).__name__}")
        async with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return _MISSING
            if entry.is_expired():
                del self._cache[key]
                return _MISSING
            return entry.data

    async def set(self, key: str, data: Any, ttl_minutes: int | None = None) -> None:
        """设置缓存（浅拷贝 data）"""
        if not isinstance(key, str):
            raise TypeError(f"key must be str, got {type(key).__name__}")
        ttl = ttl_minutes if ttl_minutes is not None else self.default_ttl_minutes
        # 校验 ttl（CachedData 构造时也会校验，这里提早抛错更友好）
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 0:
            raise ValueError(f"ttl_minutes must be non-negative int, got {ttl!r}")

        async with self._lock:
            # 容量检查：满了清最早的（FIFO 简化版；如需严格 LRU，可换 OrderedDict）
            if (
                self._max_size is not None
                and key not in self._cache
                and len(self._cache) >= self._max_size
            ):
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]
            self._cache[key] = CachedData(data, ttl)

    async def delete(self, key: str) -> bool:
        """删除指定 key；返回是否原本存在"""
        if not isinstance(key, str):
            raise TypeError(f"key must be str, got {type(key).__name__}")
        async with self._lock:
            return self._cache.pop(key, None) is not None

    async def delete_prefix(self, prefix: str) -> int:
        """删除所有以前缀开头的 key；返回删除数量

        用途：重连时只清连接相关缓存，避免误清其他类型缓存
        """
        if not isinstance(prefix, str):
            raise TypeError(f"prefix must be str, got {type(prefix).__name__}")
        async with self._lock:
            to_del = [k for k in self._cache if k.startswith(prefix)]
            for k in to_del:
                del self._cache[k]
            return len(to_del)

    async def clear_all(self) -> None:
        """清空所有缓存"""
        async with self._lock:
            self._cache.clear()

    def ttl_remaining(self, key: str) -> float | None:
        """查询 key 剩余 TTL（秒）；key 不存在或已过期返回 None

        同步接口（不持锁）：用于监控/调试，不保证并发原子性
        """
        entry = self._cache.get(key)
        if entry is None or entry.is_expired():
            return None
        return entry.ttl_remaining()

    def __contains__(self, key: object) -> bool:
        """支持 `key in cache_layer` 语法（不算过期 key 为存在）"""
        if not isinstance(key, str):
            return False
        entry = self._cache.get(key)
        return entry is not None and not entry.is_expired()

    def __len__(self) -> int:
        """支持 `len(cache_layer)`（含过期项，会在下次 get 时被清理）"""
        return len(self._cache)

    def __repr__(self) -> str:
        return f"CacheLayer(size={len(self._cache)}, max_size={self._max_size})"
