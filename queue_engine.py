"""
Song Queue Engine V2 — 歌曲排队调度核心（增强版）
管理歌曲队列、逐个处理、完成后自动推下一首

新增:
- FIFO 队列 + 优先级插队
- 同时只能处理一首歌
- 前一首完成 → 自动推下一首
- 重试机制（指数退避，最多 N 次）
- 取消任意歌曲（含当前处理中的）
- 进度回调
- 提供回调钩子供插件推送消息/更新浮窗
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, Awaitable

logger = logging.getLogger("rvc_singer.queue_engine")


# ════════════════════════════════════════════════
# 数据模型
# ════════════════════════════════════════════════

class QueueItemStatus(Enum):
    WAITING = "waiting"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRYING = "retrying"


class Priority(Enum):
    HIGH = 1
    NORMAL = 2
    LOW = 3


@dataclass
class QueueItem:
    """队列中的一首歌"""
    song_name: str
    model: str
    pitch_shift: int = 0
    priority: Priority = Priority.NORMAL
    added_at: float = field(default_factory=time.time)
    status: QueueItemStatus = QueueItemStatus.WAITING
    task_id: str = ""
    result: Optional[dict] = None
    error: str = ""
    retry_count: int = 0
    max_retries: int = 2


@dataclass
class QueueSnapshot:
    """队列当前快照，供 UI/日志/推送使用"""
    items: list[dict]
    now_playing: Optional[str]
    queue_size: int
    progress: float = 0.0
    status: str = "idle"  # idle / processing / error

    def to_dict(self) -> dict:
        return {
            "items": self.items,
            "now_playing": self.now_playing,
            "queue_size": self.queue_size,
            "progress": self.progress,
            "status": self.status,
        }


# ════════════════════════════════════════════════
# 回调协议
# ════════════════════════════════════════════════

# 提交任务回调: (song_name, model, pitch_shift) -> Ok({"task_id": "xxx"}) | Err
SubmitCallback = Callable[[str, str, int], Awaitable]
# 等待结果回调: (task_id, song_name) -> Ok(result_dict) | Err
WaitCallback = Callable[[str, str], Awaitable]
# 播放推送回调: (result_dict, song_name) -> None
PlayCallback = Callable[[dict, str], Awaitable]
# 快照变更回调: (QueueSnapshot) -> None
SnapshotCallback = Callable[[QueueSnapshot], None]
# 进度回调: (song_name: str, progress: float) -> None
ProgressCallback = Callable[[str, float], None]


# ════════════════════════════════════════════════
# 队列引擎
# ════════════════════════════════════════════════

class SongQueueEngine:
    """歌曲排队调度器 V2

    工作流:
    user 点歌 → enqueue(item) → 如有空闲槽位 → process_next() → 提交到 B 端
    → 轮询等待（带进度更新）→ on_complete 回调 → 自动 process_next() → 下一首

    新增特性:
    - 优先级调度（HIGH 插队，插入队列前面）
    - 自动重试（指数退避，最多 max_retries 次）
    - 进度实时回调
    - 取消正在处理中的任务
    """

    def __init__(self, max_queue_size: int = 50):
        self._queue: deque[QueueItem] = deque()
        self._max_size = max_queue_size
        self._current: Optional[QueueItem] = None
        self._lock = asyncio.Lock()
        self._cancel_flag = False  # 取消当前任务标志

        # 回调钩子
        self.submit_fn: Optional[SubmitCallback] = None
        self.wait_fn: Optional[WaitCallback] = None
        self.on_play_callback: Optional[PlayCallback] = None
        self.on_snapshot_callback: Optional[SnapshotCallback] = None
        self.on_progress_callback: Optional[ProgressCallback] = None

    # ── 入队 ──

    async def enqueue(self, song_name: str, model: str,
                      pitch_shift: int = 0,
                      priority: Priority = Priority.NORMAL,
                      max_retries: int = 2) -> tuple[bool, str]:
        """加入队列。返回 (ok, message)。

        Args:
            song_name: 歌曲名
            model: 模型名
            pitch_shift: 音高偏移（半音）
            priority: 优先级（HIGH 会插队到前面）
            max_retries: 本首最大重试次数
        """
        async with self._lock:
            if len(self._queue) >= self._max_size:
                return False, f"队列已满（最多 {self._max_size} 首）"

            # 校验 pitch_shift 范围（RVC 支持的合法值）
            if not isinstance(pitch_shift, int):
                pitch_shift = int(pitch_shift) if isinstance(pitch_shift, (float, str)) else 0
            pitch_shift = max(-36, min(36, pitch_shift))

            item = QueueItem(
                song_name=song_name,
                model=model,
                pitch_shift=pitch_shift,
                priority=priority,
                max_retries=max_retries,
            )

            # 按优先级插入
            if priority == Priority.HIGH:
                # 插入到第一个非 HIGH 项之前
                insert_idx = 0
                for i, qi in enumerate(self._queue):
                    if qi.priority != Priority.HIGH:
                        insert_idx = i
                        break
                else:
                    insert_idx = len(self._queue)
                list_q = list(self._queue)
                list_q.insert(insert_idx, item)
                self._queue = deque(list_q)
            elif priority == Priority.LOW:
                self._queue.append(item)
            else:
                # NORMAL: 插入到 HIGH 之后、LOW 之前
                insert_idx = len(self._queue)
                for i, qi in enumerate(self._queue):
                    if qi.priority == Priority.LOW:
                        insert_idx = i
                        break
                list_q = list(self._queue)
                list_q.insert(insert_idx, item)
                self._queue = deque(list_q)

            position = self._count_waiting()
            await self._emit_snapshot()

        # 如果没有在处理，立即开始
        async with self._lock:
            if self._current is None:
                asyncio.create_task(self._process_next())

        return True, (
            f"《{song_name}》已加入队列（第 {position} 首）"
            if self._current else f"《{song_name}》开始处理中...")

    async def enqueue_batch(self, songs: list[dict]) -> tuple[int, int]:
        """批量入队。返回 (成功数, 失败数)"""
        ok_count = fail_count = 0
        for s in songs:
            success, _ = await self.enqueue(
                song_name=s.get("song_name", "??"),
                model=s.get("model", ""),
                pitch_shift=s.get("pitch_shift", 0),
                priority=s.get("priority", Priority.NORMAL),
                max_retries=s.get("max_retries", 2),
            )
            if success:
                ok_count += 1
            else:
                fail_count += 1
        return ok_count, fail_count

    # ── 取消 ──

    async def cancel(self, song_name: str) -> tuple[bool, str]:
        """取消队列中的某首歌（支持取消正在处理的）"""
        async with self._lock:
            # 检查等待中的
            for item in self._queue:
                if item.song_name == song_name and item.status == QueueItemStatus.WAITING:
                    item.status = QueueItemStatus.CANCELLED
                    self._queue.remove(item)
                    await self._emit_snapshot()
                    return True, f"《{song_name}》已取消"

            # 检查当前正在处理的
            if self._current and self._current.song_name == song_name:
                self._cancel_flag = True
                return True, f"正在取消《{song_name}》..."

        return False, f"未找到《{song_name}》"

    async def cancel_all(self):
        """清空整个队列（含当前任务）"""
        async with self._lock:
            self._queue.clear()
            self._cancel_flag = True
            await self._emit_snapshot()
        logger.info("队列已清空")

    # ── 查询 ──

    def get_snapshot(self) -> QueueSnapshot:
        """获取当前快照"""
        items_data = []
        for item in self._queue:
            items_data.append({
                "song_name": item.song_name,
                "status": item.status.value,
                "priority": item.priority.name,
                "retry_count": item.retry_count,
            })
        return QueueSnapshot(
            items=items_data,
            now_playing=self._current.song_name if self._current else None,
            queue_size=len(self._queue),
            progress=getattr(self._current, '_progress', 0.0) if self._current else 0.0,
            status=(
                "processing" if self._current else
                "error" if any(i.status == QueueItemStatus.FAILED for i in self._queue)
                else "idle"
            ),
        )

    def is_empty(self) -> bool:
        return len(self._queue) == 0 and self._current is None

    @property
    def queue_length(self) -> int:
        return len(self._queue)

    def _count_waiting(self) -> int:
        return sum(1 for i in self._queue if i.status == QueueItemStatus.WAITING)

    # ── 快照通知 ──

    async def _emit_snapshot(self):
        if self.on_snapshot_callback:
            snapshot = self.get_snapshot()
            try:
                if asyncio.iscoroutinefunction(self.on_snapshot_callback):
                    await self.on_snapshot_callback(snapshot)
                else:
                    self.on_snapshot_callback(snapshot)
            except Exception:
                pass

    # ── 进度通知 ──

    async def _emit_progress(self, song_name: str, progress: float):
        if self._current:
            self._current._progress = progress  # type: ignore
        if self.on_progress_callback:
            try:
                if asyncio.iscoroutinefunction(self.on_progress_callback):
                    await self.on_progress_callback(song_name, progress)
                else:
                    self.on_progress_callback(song_name, progress)
            except Exception:
                pass

    # ── 核心处理循环 ──

    async def _process_next(self):
        """处理队列中下一首歌（含重试逻辑）"""
        async with self._lock:
            if not self._queue:
                self._current = None
                await self._emit_snapshot()
                return

            item = self._queue.popleft()
            item.status = QueueItemStatus.PROCESSING
            self._current = item
            self._cancel_flag = False
            await self._emit_snapshot()

        # 处理循环（含重试）
        while True:
            try:
                result = await self._process_one(item)
                if result:
                    # 成功
                    item.status = QueueItemStatus.COMPLETED
                    item.result = result

                    # 回调：播放推送
                    if self.on_play_callback:
                        try:
                            if asyncio.iscoroutinefunction(self.on_play_callback):
                                await self.on_play_callback(item.result, item.song_name)
                            else:
                                self.on_play_callback(item.result, item.song_name)
                        except Exception as exc:
                            logger.warning("on_play_callback 出错: %s", exc)

                    break
                else:
                    # 失败 — 尝试重试
                    if self._cancel_flag:
                        item.status = QueueItemStatus.CANCELLED
                        item.error = "用户取消"
                        break

                    item.retry_count += 1
                    if item.retry_count <= item.max_retries:
                        item.status = QueueItemStatus.RETRYING
                        logger.info(
                            "《%s》第 %d/%d 次重试",
                            item.song_name,
                            item.retry_count,
                            item.max_retries,
                        )
                        # 指数退避
                        delay = min(2 ** item.retry_count, 15)
                        await asyncio.sleep(delay)
                        self._cancel_flag = False
                        continue
                    else:
                        item.status = QueueItemStatus.FAILED
                        break

            except Exception as exc:
                item.status = QueueItemStatus.FAILED
                item.error = str(exc)
                logger.exception("处理《%s》时异常: %s", item.song_name, exc)
                break

        # 完成后：发快照 → 处理下一首
        async with self._lock:
            self._current = None
            self._cancel_flag = False
            await self._emit_snapshot()

        if self._queue:
            await asyncio.sleep(0.6)  # 短暂间隔让 UI 刷新
            asyncio.create_task(self._process_next())

    async def _process_one(self, item: QueueItem) -> Optional[dict]:
        """处理单首歌曲。返回结果字典，失败返回 None。"""

        # 1. 提交到 B 端
        if not self.submit_fn:
            item.error = "submit_fn 未设置"
            return None

        result = await self.submit_fn(item.song_name, item.model, item.pitch_shift)

        if hasattr(result, 'is_err') and result.is_err():
            item.error = str(result.error)
            return None

        item.task_id = (
            result.value.get("task_id")
            if hasattr(result, 'value')
            else result.get("task_id", "")
        )

        if not item.task_id:
            item.error = "B 端未返回 task_id"
            return None

        # 2. 轮询等待（带取消检查 + 进度回调）
        if not self.wait_fn:
            item.error = "wait_fn 未设置"
            return None

        last_progress = 0.0
        while True:
            # 检查取消
            if self._cancel_flag:
                item.error = "用户取消"
                return None

            wait_result = await self.wait_fn(item.task_id, item.song_name)

            if wait_result is None:
                item.error = "wait_fn 返回 None"
                return None

            if hasattr(wait_result, 'is_err') and wait_result.is_err():
                item.error = str(wait_result.error)
                return None

            # 检查是否完成
            data = wait_result.value if hasattr(wait_result, 'value') else wait_result
            is_done = data.get("done", data.get("finished", False))
            progress = data.get("progress", 0.0)

            if is_done:
                # 通知进度 100%
                await self._emit_progress(item.song_name, 1.0)
                return data

            # 进度回调（去重）
            if abs(progress - last_progress) > 0.01:
                last_progress = progress
                await self._emit_progress(item.song_name, progress)

            # 轮询间隔
            await asyncio.sleep(0.5)
