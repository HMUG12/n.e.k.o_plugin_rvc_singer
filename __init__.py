"""
RVC Singer Plugin — NEKO 歌声合成对接插件

架构：
  NEKO Plugin A (本文件) ←→ HTTP API ←→ RVC Studio B (独立桌面程序)

工作流：
  1. 用户在 NEKO 中说"唱首歌"
  2. NEKO Agent 调用本插件的 entry，获取上下文（歌曲名、风格等）
  3. 插件通过 HTTP 转发请求到 RVC Studio B
  4. RVC Studio B 使用 RVC 引擎进行人声分离→音色转换→合成完整歌曲
  5. 处理完成后返回音频 + 歌词 + viseme 口型数据
  6. 插件将结果推送到 NEKO 对话，触发口型同步播放

优化特性：
  - HTTP连接池复用 & 会话管理
  - 参数验证 & 类型检查
  - 本地缓存机制（歌曲、模型列表）
  - 并发控制 & 任务去重
  - 错误分类 & 自适应恢复
  - 健康检查 & 自动重连
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode, urlparse

# ── 将插件目录加入 sys.path，确保本地依赖（如 PySide6）可导入 ──
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

# 按 Project N.E.K.O. 插件规范：插件自给自足，常量模块内置于本目录（config.py / paths.py）。
# 不再 import B 端项目根；A 端与 B 端各自持有相同内容，由 sync_plugin.ps1 保持同步。

from plugin.sdk.plugin import (  # noqa: E402 — 必须在 sys.path 注入之后导入
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    neko_plugin,
    plugin_entry,
    tr,
    ui,
)

# 导入新的模块（核心依赖，无外部依赖）
from .cache import (  # noqa: E402  # _MISSING 用于区分"未命中"与"data is None"
    _MISSING,
    CacheLayer,
)
from .http_client import AsyncHttpClient  # noqa: E402
from .queue_engine import QueueSnapshot, SongQueueEngine  # noqa: E402
from .state import patch_sdk_error  # noqa: E402

# ═══════════════ 插件内常量（实现细节） ══════════════
# 按 Project N.E.K.O. 插件规范：常量要么放 plugin.toml 的 [settings]（用户可调），
# 要么 inline 在 __init__.py 顶部（实现细节）。无需独立 config.py。
A_HTTP_TIMEOUT_DEFAULT = 5.0      # GET /api/models、/api/config 等常规调用
A_HTTP_TIMEOUT_HEALTH = 2.0       # /api/health 端点
A_HTTP_TIMEOUT_SEARCH = 310.0     # /api/search_and_download（网络搜索下载）
A_HTTP_TIMEOUT_UPLOAD = 30.0      # /api/upload
A_POLL_YIELD_INTERVAL = 0.5       # 进度轮询让出间隔
HEALTH_WARMUP_DELAY = 2.0         # Studio 启动后暖机延迟
STALE_TASK_TIMEOUT = 300.0        # 进度 5 分钟不变视为卡死

# ── PySide6 可选依赖：缺失时降级为 no-op stub ──
# 悬浮歌词/队列窗口依赖 PySide6，但 NEKO 环境可能未安装。
# 导入失败时提供空操作 stub，核心功能（HTTP 通信）不受影响。
try:
    from .lyrics_window import hide_lyrics_window, push_lyrics_data, show_lyrics_window
except ImportError:
    def show_lyrics_window(): pass
    def push_lyrics_data(data): pass
    def hide_lyrics_window(): pass

try:
    from .queue_window import (
        hide_queue_window,
        push_queue_data,
        set_cancel_callback,
        show_queue_window,
    )
except ImportError:
    def show_queue_window(): pass
    def push_queue_data(data): pass
    def hide_queue_window(): pass
    def set_cancel_callback(cb): pass

# ── 关键修复：确保 SdkError 始终有 .message 属性 ──
patch_sdk_error()

_RVC_STUDIO_DEFAULT_HOST = "127.0.0.1"
_RVC_STUDIO_DEFAULT_PORT = 19877
_API_TIMEOUT = 600  # 10分钟超时（歌曲处理可能较慢）
_HEALTH_CHECK_INTERVAL = 30  # 从 120 秒降低到 30 秒，避免长时间不检查 Studio 状态
_MAX_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 1


def _ensure_url(host: str, port: int, use_https: bool = False) -> str:
    """构建 RVC Studio 基础 URL

    - host 可自带 scheme（如 "https://my.server.com"）→ 尊重其 scheme
    - host 已带端口（如 "my.server.com:8443"）→ 不重复追加
    - 否则按 use_https 拼 http/https
    """
    h = (host or "").strip().rstrip("/")
    if h.startswith(("http://", "https://")):
        scheme, hostpart = h.split("://", 1)
    else:
        scheme, hostpart = ("https" if use_https else "http"), h
    # 防御空主机名（例如远端配置下发失败时 hostpart 为空串）
    if not hostpart.strip():
        hostpart = "127.0.0.1"
    if ":" in hostpart:
        return f"{scheme}://{hostpart}"
    return f"{scheme}://{hostpart}:{port}"


def _validate_host_port(host: str, port: int) -> tuple[bool, str]:
    """验证主机和端口配置"""
    if not isinstance(host, str) or not host.strip():
        return False, "主机地址不能为空"
    if not isinstance(port, int) or port <= 0 or port > 65535:
        return False, "端口必须是 1-65535 之间的整数"
    return True, ""


@neko_plugin
class RvcSingerPlugin(NekoPluginBase):
    """RVC 歌声合成插件 — NEKO 对接口
    
    核心特性：
    - HTTP连接池复用，减少连接开销
    - 本地缓存机制，降低API调用频率
    - 参数验证和类型检查
    - 并发控制防止任务重复
    - 错误分类和自适应恢复
    """

    def __init__(self, ctx):
        super().__init__(ctx)
        self._studio_host: str = _RVC_STUDIO_DEFAULT_HOST
        self._studio_port: int = _RVC_STUDIO_DEFAULT_PORT
        # ── P2: 远程连接加固（api_key 鉴权 / https / 自签证书跳过校验）──
        self._api_key: str = ""
        self._use_https: bool = False
        self._ssl_verify: bool = True
        self._studio_url: str = _ensure_url(self._studio_host, self._studio_port, self._use_https)
        self._http_client: AsyncHttpClient | None = None  # aiohttp 客户端
        self._studio_available: bool = False
        self._rvc_ready: bool = False          # 引擎是否已加载（独立于连接状态）
        self._health_task: asyncio.Task | None = None
        self._windows_task: asyncio.Task | None = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._cancel_event: asyncio.Event = asyncio.Event()
        self._rvc_root: str = ""
        self._default_model: str = ""  # 空字符串 = 自动选择第一个可用模型
        self._active_task_id: str | None = None
        self._last_progress: int = 0
        self._last_step: str = ""
        self._last_song_name: str = ""
        self._last_model: str = ""
        self._last_pitch_shift: int = 0
        self._last_postprocess: str = "none"  # 上次使用的音效后处理预设
        self._last_task_type: str = ""  # "sing" | "compare"
        self._auto_mix: bool = True  # 混音开关（从 GUI 同步）
        # M6: 混音配置（从 GUI 同步，供 sing_song 调用 B 端 /api/sing 时使用）
        self._mix_preset: str = "general"
        self._mix_vocal_db: float = 0.0
        self._mix_inst_db: float = -1.0
        self._mix_reverb: float = 0.0
        self._mix_original: float = 0.0
        
        # ── 缓存机制 ──
        self._cache = CacheLayer()
        
        # ── 并发控制 ──
        self._task_lock = asyncio.Lock()
        self._submitted_tasks: dict[str, float] = {}
        
        # ── 状态追踪 ──
        self._last_reported_status: str = "offline"
        self._last_status_update_time: float = 0.0
        self._status_change_threshold: float = 2.0
        self._sticky_status: dict = {}
        self._ever_connected: bool = False  # 曾经连上过（控制首次检查策略）
        self._ever_connected_before: bool = False  # 曾同步过 GUI 配置

        # ── 错误统计 ──
        self._consecutive_failures: int = 0
        self._last_error: str = ""
        self._last_error_time: float = 0

        # ── 队列引擎 ──
        self._queue_engine = SongQueueEngine(max_queue_size=50)
        self._queue_engine.submit_fn = self._queue_submit_song
        self._queue_engine.wait_fn = self._queue_wait_task
        self._queue_engine.on_play_callback = self._push_song_result
        self._queue_engine.on_snapshot_callback = self._on_queue_snapshot
        self._queue_engine.on_progress_callback = self._on_queue_progress

    # ── HTTP 客户端管理 ──

    async def _init_http_client(self):
        """初始化（或重建）HTTP 客户端"""
        if self._http_client:
            await self._http_client.close()
        self._http_client = AsyncHttpClient(
            self._studio_url,
            api_key=self._api_key,
            ssl_verify=self._ssl_verify,
            timeout=_API_TIMEOUT,
        )

    # ═══════════════ 生命周期 ═══════════════

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        """启动时加载配置并启动后台健康检查

        ⚠️ NEKO 铁律 #4：启动 < 10 秒
        • 配置加载：2s 超时（快失败，不拖累启动）
        • 健康检查：异步后台（不阻塞启动）
        • 浮窗创建：后台任务（PySide6 窗口初始化可能耗时 2-4s，不能阻塞 startup）
        """
        try:
            cfg = await asyncio.wait_for(self.config.dump(), timeout=2.0)
            settings = cfg.get("settings", {})
            self._studio_host = settings.get("rvc_studio_host", _RVC_STUDIO_DEFAULT_HOST)
            self._studio_port = int(settings.get("rvc_studio_port", _RVC_STUDIO_DEFAULT_PORT))
            self._api_key = str(settings.get("api_key", "") or "").strip()
            self._use_https = bool(settings.get("use_https", False))
            self._ssl_verify = bool(settings.get("ssl_verify", True))
            self._rvc_root = settings.get("rvc_root_path", "")
            self._default_model = settings.get("default_model", "")
            self._auto_download_on_miss = bool(settings.get("auto_download_on_miss", True))
            self._not_found_max_retries = int(settings.get("not_found_max_retries", 1))
        except (asyncio.TimeoutError, Exception):
            self.logger.warning("config.dump() 超时/不可用，使用内存默认值启动")

        self._studio_url = _ensure_url(self._studio_host, self._studio_port, self._use_https)
        await self._init_http_client()

        # ⚠️ 不在启动时阻塞做健康检查，改为后台异步进行
        self._health_task = asyncio.create_task(self._health_check_loop())

        # ── 显示悬浮歌词窗口 & 队列窗口（后台任务，不阻塞 startup 返回）──
        self._windows_task = asyncio.create_task(self._init_floating_windows())

        return Ok({
            "status": "started",
            "studio_url": self._studio_url,
            "studio_available": self._studio_available,
            "message": "插件已启动，后台正在检查 RVC Studio 状态..."
        })

    async def _init_floating_windows(self):
        """后台初始化悬浮歌词 & 队列窗口。
        使用 run_in_executor 将 PySide6 窗口创建放到线程池，
        避免 Qt 初始化阻塞插件主事件循环。
        """
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, show_lyrics_window)
            await loop.run_in_executor(None, show_queue_window)
            # 跨线程回调：使用 call_soon_threadsafe 避免 no-running-loop 崩溃
            set_cancel_callback(lambda song_name: loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(
                    self._queue_engine.cancel(song_name), loop=loop)))
            self.logger.info("悬浮歌词窗口 & 队列窗口已启动（含取消回调节点）")
        except Exception as e:
            self.logger.warning(f"显示浮窗失败（非致命）: {e}")

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_):
        """清理资源"""
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
        if self._windows_task:
            self._windows_task.cancel()
            try:
                await self._windows_task
            except asyncio.CancelledError:
                pass
        # 取消并等待所有后台任务完成（带超时保护，防止卡死的任务阻塞退出）
        bg_tasks = list(self._bg_tasks)
        for task in bg_tasks:
            task.cancel()
        if bg_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*bg_tasks, return_exceptions=True),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                self.logger.warning("后台任务未在 5 秒内清理完成，强制继续关闭")
        # 清空队列
        await self._queue_engine.cancel_all()
        # 销毁浮窗（参考旧版 RVCStudio 最佳实践: 不仅隐藏，要完全销毁窗口对象）
        try:
            hide_lyrics_window()
            hide_queue_window()
            # 尝试调用销毁函数（如果存在）
            from .lyrics_window import destroy_lyrics_window
        except ImportError:
            pass
        else:
            destroy_lyrics_window()
        try:
            from .queue_window import destroy_queue_window
        except ImportError:
            pass
        else:
            destroy_queue_window()
        # 关闭 HTTP 客户端并置空
        if self._http_client:
            await self._http_client.close()
            self._http_client = None
        return Ok({"status": "shutdown"})

    # ═══════════════ 异步 HTTP 核心（使用 aiohttp，符合 NEKO asyncio 标准）═══════════════

    async def _check_studio_now(self):
        """立即检查一次 RVC Studio 状态，不抛异常（带智能重试）

        使用 aiohttp HTTP 客户端，符合 NEKO asyncio 标准。
        """
        valid, err_msg = _validate_host_port(self._studio_host, self._studio_port)
        if not valid:
            self._studio_available = False
            self._rvc_ready = False
            self.logger.error(f"配置错误: {err_msg}")
            return
        
        # 首次成功连接前保持激进重试；一旦连上过就降级为轻量重试
        is_first = not self._ever_connected

        max_retries = 8 if is_first else 3
        retry_delay = 1.5 if is_first else 0.8

        for attempt in range(max_retries):
            status, data = await self._http_client.get(
                "/api/health", timeout=A_HTTP_TIMEOUT_HEALTH,
            )
            if status == 200 and isinstance(data, dict):
                is_ok = data.get("status") == "ok"
                rvc_ready = data.get("rvc_ready", False)
                self._studio_available = is_ok
                self._rvc_ready = rvc_ready
                self._ever_connected = True
                if is_ok and not rvc_ready:
                    self.logger.warning("健康检查: Studio 在线但 RVC 引擎未就绪 (rvc_ready=false)")
                self._consecutive_failures = 0
                self.logger.info("健康检查成功: Studio 状态 = %s, rvc_ready=%s",
                                 self._studio_available, rvc_ready)
                await self._send_log_to_studio("info", f"健康检查通过: online={self._studio_available}, rvc_ready={rvc_ready}")

                # 每次健康检查成功都同步 GUI 设置（而非仅首次），
                # 确保用户在 GUI 中修改 default_model / auto_mix 后立即生效
                await self._sync_gui_config_from_studio()
                self._ever_connected_before = True
                return
            else:
                self._consecutive_failures += 1
                err = (isinstance(data, dict) and data.get("error")) or str(data) or f"HTTP {status}"
                self._last_error = err
                self.logger.warning("健康检查异常 (尝试 %d/%d): %s",
                                    attempt + 1, max_retries, err)
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
        
        # 所有重试都失败
        self._studio_available = False
        self._rvc_ready = False
        self.logger.warning(f"健康检查失败: 重试{max_retries}次后仍无法连接到 {self._studio_url}")


    async def _sync_gui_config_from_studio(self):
        """从 B 端 /api/config 同步用户在 GUI 中设置的首选项。

        解决 GUI 配置文件 (~/.rvc_studio/config.json) 与 NEKO 插件
        store/config 存储隔离的问题。同步项：
        - default_model: 默认模型名
        - auto_mix: 是否自动混音
        - pitch_shift: 默认变调
        - audio_preset: 后处理预设
        """
        try:
            s, cfg_data = await self._http_client.get("/api/config", timeout=A_HTTP_TIMEOUT_DEFAULT)
            if s == 200 and isinstance(cfg_data, dict):
                gui_model = cfg_data.get("default_model", "")
                if gui_model:
                    self._default_model = gui_model
                    self.logger.info("从 GUI 同步默认模型: %s", gui_model)
                else:
                    self.logger.debug("GUI 未设置默认模型，保持当前: %s", self._default_model)

                gui_auto_mix = cfg_data.get("auto_mix")
                if gui_auto_mix is not None:
                    self._auto_mix = bool(gui_auto_mix)
                    self.logger.info("从 GUI 同步混音设置: %s", self._auto_mix)

                gui_pitch = cfg_data.get("pitch_shift")
                if gui_pitch is not None:
                    self._last_pitch_shift = int(gui_pitch)

                gui_preset = cfg_data.get("audio_preset")
                if gui_preset:
                    self._last_postprocess = gui_preset

                # M6: 同步混音配置（预设 + 4 个滑块）
                mix_preset = cfg_data.get("mix_preset")
                if mix_preset:
                    self._mix_preset = str(mix_preset)
                mix_vocal_db = cfg_data.get("mix_vocal_db")
                if mix_vocal_db is not None:
                    try:
                        self._mix_vocal_db = float(mix_vocal_db)
                    except (TypeError, ValueError):
                        pass
                mix_inst_db = cfg_data.get("mix_inst_db")
                if mix_inst_db is not None:
                    try:
                        self._mix_inst_db = float(mix_inst_db)
                    except (TypeError, ValueError):
                        pass
                mix_reverb = cfg_data.get("mix_reverb")
                if mix_reverb is not None:
                    try:
                        self._mix_reverb = float(mix_reverb)
                    except (TypeError, ValueError):
                        pass
                mix_original = cfg_data.get("mix_original")
                if mix_original is not None:
                    try:
                        self._mix_original = float(mix_original)
                    except (TypeError, ValueError):
                        pass
        except Exception as e:
            self.logger.debug("同步 GUI 配置失败（非致命）: %s", e)


    def _full_status(self, **overrides) -> dict:
        """构造全量状态字典（修复：状态显示错误）

        NEKO 主程序的 apply_status_update 是**全量替换**（非合并），
        每次 report_status 都必须带全所有字段，否则面板上的
        studio_available / studio_url / merged_audio_url 等字段会丢失，
        导致完成后面板误显示"离线"红点或播放器数据被清空。
        _sticky_status 保存播放器数据（音频 URL/歌词/口型/对比结果），
        跨上报保留，直到新任务提交时清除。
        """
        status = {
            "status": "ready" if self._studio_available else "offline",
            "studio_available": self._studio_available,
            "rvc_ready": self._rvc_ready,
            "studio_url": self._studio_url,
            "active_task": self._active_task_id,
            "song_name": self._last_song_name,
            "model": self._last_model,
            "progress": self._last_progress,
            "step": self._last_step,
        }
        status.update(self._sticky_status)
        status.update(overrides)
        return status

    async def _health_check_loop(self):
        """后台健康检查 RVC Studio 是否在线，并上报状态

        修复：复用 _check_studio_now()，统一 URL（https/自定义端口）、
        User-Agent、API Key 鉴权与 SSL 上下文处理。
        此前硬编码 http://host:port 且不带鉴权头，HTTPS/远程场景下
        健康检查必然失败，手动重连成功后 30 秒内又被覆盖为离线。
        """
        self.logger.info("后台健康检查启动，等待 Studio 初始化...")
        # 暖机延迟：给 Studio 服务端启动时间（避免过早判定离线）
        await asyncio.sleep(HEALTH_WARMUP_DELAY)
        check_count = 0
        while True:
            check_count += 1
            try:
                prev_available = self._studio_available
                await self._check_studio_now()
                if self._studio_available != prev_available:
                    self.logger.info(
                        "[健康检查 #%d] 状态变化: %s -> %s",
                        check_count, prev_available, self._studio_available,
                    )

                # 上报状态给 NEKO 主程序（变化立即上报；无变化每 30 秒心跳一次）
                try:
                    current_status = "ready" if self._studio_available else "offline"
                    current_time = time.time()

                    should_report = False
                    if current_status != self._last_reported_status:
                        if current_time - self._last_status_update_time > self._status_change_threshold:
                            self._last_reported_status = current_status
                            self._last_status_update_time = current_time
                            should_report = True
                    else:
                        if current_time - self._last_status_update_time > 30:
                            self._last_status_update_time = current_time
                            should_report = True

                    # 有活跃任务时跳过心跳（避免覆盖 processing 进度状态）
                    if should_report and not self._active_task_id:
                        self.report_status(self._full_status(status=current_status))
                except Exception:
                    pass

            except Exception as e:
                self.logger.error(
                    "[健康检查 #%d] 异常: %s - %s",
                    check_count, type(e).__name__, e,
                )
                self.logger.error(
                    "[健康检查 #%d] 堆栈: %s",
                    check_count, traceback.format_exc(),
                )

            # 离线时加速轮询（10s），在线时保持 30s 心跳
            await asyncio.sleep(10 if not self._studio_available else 30)

    # ═══════════════ UI 入口（唯一调用链路） ═══════════════

    @plugin_entry(
        id="sing_song",
        name="唱首歌",
        description=(
            "让N.E.K.O用RVC训练好的声音唱歌。\n"
            "⚠️ 使用场景：用户说'唱首歌'、'来一首'、'sing a song'等请求时调用。\n"
            "⚠️ Studio 连接会自动检查，如果未连接会自动返回提示，无需调用额外工具确认。\n"
            "⚠️ 模型选择：\n"
            "  • 用户明确说'用XX模型唱'、'换XX唱'时，传入model_name参数\n"
            "  • 否则使用默认模型（不要主动询问用什么模型）\n"
            "  • 可用模型列表通过 check_studio_status 查询\n"
            "🔑 歌曲名规则：song_name 必须使用 search_and_download_song 返回的 song_name 字段！\n"
            "  不要用你自己想的搜索词，因为平台存储的歌名可能不同（如日文原名 vs 中文翻译）。\n"
            "  例如：搜「怪物之歌」时返回的 song_name 是「バケモノの唄」，必须用后者调用本入口。\n"
            "⚠️ 歌曲自动下载流程：\n"
            "  1. 先调用本入口提交唱歌请求\n"
            "  2. 如果返回错误'未找到歌曲音频文件'，不要直接让用户上传文件！\n"
            "  3. 而是调用 search_and_download_song 自动从网络下载歌曲\n"
            "  4. 下载成功 → 用返回的 song_name（不是搜索词）再次调用 sing_song\n"
            "⚠️ 绝对不要直接让用户提供本地文件路径，优先用 search_and_download_song 联网下载。\n"
            "如果 song_name 为空，先询问用户想听什么歌。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "song_name": {
                    "type": "string",
                    "description": (
                        "歌曲名称。如果刚用 search_and_download_song 下载了歌曲，"
                        "必须填写其返回结果中的 song_name（不要用你自己的搜索词，因为缓存的歌名可能不同）"
                    )
                },
                "model_name": {
                    "type": "string",
                    "description": (
                        "RVC模型名称。用户明确说'用XX模型唱'、'换XX唱'时填写，"
                        "否则留空使用默认模型。可用模型可通过 check_studio_status 查询。"
                    ),
                    "default": ""
                },
                "pitch_shift": {
                    "type": "integer",
                    "description": "变调，半音数量，0为不变",
                    "default": 0
                },
                "postprocess": {
                    "type": "string",
                    "description": (
                        "音效后处理预设（可选）：none=无(默认) / studio=录音棚(降噪+提亮+压缩) / "
                        "live=现场(轻混响) / ktv=KTV(重混响) / bright=明亮(高频提升) / "
                        "warm=温暖(低频增强) / denoise=纯净(强降噪)。"
                        "用户说'加点混响'选 live 或 ktv，'声音干净点'选 studio 或 denoise"
                    ),
                    "default": "none"
                },
            },
        },
        llm_result_fields=["status", "task_id", "song_name"],
        timeout=15.0,  # 入口快速返回，后台异步处理
    )
    async def sing_song(
        self,
        song_name: str = "",
        model_name: str = "",
        pitch_shift: int = 0,
        postprocess: str = "none",
        # M6: 混音配置（可选，未传则用 GUI 同步的 self._mix_*）
        mix_preset: str | None = None,
        mix_vocal_db: float | None = None,
        mix_inst_db: float | None = None,
        mix_reverb: float | None = None,
        mix_original: float | None = None,
        **_,
    ):
        """主入口：处理唱歌请求

        采用异步模式：
        1. 立即提交任务到 RVC Studio
        2. 返回 task_id，由后台协程等待完成
        3. 后台通过 push_message 推送进度和最终结果
        4. 避免入口函数超时（默认 30s）
        
        改进：并发控制、参数验证、模型切换支持
        """
        # ── 参数验证 ──
        if not song_name or not isinstance(song_name, str):
            return Err(SdkError("歌曲名称必须是非空字符串"))
        
        song_name = song_name.strip()
        if not song_name:
            return Err(SdkError(
                "喵～主人还没说要唱什么歌呢！请告诉我歌曲名称吧～"
            ))
        
        if pitch_shift is not None and not isinstance(pitch_shift, int):
            return Err(SdkError("变调参数必须是整数"))
        
        pitch_shift = pitch_shift or 0
        if not -12 <= pitch_shift <= 12:
            return Err(SdkError("变调范围必须在 -12 到 +12 之间"))

        # 音效后处理预设（非法值回退 none，B 端也会二次校验）
        _valid_pp = {"none", "studio", "live", "ktv", "bright", "warm", "denoise"}
        postprocess = str(postprocess or "none").strip().lower()
        if postprocess not in _valid_pp:
            postprocess = "none"

        # ── 模型选择：用户指定 > 默认模型 > 自动选择第一个可用模型 ──
        model_name = (model_name or "").strip()
        
        # 先拉取可用模型列表（用于校验和自动选择）
        available_models = []
        if self._studio_available:
            try:
                status, data = await self._http_client.get("/api/models", timeout=A_HTTP_TIMEOUT_DEFAULT)
                if status == 200 and isinstance(data, dict):
                    available_models = data.get("models", [])
            except Exception as e:
                self.logger.warning(f"获取模型列表失败: {e}")
        
        if model_name:
            # 用户明确指定模型，校验模型是否存在
            if available_models:
                model_stem = model_name.replace(".pth", "")
                if not any(m.replace(".pth", "") == model_stem for m in available_models):
                    return Err(SdkError(
                        f"喵～找不到模型「{model_name}」呢！\n"
                        f"可用模型: {', '.join(available_models[:10])}\n"
                        "可以调用 check_studio_status 查看完整模型列表哦～"
                    ))
                model = model_stem
            else:
                model = model_name.replace(".pth", "")
                self.logger.warning(f"无法校验模型 {model_name}，将尝试使用")
            self.logger.info(f"用户指定模型: {model}")
        else:
            # 使用默认模型，如果为空或不存在则自动选择第一个
            if self._default_model and available_models:
                default_stem = self._default_model.replace(".pth", "")
                if any(m.replace(".pth", "") == default_stem for m in available_models):
                    model = default_stem
                    self.logger.info(f"使用默认模型: {model}")
                else:
                    # 默认模型在 API 返回的列表中不存在（可能是名称差异或模型被删除）
                    # 仅回退到第一个可用模型用于本次演唱，不持久化覆盖用户选择
                    old_default = self._default_model
                    model = available_models[0].replace(".pth", "")
                    self.logger.warning(
                        "默认模型「%s」不在可用模型列表中，本次回退到: %s（不持久化）",
                        old_default, model
                    )
            elif available_models:
                model = available_models[0].replace(".pth", "")
                self.logger.info("未设置默认模型，自动选择: %s", model)
                # 仅当之前确实没有默认模型时才持久化
                if not self._default_model:
                    self._default_model = model
                    try:
                        await self.store.set("settings", {"default_model": model})
                    except Exception:
                        pass
            else:
                model = self._default_model.replace(".pth", "") if self._default_model else ""

        # ── 并发控制：原子化抢占 active_task_id ──
        async with self._task_lock:
            if self._active_task_id:
                ok, msg = await self._queue_engine.enqueue(
                    song_name, model, pitch_shift
                )
                if ok:
                    return Ok({
                        "status": "queued",
                        "message": msg,
                        "song_name": song_name,
                    })
                return Err(SdkError(msg))
            # 抢先标记活跃任务，防止并发重复提交
            self._active_task_id = "__pending__"

        # ── 快速检查 RVC Studio 是否在线 ──
        if not self._studio_available:
            async with self._task_lock:
                self._active_task_id = None
            return Err(SdkError(
                "喵～RVC Studio 还没启动的说！\n"
                "请先打开 RVC Studio 独立程序，确保它显示'就绪'状态后再试哦～\n"
                f"预期地址: {self._studio_url}"
            ))

        # model 已在并发控制前计算，此处仅更新状态
        self._last_model = model
        self._last_pitch_shift = pitch_shift
        self._last_postprocess = postprocess
        self._last_task_type = "sing"
        # M6: 记住本次的混音参数（重试时回放）
        if mix_preset is not None:
            self._mix_preset = str(mix_preset)
        if mix_vocal_db is not None:
            try:
                self._mix_vocal_db = float(mix_vocal_db)
            except (TypeError, ValueError):
                pass
        if mix_inst_db is not None:
            try:
                self._mix_inst_db = float(mix_inst_db)
            except (TypeError, ValueError):
                pass
        if mix_reverb is not None:
            try:
                self._mix_reverb = float(mix_reverb)
            except (TypeError, ValueError):
                pass
        if mix_original is not None:
            try:
                self._mix_original = float(mix_original)
            except (TypeError, ValueError):
                pass
        self.logger.info(f"唱歌请求: song={song_name}, model={model}, pitch={pitch_shift}, pp={postprocess}, mix={self._mix_preset}/{self._mix_vocal_db}/{self._mix_inst_db}/{self._mix_reverb}/{self._mix_original}")
        await self._send_log_to_studio("info", f"收到演唱请求: song={song_name}, model={model}, pitch={pitch_shift}, pp={postprocess}")

        # ── 提交处理请求到 RVC Studio ──
        task_result = await self._submit_song_task(
            song_name=song_name,
            model_name=model,
            pitch_shift=pitch_shift,
            postprocess=postprocess,
            # M6: 透传混音配置
            mix_preset=mix_preset,
            mix_vocal_db=mix_vocal_db,
            mix_inst_db=mix_inst_db,
            mix_reverb=mix_reverb,
            mix_original=mix_original,
        )
        if isinstance(task_result, Err):
            async with self._task_lock:
                self._active_task_id = None
            return task_result

        task_data = task_result.value
        task_id = task_data["task_id"]

        # 正式标记 active_task_id
        async with self._task_lock:
            self._active_task_id = task_id
        self._submitted_tasks[task_id] = time.time()
        self._cancel_event.clear()

        # 发送"开始处理"消息（blind：仅通知，AI 不回应）
        try:
            self.push_message(
                source="rvc_singer",
                visibility=["chat"],
                ai_behavior="blind",
                parts=[{"type": "text", "text": f"喵～收到！正在学唱《{song_name}》... 🎵"}],
                priority=3,
            )
        except Exception:
            self.logger.debug("开始处理消息推送失败（不影响合成）")

        # 上报状态（清除上一首的播放器数据，带全连接字段）
        try:
            self._sticky_status = {}
            self.report_status(self._full_status(
                status="processing",
                active_task=task_id,
                song_name=song_name,
                model=model,
                progress=0,
                step="已提交",
            ))
        except Exception:
            pass

        # 启动后台等待任务（不阻塞入口返回，避免超时）
        bg_task = asyncio.create_task(self._bg_wait_and_push(task_id, song_name))
        self._bg_tasks.add(bg_task)
        bg_task.add_done_callback(lambda t: self._bg_tasks.discard(t))

        return Ok({
            "status": "processing",
            "task_id": task_id,
            "song_name": song_name,
        })

    @ui.action(
        label="⏹ 取消任务",
        icon="⏹",
        tone="danger",
        group="control",
        order=1,
        refresh_context=True,
    )
    @plugin_entry(
        id="cancel_song",
        name="取消当前任务",
        description=(
            "强制取消当前正在执行的演唱/对比任务。\n"
            "⚠️ 使用场景：\n"
            "  1. 当前任务卡住不动时（sing_song 报'正在唱另一首歌'但看起来已卡死）。\n"
            "  2. 用户说'取消'、'别唱了'、'停下'等请求时。\n"
            "⚠️ 取消后需要等待几秒让 RVC Studio 释放资源，然后可以重新提交。"
        ),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["cancelled", "previously_active_id", "message"],
    )
    async def cancel_song(self, **_):
        """强制取消当前活跃任务，释放并发锁"""
        prev_id = self._active_task_id
        cancelled_count = 0

        # 1) 发信号给轮询循环
        if prev_id:
            self._cancel_event.set()
            self.logger.info(f"取消信号已发送: task_id={prev_id}")

        # 2) 取消后台任务 + 清除活跃标记（原子操作）
        async with self._task_lock:
            tasks = [t for t in self._bg_tasks if not t.done()]
            for t in tasks:
                t.cancel()
                cancelled_count += 1
            self._bg_tasks.clear()
            self._active_task_id = None

        # 等待被取消的任务完成清理（finally 块执行 _active_task_id 释放等）
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # 2.5) 通知 B 端取消任务（释放 GPU 资源，避免 B 端空跑）
        if prev_id:
            try:
                await self._http_client.post(
                    f"/api/task/{prev_id}/cancel",
                    json_data={},
                    timeout=A_HTTP_TIMEOUT_DEFAULT,
                )
            except Exception:
                pass
            # 清理提交记录，防止长期运行内存泄漏
            self._submitted_tasks.pop(prev_id, None)

        # 2.6) 恢复 LLM 对话
        try:
            await self._clear_busy_signal()
        except Exception:
            pass

        # 3) 通知用户
        if prev_id:
            if self._last_task_type == "compare":
                msg = f"喵～已取消对比任务 {prev_id}，现在可以重新开始对比啦！🎵"
            else:
                msg = f"喵～已取消任务 {prev_id}，现在可以重新点歌啦！🎵"
            self.push_message(
                source="rvc_singer",
                visibility=["chat"],
                ai_behavior="blind",
                parts=[{"type": "text", "text": msg}],
                priority=3,
            )
        else:
            self.push_message(
                source="rvc_singer",
                visibility=["chat"],
                ai_behavior="blind",
                parts=[{"type": "text", "text": "喵～现在没有正在执行的任务哦～"}],
                priority=3,
            )

        # 3.5) 清空歌曲队列
        await self._queue_engine.cancel_all()
        try:
            push_lyrics_data({"action": "stop"})
            push_queue_data({"action": "update", "snapshot": {
                "items": [], "now_playing": None, "queue_size": 0
            }})
        except Exception:
            pass

        # 4) 重置面板状态
        self._last_progress = 0
        self._last_step = "已取消"
        self.report_status(self._full_status(status="ready"))

        self.logger.info(
            f"取消任务完成: prev_id={prev_id}, cancelled_tasks={cancelled_count}"
        )

        return Ok({
            "cancelled": bool(prev_id),
            "previously_active_id": prev_id,
            "message": "任务已取消" if prev_id else "没有活跃任务需要取消",
        })

    @plugin_entry(
        id="check_studio_status",
        name="检查RVC Studio状态",
        description="检查 RVC Studio 独立程序是否在线、可用模型列表、当前状态。用于查询可用的RVC声音模型。",
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["online", "models", "status", "default_model"],
    )
    async def check_studio_status(self, **_):
        """检查 RVC Studio 状态并返回可用模型列表"""
        if not self._studio_available:
            return Ok({
                "online": False,
                "models": [],
                "status": "RVC Studio 未启动",
                "message": "请先启动 RVC Studio 独立程序",
                "default_model": self._default_model,
            })

        status, data = await self._http_client.get(
            "/api/status", timeout=A_HTTP_TIMEOUT_DEFAULT,
        )
        if status == 200 and isinstance(data, dict):
            # 额外获取详细模型列表（/api/models）
            models_list = []
            try:
                m_status, m_data = await self._http_client.get("/api/models", timeout=A_HTTP_TIMEOUT_DEFAULT)
                if m_status == 200 and isinstance(m_data, dict):
                    models_list = m_data.get("models", [])
            except Exception:
                models_list = data.get("models", [])
            
            result = {
                "online": True,
                "status": data.get("status", "unknown"),
                "rvc_ready": data.get("rvc_ready", False),
                "models": models_list,
                "model_count": len(models_list),
                "default_model": self._default_model,
                "current_model": data.get("current_model", ""),
                "message": f"RVC Studio 在线 | 可用模型: {len(models_list)} 个 | 默认模型: {self._default_model}"
            }
            return Ok(result)
        elif status == -1:
            err = data.get("error", "unknown") if isinstance(data, dict) else str(data)
            return Err(SdkError(
                f"连接失败: {err}。下一步：调用 reconnect_studio 重连，"
                "或调用 check_studio_status 查看详细状态"))
        else:
            return Err(SdkError(f"获取状态失败: HTTP {status}"))

    @ui.action(
        label="🔗 重新连接",
        icon="🔗",
        tone="primary",
        group="connection",
        order=5,
        refresh_context=True,
    )
    @plugin_entry(
        id="reconnect_studio",
        name="重连RVC Studio",
        description=(
            "强制重新连接 RVC Studio 独立程序。\n"
            "⚠️ 调用时机：RVC Studio 启动/重启后，或连接意外中断时。\n"
            "⚠️ 此操作会重新读取配置、测试连接、清除缓存数据、并推送连接结果到聊天窗口。"
        ),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["online", "message", "studio_url", "models"],
    )
    async def reconnect_studio(self, **_):
        """手动重连 RVC Studio

        流程：
        1. 从 store + config 重新读取最新配置
        2. 重建目标 URL
        3. 清除所有缓存数据
        4. 同步执行健康检查（不抛异常）
        5. 推送连接结果到聊天窗口 + 上报 report_status
        """
        # ── 1. 重新读取 store 中的最新配置 ──
        try:
            store_data = await self.store.get("settings", {})
            settings = store_data.value if hasattr(store_data, "value") else store_data
            if isinstance(settings, dict) and settings:
                self._studio_host = settings.get("rvc_studio_host", self._studio_host)
                self._studio_port = int(settings.get("rvc_studio_port", self._studio_port))
                self._rvc_root = settings.get("rvc_root_path", self._rvc_root)
                # 仅当 store 中有非空模型名时才覆盖，避免空字符串冲掉 GUI 同步的值
                store_model = settings.get("default_model", "")
                if store_model:
                    self._default_model = store_model
                self.logger.info("从 store 读取配置: host=%s, port=%d", self._studio_host, self._studio_port)
        except Exception as e:
            self.logger.warning("读取 store 配置失败，回退到内存缓存: %s", e)

        # ── 2. 同步 config 到内存（带超时保护）──
        try:
            cfg = await asyncio.wait_for(self.config.dump(), timeout=5.0)
            cfg_settings = cfg.get("settings", {})
            if cfg_settings:
                self._studio_host = cfg_settings.get("rvc_studio_host", self._studio_host)
                self._studio_port = int(cfg_settings.get("rvc_studio_port", self._studio_port))
                self._api_key = str(cfg_settings.get("api_key", self._api_key) or "").strip()
                self._use_https = bool(cfg_settings.get("use_https", self._use_https))
                self._ssl_verify = bool(cfg_settings.get("ssl_verify", self._ssl_verify))
                # 仅当 config 中有非空模型名时才覆盖
                cfg_model = cfg_settings.get("default_model", "")
                if cfg_model:
                    self._default_model = cfg_model
                self._auto_download_on_miss = bool(cfg_settings.get("auto_download_on_miss", self._auto_download_on_miss))
                self._not_found_max_retries = int(cfg_settings.get("not_found_max_retries", self._not_found_max_retries))
        except (asyncio.TimeoutError, Exception):
            self.logger.warning("config.dump() 超时，使用 store/内存缓存继续")

        # ── 3. 重建目标 URL + HTTP 客户端 + 清除缓存 ──
        self._studio_url = _ensure_url(self._studio_host, self._studio_port, self._use_https)
        await self._init_http_client()
        # M43: 选择性清理 — 只清与连接相关的缓存，保留其他类型
        await self._cache.delete_prefix("http_")
        await self._cache.delete("songs")  # 旧 URL 的缓存必定失效
        await self._cache.delete("models")
        self._consecutive_failures = 0
        self._last_reported_status = ""
        self.logger.info("手动重连: 目标 = %s", self._studio_url)
        await self._send_log_to_studio("info", f"手动重连: 目标={self._studio_url}")

        # ── 4. 同步健康检查（内部会调用 _sync_gui_config_from_studio 同步 default_model/auto_mix）──
        await self._check_studio_now()

        # ── 5. 推送结果 ──
        if self._studio_available:
            # 获取模型列表用于展示
            model_names = []
            try:
                s, d = await self._http_client.get("/api/models", timeout=A_HTTP_TIMEOUT_DEFAULT)
                if s == 200 and isinstance(d, dict):
                    model_names = d.get("models", [])
            except Exception:
                pass

            msg = f"喵～已经连上 RVC Studio 啦！✨\n地址: {self._studio_url}"
            if model_names:
                msg += f"\n可用模型: {', '.join(model_names[:5])}"
                if len(model_names) > 5:
                    msg += f" 等 {len(model_names)} 个"

            self.push_message(
                source="rvc_singer",
                visibility=["chat"],
                ai_behavior="respond",
                parts=[{"type": "text", "text": msg}],
                priority=5,
            )

            # 上报状态并同步防抖记录（修复：重连成功后健康检查
            # 心跳窗口内不会再用旧状态覆盖面板显示）
            # 清除旧粘性状态（音频 URL 可能指向旧服务器，切换后已无效）
            self._sticky_status = {}
            self._last_reported_status = "ready"
            self._last_status_update_time = time.time()
            self.report_status(self._full_status(status="ready"))

            return Ok({
                "online": True,
                "message": "连接成功",
                "studio_url": self._studio_url,
                "models": model_names,
            })
        else:
            msg = (
                f"喵呜... 连不上 RVC Studio 😿\n"
                f"目标地址: {self._studio_url}\n"
                f"请确认 RVC Studio 已启动并在正确的端口运行喵～"
            )

            self.push_message(
                source="rvc_singer",
                visibility=["chat"],
                ai_behavior="respond",
                parts=[{"type": "text", "text": msg}],
                priority=5,
            )

            self._last_reported_status = "offline"
            self._last_status_update_time = time.time()
            self.report_status(self._full_status(status="offline"))

            return Ok({
                "online": False,
                "message": "连接失败",
                "studio_url": self._studio_url,
                "models": [],
            })

    @plugin_entry(
        id="list_songs",
        name="查看已有歌曲",
        description=(
            "列出歌曲库中所有已索引的歌曲。支持搜索、筛选和分页。\n"
            "⚠️ 调用时机：用户问'有哪些歌'、'能唱什么歌'时，或在 sing_song 报'未找到歌曲'后让用户了解现有歌曲列表。\n"
            "⚠️ 参数说明：\n"
            "  - q: 搜索关键词，模糊匹配标题/歌手/标签\n"
            "  - artist: 按歌手筛选\n"
            "  - source: 按来源筛选 (local/youtube/bilibili/upload)\n"
            "  - sort: 排序字段 (title/artist/duration_sec/created_at)\n"
            "  - limit: 每页数量，默认50，最大200"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "搜索关键词，搜索标题和歌手名", "default": ""},
                "artist": {"type": "string", "description": "按歌手筛选", "default": ""},
                "source": {"type": "string", "description": "按来源筛选: local/youtube/bilibili/upload", "default": ""},
                "sort": {"type": "string", "description": "排序: title/artist/duration_sec/created_at", "default": "created_at"},
                "limit": {"type": "integer", "description": "返回数量，默认50", "default": 50},
            },
        },
        llm_result_fields=["songs", "total", "count", "message"],
    )
    async def list_songs(self, q: str = "", artist: str = "", source: str = "",
                         sort: str = "created_at", limit: int = 50, **_):
        """列出已有歌曲（SQLite 歌曲库）"""
        if not self._studio_available:
            return Err(SdkError(
                "RVC Studio 未启动，无法获取歌曲列表。"
                "下一步：提醒用户启动 RVC Studio，然后调用 reconnect_studio 重连"))

        params = {}
        if q.strip():
            params["q"] = q.strip()
        if artist.strip():
            params["artist"] = artist.strip()
        if source.strip():
            params["source"] = source.strip()
        if sort:
            params["sort"] = sort
        params["limit"] = min(limit, 200) if limit else 50
        params["order"] = "desc"

        query_string = urlencode(params)
        endpoint = f"/api/songs?{query_string}"
        status, data = await self._http_client.get(endpoint, timeout=A_HTTP_TIMEOUT_DEFAULT)
        if status == 200:
            result = data if isinstance(data, dict) else {"raw": data}
            song_count = result.get("count", result.get("total", 0))
            if q:
                result["message"] = f"搜索「{q}」找到 {song_count} 首歌"
            else:
                result["message"] = f"歌曲库共有 {result.get('total', song_count)} 首歌"
            return Ok(result)
        elif status == -1:
            err = data.get("error", "unknown") if isinstance(data, dict) else str(data)
            return Err(SdkError(f"连接失败: {err}"))
        else:
            return Err(SdkError(f"获取歌曲列表失败: HTTP {status}"))

    @plugin_entry(
        id="upload_song",
        name="上传歌曲",
        description=(
            "让用户上传歌曲音频文件到 RVC Studio 的歌曲库（songs/ 目录），供后续唱歌使用。\n"
            "⚠️ 调用时机：仅在联网下载也失败时作为最后手段使用。优先使用 search_and_download_song。\n"
            "⚠️ 用户需要提供歌曲文件的本地绝对路径。\n"
            "⚠️ 上传完成后，应再次调用 sing_song 来演唱该歌曲。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "歌曲音频文件的本地绝对路径（wav/mp3/flac/m4a/ogg/mp4）"
                },
                "song_name": {
                    "type": "string",
                    "description": "歌曲名称（用于后续识别），如果不提供则使用文件名",
                    "default": ""
                },
            },
            "required": ["file_path"],
        },
        llm_result_fields=["status", "filename", "message"],
    )
    async def upload_song(self, file_path: str = "", song_name: str = "", **_):
        """上传歌曲到 RVC Studio"""
        if not self._studio_available:
            return Err(SdkError(
                "RVC Studio 未启动，无法上传歌曲。"
                "下一步：提醒用户启动 RVC Studio，然后调用 reconnect_studio 重连"))

        if not file_path:
            return Err(SdkError("请提供歌曲文件的本地绝对路径"))

        # 通过 HTTP API 告诉 RVC Studio 去复制文件（因为插件和 Studio 可能不在同一进程/机器）
        payload = {
            "file_path": file_path,
            "song_name": song_name.strip() if song_name else "",
        }
        status, data = await self._http_client.post(
            "/api/upload", json_data=payload, timeout=A_HTTP_TIMEOUT_UPLOAD,
        )
        if status == 200:
            return Ok(data if isinstance(data, dict) else {"raw": data})
        elif status == -1:
            err = data.get("error", "unknown") if isinstance(data, dict) else str(data)
            return Err(SdkError(f"上传歌曲失败: {err}"))
        else:
            error_text = data if isinstance(data, str) else json.dumps(data)
            return Err(SdkError(f"上传失败: {error_text}"))

    @plugin_entry(
        id="search_and_download_song",
        name="联网搜索下载歌曲",
        description=(
            "自动从网络搜索并下载歌曲音频文件。\n"
            "⚠️ 调用时机：\n"
            "  1. sing_song 返回'未找到歌曲'错误时，应首先调用此入口自动下载\n"
            "  2. 用户说'帮我找某首歌'时\n"
            "⚠️ 这个入口会从 YouTube/Bilibili 等平台搜索并下载音频，无需用户提供文件。\n"
            "⚠️ 下载完成后，应再次调用 sing_song 来演唱该歌曲。\n"
            "⚠️ 如果下载也失败，最后才考虑用 upload_song 让用户手动上传。\n"
            "🔑 关键：返回结果中的 song_name 字段是歌曲在服务器上的实际保存名称，\n"
            "  可能与你的搜索词不同（如搜「怪物之歌」实际保存为「バケモノの唄」）。\n"
            "  后续调用 sing_song 时，必须使用返回结果中的 song_name，不要用搜索词！"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "song_name": {
                    "type": "string",
                    "description": "要搜索下载的歌曲名称"
                },
                "artist": {
                    "type": "string",
                    "description": "歌手名（可选，帮助精确搜索）",
                    "default": ""
                },
                "source": {
                    "type": "string",
                    "description": "搜索源：auto(自动) / youtube / bilibili",
                    "default": "auto"
                },
            },
            "required": ["song_name"],
        },
        llm_result_fields=["status", "song_name", "query_name", "message", "video_title", "size_mb"],
    )
    async def search_and_download_song(
        self,
        song_name: str = "",
        artist: str = "",
        source: str = "auto",
        **_
    ):
        """联网搜索并下载歌曲音频"""
        if not self._studio_available:
            return Err(SdkError(
                "RVC Studio 未启动，无法搜索下载歌曲。"
                "下一步：提醒用户启动 RVC Studio，然后调用 reconnect_studio 重连"))

        if not song_name.strip():
            return Err(SdkError("请提供要搜索的歌曲名称"))

        # 先推送提示消息
        self.push_message(
            source="rvc_singer",
            visibility=["chat"],
            ai_behavior="blind",
            parts=[{"type": "text", "text": f"喵～正在网上帮主人搜《{song_name}》的原曲... 翻箱倒柜中 🔍"}],
            priority=3,
        )

        payload = {
            "song_name": song_name.strip(),
            "artist": artist.strip() if artist else "",
            "source": source,
        }

        for attempt in range(_MAX_RETRY_ATTEMPTS):
            if attempt > 0:
                # 重试时推送提示（不重复发搜索消息）
                self.push_message(
                    source="rvc_singer",
                    visibility=["chat"],
                    ai_behavior="blind",
                    parts=[{"type": "text", "text": f"喵～第一次没找到，再帮主人找找看... (第{attempt + 1}次尝试) 🔄"}],
                    priority=3,
                )

            status, data = await self._http_client.post(
                "/api/search_and_download", json_data=payload, timeout=A_HTTP_TIMEOUT_SEARCH,
            )
            if status == 200:
                if not isinstance(data, dict):
                    return Err(SdkError("响应格式异常"))
                if data.get("status") == "ok":
                    # 记录下载后的实际歌曲名（可能与搜索词不同，如中日文差异）
                    actual_name = data.get("song_name") or song_name
                    self._last_song_name = actual_name
                    self.push_message(
                        source="rvc_singer",
                        visibility=["chat"],
                        ai_behavior="blind",
                        parts=[{"type": "text", "text": f"喵～{data.get('message', '下载完成啦')} ✨"}],
                        priority=3,
                    )
                    return Ok(data)
                elif data.get("status") == "already_exists":
                    actual_name = data.get("song_name") or song_name
                    self._last_song_name = actual_name
                    return Ok(data)
                else:
                    err_msg = data.get("message", data.get("error", "下载失败"))
                    if attempt < _MAX_RETRY_ATTEMPTS - 1:
                        self.logger.warning(
                            f"搜索下载失败，重试... (尝试 {attempt + 1}/{_MAX_RETRY_ATTEMPTS}): {err_msg}"
                        )
                        await asyncio.sleep(HEALTH_WARMUP_DELAY)
                        continue
                    return Err(SdkError(err_msg))
            elif status == -1:
                err = data.get("error", "unknown") if isinstance(data, dict) else str(data)
                if attempt < _MAX_RETRY_ATTEMPTS - 1:
                    self.logger.warning(
                        f"搜索下载网络错误，重试... (尝试 {attempt + 1}/{_MAX_RETRY_ATTEMPTS}): {err}"
                    )
                    await asyncio.sleep(3)
                    continue
                if "timeout" in err.lower():
                    return Err(SdkError("搜索下载超时（5分钟），请重试或换一首歌"))
                return Err(SdkError(f"搜索下载失败: {err}"))
            else:
                error_msg = data.get("message", data.get("error", f"HTTP {status}")) if isinstance(data, dict) else str(data)
                if attempt < _MAX_RETRY_ATTEMPTS - 1:
                    self.logger.warning(
                        f"搜索下载服务错误，重试... (尝试 {attempt + 1}/{_MAX_RETRY_ATTEMPTS}): {error_msg}"
                    )
                    await asyncio.sleep(HEALTH_WARMUP_DELAY)
                    continue
                return Err(SdkError(error_msg))

        # 兜底：已达最大重试次数
        return Err(SdkError("搜索下载失败（已达最大重试次数）"))

    @plugin_entry(
        id="compare_voices",
        name="多模型对比试听",
        description=(
            "用 2~4 个不同的 RVC 音色模型演唱同一段歌曲片段，生成 A/B 对比试听。\n"
            "⚠️ 使用场景：用户说'对比一下音色'、'哪个模型唱得好'、'A/B 测试'等请求时调用。\n"
            "⚠️ 前置条件：RVC Studio 必须已启动；歌曲必须已存在（否则先 search_and_download_song）。\n"
            "⚠️ 结果在「演唱播放器」面板中试听（每个模型一个试听按钮），不在聊天窗口播放。\n"
            "⚠️ 如果用户没有指定模型，无需询问，直接传空 models 数组，后台会自动拉取可用模型并选取前 4 个进行对比。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "song_name": {
                    "type": "string",
                    "description": "歌曲名称（必填）"
                },
                "models": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要对比的 RVC 模型名称列表（2~4 个）。留空或省略则自动获取可用模型选取前 4 个。"
                },
                "pitch_shift": {
                    "type": "integer",
                    "description": "变调，半音数量，0为不变",
                    "default": 0
                },
                "clip_seconds": {
                    "type": "integer",
                    "description": "对比片段时长（秒），10~60，默认30",
                    "default": 30
                },
            },
            "required": ["song_name"],
        },
        llm_result_fields=["status", "song_name", "message", "models"],
        timeout=15.0,  # 入口快速返回，后台异步处理
    )
    async def compare_voices(
        self,
        song_name: str = "",
        models: list | None = None,
        pitch_shift: int = 0,
        clip_seconds: int = 30,
        **_,
    ):
        """A/B 对比入口：同一段人声用多个模型各转一遍，结果推送到播放器面板试听

        异步模式（与 sing_song 相同）：
        1. 提交 POST /api/compare 拿 task_id 后快速返回
        2. 后台协程轮询任务，完成后 report_status 推 compare_results 给 player.html
        3. push_message 只发文本通知（NEKO 播放链路，不走浏览器）
        """
        # ── 参数验证 ──
        if not song_name or not isinstance(song_name, str) or not song_name.strip():
            return Err(SdkError("喵～请告诉我要对比的歌曲名称哦～"))
        song_name = song_name.strip()

        if not models or not isinstance(models, list) or len(models) < 2:
            # 自动获取可用模型，无需打扰用户
            try:
                s, d = await self._http_client.get("/api/models", timeout=A_HTTP_TIMEOUT_DEFAULT)
                if s == 200 and isinstance(d, dict) and d.get("models"):
                    available = d["models"][:4]
                    if len(available) >= 2:
                        models = available
                        self.logger.info(f"auto-selected models for compare: {models}")
            except Exception:
                pass
        if not models or not isinstance(models, list) or len(models) < 2:
            return Err(SdkError("当前可用模型不足 2 个，无法进行对比"))

        model_list = list(dict.fromkeys(str(m).strip() for m in models if str(m).strip()))
        if not 2 <= len(model_list) <= 4:
            return Err(SdkError(f"对比需要 2~4 个模型，当前给了 {len(model_list)} 个"))

        pitch_shift = int(pitch_shift) if pitch_shift else 0
        if not -12 <= pitch_shift <= 12:
            return Err(SdkError("变调范围必须在 -12 到 +12 之间"))

        try:
            clip_seconds = int(clip_seconds) if clip_seconds else 30
        except (ValueError, TypeError):
            clip_seconds = 30
        clip_seconds = max(10, min(60, clip_seconds))

        self._last_task_type = "compare"
        self._last_model = model_list[0] if model_list else ""
        self._sticky_status = {}  # 清除上一首歌曲的播放器数据

        # ── 并发控制 ──
        async with self._task_lock:
            if self._active_task_id:
                return Err(SdkError(
                    f"喵～现在还有任务在处理呢（任务 {self._active_task_id}），请等它完成再来对比哦～"
                ))

        if not self._studio_available:
            return Err(SdkError(
                "喵～RVC Studio 还没启动的说！\n"
                "请先打开 RVC Studio 独立程序哦～\n"
                f"预期地址: {self._studio_url}。下一步：调用 reconnect_studio 重连"
            ))

        self.logger.info(f"A/B 对比请求: song={song_name}, models={model_list}, clip={clip_seconds}s")
        await self._send_log_to_studio("info", f"收到对比请求: song={song_name}, models={model_list}")

        # ── 提交对比任务（重试与错误分类对齐 _submit_song_task）──
        payload = {
            "song_name": song_name,
            "models": model_list,
            "pitch_shift": pitch_shift,
            "clip_seconds": clip_seconds,
        }
        task_id = None
        for attempt in range(_MAX_RETRY_ATTEMPTS):
            status, data = await self._http_client.post(
                "/api/compare", json_data=payload, timeout=10,
            )
            if status == 200 and isinstance(data, dict):
                task_id = data.get("task_id")
                break
            elif status == -1:
                err_info = data.get("error", str(data)) if isinstance(data, dict) else str(data)
                if attempt < _MAX_RETRY_ATTEMPTS - 1:
                    self.logger.warning(f"提交对比任务失败，重试... ({attempt + 1}/{_MAX_RETRY_ATTEMPTS}): {err_info}")
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                return Err(SdkError(
                    f"提交对比任务超时（重试{_MAX_RETRY_ATTEMPTS}次仍失败）: {err_info}。"
                    "下一步：调用 check_studio_status 确认服务状态后重试 compare_voices"))
            elif status == 400 and isinstance(data, dict):
                err_msg = data.get("error", "参数错误")
                avail = data.get("available_models") or []
                if avail:
                    err_msg += f"\n可用模型: {', '.join(avail[:8])}"
                return Err(SdkError(err_msg))
            elif status == 429:
                return Err(SdkError("RVC Studio 任务队列已满，请稍后再试哦～"))
            elif status == 503:
                self._studio_available = False
                return Err(SdkError("RVC Studio 还在准备中呢，请稍等几秒再试哦～"))
            else:
                error_text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
                return Err(SdkError(f"提交对比任务失败 (HTTP {status}): {error_text[:200]}"))

        if not task_id:
            return Err(SdkError("提交对比任务失败：未获得 task_id"))

        # 标记活跃任务（二次检查 + 锁内原子化设置，与 sing_song 对齐防止 TOCTOU 竞态）
        async with self._task_lock:
            if self._active_task_id:
                self.logger.warning(
                    "竞态冲突(compare)：task_id=%s 被 task_id=%s 抢占了 active_task_id",
                    task_id, self._active_task_id,
                )
                # 取消刚提交到 B 端的孤儿任务
                try:
                    await self._http_client.post(
                        f"/api/task/{task_id}/cancel",
                        json_data={},
                        timeout=A_HTTP_TIMEOUT_DEFAULT,
                    )
                except Exception:
                    pass
                return Err(SdkError(
                    f"喵～现在还有任务在处理呢（任务 {self._active_task_id}），请等它完成再来对比哦～"
                ))
            self._active_task_id = task_id
        self._submitted_tasks[task_id] = time.time()
        self._cancel_event.clear()

        # 聊天窗口通知开始
        self.push_message(
            source="rvc_singer",
            visibility=["chat"],
            ai_behavior="blind",
            parts=[{"type": "text", "text": (
                f"喵～收到对比请求！正在用 {len(model_list)} 个音色"
                f"（{', '.join(model_list)}）试唱《{song_name}》片段... 🎧"
            )}],
            priority=3,
        )

        # 上报状态（清除上一次的播放器数据，带全连接字段）
        try:
            self._sticky_status = {}
            self.report_status(self._full_status(
                status="processing",
                active_task=task_id,
                song_name=song_name,
                progress=0,
                step="对比任务已提交",
            ))
        except Exception:
            pass

        # 后台等待（不阻塞入口）
        bg_task = asyncio.create_task(self._bg_wait_compare(task_id, song_name, model_list))
        self._bg_tasks.add(bg_task)
        bg_task.add_done_callback(lambda t: self._bg_tasks.discard(t))

        return Ok({
            "status": "processing",
            "task_id": task_id,
            "song_name": song_name,
            "models": model_list,
            "message": f"喵～《{song_name}》的 A/B 对比开始啦，完成后请在「演唱播放器」面板试听各模型效果哦～ 🎧",
        })

    # ═══════════════ UI 面板支持 ═══════════════

    @ui.context(id="dashboard", title=tr("panel.title", default="RVC 歌声合成"))
    async def get_dashboard_ui_context(self) -> dict[str, Any]:
        """为 Hosted UI 面板提供状态数据
        
        改进：使用 SQLite 歌曲库 API，获取统计和丰富元数据
        """
        songs_data = []
        models_data = []
        songs_total = 0
        songs_stats = {}
        
        # ── 缓存读取 ──
        if self._studio_available:
            # M43: 用 _MISSING 哨兵区分"未命中"与"data 是 None/空"
            _songs = await self._cache.get("songs")
            songs_data = [] if _songs is _MISSING else _songs

            # 缓存过期或不存在，从 SQLite 歌曲库 API 获取
            if not songs_data:
                status, resp_data = await self._http_client.get(
                    "/api/songs?limit=200&order=desc", timeout=A_HTTP_TIMEOUT_DEFAULT)
                if status == 200 and isinstance(resp_data, dict):
                    songs_data = resp_data.get("songs", [])
                    songs_total = resp_data.get("total", len(songs_data))
                    await self._cache.set("songs", songs_data)
                else:
                    self.logger.warning(f"获取歌曲列表失败: HTTP {status}")

            # 获取歌曲库统计
            try:
                stat_status, stat_data = await self._http_client.get(
                    "/api/songs/stats", timeout=3)
                if stat_status == 200 and isinstance(stat_data, dict):
                    songs_stats = stat_data
                    songs_total = stat_data.get("total", songs_total)
            except Exception:
                pass

            # 尝试从缓存读取模型列表
            _models = await self._cache.get("models")
            models_data = [] if _models is _MISSING else _models

            # 缓存过期或不存在，重新获取
            if not models_data:
                status, resp_data = await self._http_client.get("/api/status", timeout=A_HTTP_TIMEOUT_DEFAULT)
                if status == 200 and isinstance(resp_data, dict):
                    models_data = resp_data.get("models", [])
                    await self._cache.set("models", models_data)
                else:
                    self.logger.warning(f"获取模型列表失败: HTTP {status}")

        return {
            "studio_available": self._studio_available,
            "studio_url": self._studio_url,
            "active_task": self._active_task_id,
            "song_name": self._last_song_name,
            "progress": self._last_progress,
            "step": self._last_step,
            "songs": songs_data,
            "song_count": songs_total or len(songs_data),
            "song_stats": songs_stats,
            "models": models_data,
            "model_count": len(models_data),
            "default_model": self._default_model,
            "config": {
                "rvc_studio_host": self._studio_host,
                "rvc_studio_port": self._studio_port,
                "rvc_root_path": self._rvc_root,
                "default_model": self._default_model,
                "auto_mix_background": True,
                "use_https": self._use_https,
                "ssl_verify": self._ssl_verify,
                "api_key_set": bool(self._api_key),  # 只回显是否已设置，不泄露明文
                "auto_download_on_miss": self._auto_download_on_miss,
                "not_found_max_retries": self._not_found_max_retries,
            },
        }

    @ui.action(
        label=tr("actions.updateConfig.label", default="Save config"),
        icon="💾",
        tone="success",
        group="config",
        order=10,
        refresh_context=True,
    )
    @plugin_entry(
        id="update_config",
        name=tr("entries.updateConfig.name", default="更新配置"),
        description=tr("entries.updateConfig.description", default="更新 RVC 歌声合成插件配置。"),
        input_schema={
            "type": "object",
            "properties": {
                "rvc_studio_host": {"type": "string"},
                "rvc_studio_port": {"type": "integer"},
                "rvc_root_path": {"type": "string"},
                "default_model": {"type": "string"},
                "auto_mix_background": {"type": "boolean"},
                "api_key": {"type": "string", "description": "B 端 API Key（B 端未启用鉴权时留空）"},
                "use_https": {"type": "boolean", "description": "以 https 访问 B 端（B 端启用 --ssl-cert/--ssl-key 时开启）"},
                "ssl_verify": {"type": "boolean", "description": "校验 https 证书（自签证书场景可关闭）"},
            },
        },
    )
    async def update_config_entry(self, **kwargs):
        """通过 UI 面板更新配置
        
        改进：参数验证、缓存清理、错误处理
        """
        allowed = {"rvc_studio_host", "rvc_studio_port", "rvc_root_path", "default_model",
                   "auto_mix_background", "api_key", "use_https", "ssl_verify",
                   "auto_download_on_miss", "not_found_max_retries"}
        updates = {k: v for k, v in kwargs.items() if k in allowed and not k.startswith("_")}
        if not updates:
            return Err(SdkError("没有有效的配置字段需要更新"))

        # ── 参数验证 ──
        if "rvc_studio_host" in updates:
            host = str(updates["rvc_studio_host"]).strip()
            if not host:
                return Err(SdkError("主机地址不能为空"))
            updates["rvc_studio_host"] = host
        
        if "rvc_studio_port" in updates:
            try:
                port = int(updates["rvc_studio_port"])
                if not (1 <= port <= 65535):
                    return Err(SdkError("端口必须在 1-65535 之间"))
                updates["rvc_studio_port"] = port
            except (ValueError, TypeError):
                return Err(SdkError("端口必须是整数"))
        
        if "rvc_root_path" in updates:
            updates["rvc_root_path"] = str(updates["rvc_root_path"]).strip()
        
        if "default_model" in updates:
            model = str(updates["default_model"]).strip()
            if not model:
                return Err(SdkError("默认模型不能为空"))
            updates["default_model"] = model

        # ── P2: 远程连接加固字段 ──
        if "api_key" in updates:
            updates["api_key"] = str(updates["api_key"] or "").strip()
        if "use_https" in updates:
            updates["use_https"] = bool(updates["use_https"])
        if "ssl_verify" in updates:
            updates["ssl_verify"] = bool(updates["ssl_verify"])

        # 更新内存中的值
        if "rvc_studio_host" in updates:
            self._studio_host = str(updates["rvc_studio_host"])
        if "rvc_studio_port" in updates:
            self._studio_port = int(updates["rvc_studio_port"])
        if "rvc_root_path" in updates:
            self._rvc_root = str(updates["rvc_root_path"])
        if "default_model" in updates:
            self._default_model = str(updates["default_model"])
        if "api_key" in updates:
            self._api_key = updates["api_key"]
        if "use_https" in updates:
            self._use_https = updates["use_https"]
        if "ssl_verify" in updates:
            self._ssl_verify = updates["ssl_verify"]
        if "auto_download_on_miss" in updates:
            self._auto_download_on_miss = updates["auto_download_on_miss"]
        if "not_found_max_retries" in updates:
            self._not_found_max_retries = updates["not_found_max_retries"]

        self._studio_url = _ensure_url(self._studio_host, self._studio_port, self._use_https)
        
        await self._init_http_client()
        # M43: 选择性清理 — 配置变更后只清与连接/数据相关的缓存
        await self._cache.delete_prefix("http_")
        await self._cache.delete("songs")
        await self._cache.delete("models")
        self._sticky_status = {}

        # 持久化到 store
        if self.store and self.store.enabled:
            try:
                existing = await self.store.get("settings", {})
                existing_data = existing.value if hasattr(existing, "value") else existing
                if not isinstance(existing_data, dict):
                    existing_data = {}
                existing_data.update(updates)
                await self.store.set("settings", existing_data)
            except Exception as e:
                self.logger.warning(f"持久化配置到 store 失败: {e}")

        # 同步到 config（带超时保护）
        try:
            await asyncio.wait_for(self.config.set_many(updates), timeout=5.0)
        except (asyncio.TimeoutError, Exception) as e:
            self.logger.warning(f"同步 config 失败（不影响运行）: {e}")

        # 重新检查连接
        await self._check_studio_now()

        return Ok({"status": "ok", "studio_available": self._studio_available})

    # ═══════════════ 内部方法 ═══════════════

    async def _submit_song_task(
        self,
        song_name: str,
        model_name: str,
        pitch_shift: int,
        lyrics: str = "",  # A 端传入的歌词（可选）
        postprocess: str = "none",  # P2: 音效后处理预设
        # M6: 混音配置（可选；为空时使用 self._mix_* 已同步的值）
        mix_preset: str | None = None,
        mix_vocal_db: float | None = None,
        mix_inst_db: float | None = None,
        mix_reverb: float | None = None,
        mix_original: float | None = None,
    ):
        """提交歌曲处理任务到 RVC Studio

        改进：智能重试、详细错误分类、会话复用
        """
        payload = {
            "song_name": song_name,
            "model_name": model_name,
            "pitch_shift": pitch_shift,
            "auto_mix": self._auto_mix,  # 从 GUI 同步的用户设置
            "postprocess": postprocess,
            # M6: 混音配置（透传到 B 端 MixConfig.from_dict）
            "mix_preset": mix_preset if mix_preset is not None else self._mix_preset,
            "mix_vocal_db": float(mix_vocal_db) if mix_vocal_db is not None else float(self._mix_vocal_db),
            "mix_inst_db":  float(mix_inst_db)  if mix_inst_db  is not None else float(self._mix_inst_db),
            "mix_reverb":   float(mix_reverb)   if mix_reverb   is not None else float(self._mix_reverb),
            "mix_original": float(mix_original) if mix_original is not None else float(self._mix_original),
        }
        # 如果 A 端传了歌词，一并提交给 B
        if lyrics and isinstance(lyrics, str) and lyrics.strip():
            payload["lyrics"] = lyrics.strip()
        
        for attempt in range(_MAX_RETRY_ATTEMPTS):
            status, data = await self._http_client.post(
                "/api/sing", json_data=payload, timeout=10,
            )
            if status == 200:
                self._consecutive_failures = 0
                return Ok(data) if isinstance(data, dict) else Ok({"result": data})
            elif status == -1:
                # 网络/超时错误
                err_info = data.get("error", str(data)) if isinstance(data, dict) else str(data)
                if attempt < _MAX_RETRY_ATTEMPTS - 1:
                    self.logger.warning(f"提交任务失败，重试... (尝试 {attempt + 1}/{_MAX_RETRY_ATTEMPTS}): {err_info}")
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                else:
                    return Err(SdkError(
                        f"提交任务超时（重试{_MAX_RETRY_ATTEMPTS}次仍失败）: {err_info}。"
                        "下一步：调用 check_studio_status 确认服务状态后重试 sing_song"))
            elif status == 429:
                error_data = data if isinstance(data, dict) else {}
                active_task = error_data.get("active_task", "unknown")
                return Err(SdkError(
                    f"RVC Studio 正在处理另一首歌（任务 {active_task}），请等它完成后再试哦～"
                ))
            elif status == 503:
                error_data = data if isinstance(data, dict) else {}
                err_msg = error_data.get("error", "RVC 引擎未就绪")
                self._studio_available = False
                self.logger.warning(f"RVC 引擎未就绪 (503): {err_msg}")
                return Err(SdkError(
                    f"RVC Studio 还在准备中呢（{err_msg}）。\n"
                    "请稍等几秒钟，等它显示'就绪'后再试哦～"
                ))
            elif status == 409:
                error_text = data if isinstance(data, str) else str(data)
                return Err(SdkError(f"任务冲突（可能歌曲正在处理）: {error_text}"))
            elif status == 422:
                error_text = data if isinstance(data, str) else str(data)
                return Err(SdkError(f"参数错误: {error_text}"))
            elif status == 401:
                # P2: B 端启用了 API Key 鉴权
                return Err(SdkError(
                    "B 端拒绝访问 (401)：api_key 未配置或不正确。"
                    "下一步：调用 update_config 设置 api_key（与 RVC Studio 启动参数 --api-key 一致）"
                ))
            elif status >= 500:
                if attempt < _MAX_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                else:
                    error_text = data if isinstance(data, str) else str(data)
                    return Err(SdkError(f"服务器错误（重试{_MAX_RETRY_ATTEMPTS}次）: {error_text}"))
            else:
                error_text = data if isinstance(data, str) else str(data)
                return Err(SdkError(f"提交任务失败 (HTTP {status}): {error_text}"))

        # 兜底：_MAX_RETRY_ATTEMPTS 为 0 时循环体不执行，避免隐式返回 None
        return Err(SdkError("提交任务失败：未执行任何重试（_MAX_RETRY_ATTEMPTS 配置为 0）"))

    async def _push_busy_signal(self):
        """向 LLM 上下文注入「禁止回复」指令，用户不可见。

        使用 coalesce_key 实现状态合并：下次写入同名 key 时自动替换。
        任务完成时调用 _clear_busy_signal() 替换为允许回复。
        """
        try:
            # push_message 是同步方法，不可 await（NEKO SDK 协议）
            self.push_message(
                source="rvc_singer",
                parts=[{"type": "text", "text": (
                    "[系统控制指令] rvc_singer 插件正在进行歌曲处理任务（可能需要几分钟）。"
                    "请暂时停止所有对话回复，不要调用任何工具或生成新消息。"
                    "等收到 '任务完成通知' 后才能恢复对话。"
                )}],
                visibility=[],
                ai_behavior="read",
                coalesce_key="rvc_singer_busy_signal",
                priority=10,
            )
        except Exception:
            pass

    async def _clear_busy_signal(self):
        """向 LLM 上下文注入「恢复对话」指令，替换 busy 信号。

        使用与 _push_busy_signal 相同的 coalesce_key，
        确保旧 busy 消息被自动替换，不留残余。
        """
        try:
            # push_message 是同步方法，不可 await（NEKO SDK 协议）
            self.push_message(
                source="rvc_singer",
                parts=[{"type": "text", "text": (
                    "[任务完成通知] rvc_singer 任务已结束，已恢复对话。现在可以正常响应用户了。"
                )}],
                visibility=[],
                ai_behavior="read",
                coalesce_key="rvc_singer_busy_signal",
                priority=10,
            )
        except Exception:
            pass

    async def _bg_wait_and_push(self, task_id: str, song_name: str):
        """后台等待任务完成并推送结果（通过 asyncio.create_task 调用，不阻塞入口）

        失败时自动联网下载 + 重提交逻辑已下沉到 _wait_for_completion_with_progress 内部，
        本函数只负责：等结果 → 推成功 / 失败消息。
        """
        try:
            # 通知 LLM 静默
            await self._push_busy_signal()

            result = await self._wait_for_completion_with_progress(task_id, song_name)

            if isinstance(result, Ok):
                await self._push_song_result(result.value, song_name)
                return

            # 失败（已含自动下载重试，但仍可能重试耗尽或他类错误）
            err_msg = (
                getattr(result.error, 'message', None) or str(result.error)
                if result.error else "未知错误"
            )
            self.logger.info(f"演唱失败，推送错误消息: {err_msg}")
            await self._send_log_to_studio("error", f"演唱失败: {err_msg}")
            try:
                self.push_message(
                    source="rvc_singer",
                    visibility=["chat"],
                    ai_behavior="blind",
                    parts=[{"type": "text", "text": f"喵呜... 演唱失败啦 😿 原因: {err_msg}"}],
                    priority=5,
                )
            except Exception as push_err:
                self.logger.error(f"推送错误消息失败: {push_err}")

        except asyncio.CancelledError:
            self._cancel_event.clear()
            self.logger.info(f"后台任务被取消: {task_id}")
        except Exception as e:
            err_str = str(e) if str(e) else repr(e)
            self.logger.error(f"后台任务异常: {err_str}")
            try:
                self.push_message(
                    source="rvc_singer",
                    visibility=["chat"],
                    ai_behavior="blind",
                    parts=[{"type": "text", "text": f"喵... 演唱《{song_name}》的时候出了点问题 😿: {err_str}"}],
                    priority=5,
                )
            except Exception as push_err:
                self.logger.error(f"推送错误消息失败: {push_err}")
        finally:
            # 恢复 LLM 对话
            try:
                await self._clear_busy_signal()
            except Exception:
                pass

            # 清理活跃任务标记（自动下载重试可能已更换 _active_task_id，用当前值清理）
            async with self._task_lock:
                if self._active_task_id:
                    self._submitted_tasks.pop(self._active_task_id, None)
                    self._active_task_id = None
            # 兜底清理原始 task_id（未被 replace 时两者相同，Pop None 无副作用）
            self._submitted_tasks.pop(task_id, None)
            # 上报最终状态（延迟 200ms 确保 NEKO 队列先处理 _push_song_result 的状态）
            try:
                await asyncio.sleep(0.2)
                self.report_status(self._full_status())
            except Exception:
                self.logger.debug("_bg_wait_and_push finally report_status 失败（健康检查将兜底）")

    async def _wait_for_completion_with_progress(self, task_id: str, song_name: str,
                                                  poll_interval: float = 5.0,
                                                  not_found_retries: int = 0):
        """轮询等待任务完成，仅通过 report_status 向面板上报进度（不推聊天）

        not_found_retries:
            内部计数器——本函数递归调用时 +1，表示"已经自动重试 N 次"。
            外部调用方不需要传（默认 0）。
            达到 self._not_found_max_retries 上限后，"未找到歌曲"将不再触发自动下载。
        """
        max_wait = 600   # 最多等10分钟
        stale_timeout = STALE_TASK_TIMEOUT  # 僵死检测：进度5分钟不变视为卡死
        elapsed = 0
        last_progress = -1
        last_step = ""
        # 僵死检测：记录最后一次进度变化的时间
        last_progress_change_elapsed = 0

        # 更新持久化进度跟踪
        self._last_song_name = song_name
        self._last_progress = 0
        self._last_step = ""

        while elapsed < max_wait:
            # 检查取消信号
            if self._cancel_event.is_set():
                self.logger.info(f"收到取消信号，停止轮询: {task_id}")
                return Err(SdkError("任务被取消"))

            # ── 动态轮询间隔：根据当前 step 调整 ──
            # UVR5 人声分离阶段耗时较长，降低轮询频率减少无效请求
            # 其他阶段保持较高频率以获得实时进度
            dynamic_interval = poll_interval  # 默认 5 秒
            current_step_for_interval = getattr(self, '_last_step', '')
            if '人声分离' in current_step_for_interval or '分离' in current_step_for_interval:
                dynamic_interval = 8.0   # UVR5 阶段 8 秒
            elif '准备音频' in current_step_for_interval or '生成口型' in current_step_for_interval:
                dynamic_interval = 3.0   # 快速阶段 3 秒

            # 用 aiohttp 轮询任务状态
            status, data = await self._http_client.get(
                f"/api/task/{task_id}", timeout=A_HTTP_TIMEOUT_DEFAULT,
            )
            if status == 200 and isinstance(data, dict):
                task_status = data.get("status")

                if task_status == "completed":
                    self._last_progress = 100
                    self._last_step = "完成"
                    return Ok(data)
                elif task_status == "failed":
                    err_detail = data.get('error') or data.get('message') or '未知错误'
                    self.logger.info(f"任务失败(progress): task_id={task_id}, error={err_detail}, raw_data={data}")

                    # ════════════════════════════════════════════════════════════════
                    # 「未找到歌曲」→ 自动联网下载 → 重提交（统一行为：所有调用方受益）
                    # ════════════════════════════════════════════════════════════════
                    # 设计：本地搜歌无果时，无缝联网下载（无需用户感知）。
                    # 触发条件：B 端错误消息含"未找到歌曲"前缀（见 process_song _find_song_file 失败分支）。
                    # 一处实现覆盖 4 类调用方（chat / 队列 / 对比 / 通用等待）。
                    if "未找到歌曲" in err_detail:
                        # ── 开关检查：用户可在 plugin.toml 关闭自动下载 ──
                        if not self._auto_download_on_miss:
                            self.logger.info(f"自动下载已关闭（auto_download_on_miss=false），跳过: {song_name}")
                        # ── 计数检查：超过最大重试次数 → 不再重试 ──
                        elif not_found_retries >= self._not_found_max_retries:
                            self.logger.warning(
                                f"自动下载已达上限 ({self._not_found_max_retries} 次): {song_name}"
                            )
                        else:
                            # 上报"联网下载中"状态，让面板/歌词窗可见
                            try:
                                self.report_status(self._full_status(
                                    status="processing",
                                    active_task=task_id,
                                    song_name=song_name,
                                    progress=2,
                                    step=f"本地未找到，联网下载中（{not_found_retries + 1}/{self._not_found_max_retries}）...",
                                ))
                            except Exception:
                                pass

                            # 取消原失败任务（B 端会清理临时文件）
                            try:
                                await self._http_client.post(
                                    f"/api/task/{task_id}/cancel", timeout=A_HTTP_TIMEOUT_DEFAULT,
                                )
                            except Exception:
                                pass

                            # 联网下载（chat 端有"喵..."提示；GUI 路径通过 report_status 看到状态）
                            download_result = await self.search_and_download_song(song_name)

                            if isinstance(download_result, Ok):
                                # 下载成功 — 使用 B 端返回的实际歌曲名（可能与搜索词不同）
                                actual_song_name = download_result.value.get("song_name") or song_name
                                self._last_song_name = actual_song_name
                                resubmit = await self._submit_song_task(
                                    actual_song_name,
                                    self._last_model or self._default_model,
                                    self._last_pitch_shift,
                                    postprocess=self._last_postprocess,
                                )
                                if isinstance(resubmit, Ok):
                                    new_task_id = resubmit.value.get("task_id")
                                    if new_task_id:
                                        # 更新活跃任务上下文（确保 finally 锁释放路径正确）
                                        async with self._task_lock:
                                            self._active_task_id = new_task_id
                                        self._submitted_tasks[new_task_id] = time.time()
                                        self._cancel_event.clear()
                                        self.logger.info(
                                            f"自动重试: 新任务 task_id={new_task_id}, "
                                            f"song={song_name} (第 {not_found_retries + 1} 次)"
                                        )
                                        # 切到新 task_id 继续轮询（计数 +1 上限自动生效）
                                        return await self._wait_for_completion_with_progress(
                                            new_task_id, actual_song_name, poll_interval,
                                            not_found_retries=not_found_retries + 1,
                                        )

                    # 非"未找到歌曲"或下载/重提交失败 → 维持原失败语义
                    self._last_progress = 0
                    self._last_step = "失败"
                    return Err(SdkError(
                        f"歌曲处理失败: {err_detail}"
                    ))
                elif task_status == "processing":
                    progress = data.get("progress", 0)
                    step = data.get("step", "")
                    self._last_progress = progress
                    self._last_step = step

                    # 仅上报到面板（report_status），不推聊天（避免刷屏 + 触发 AI 插话）
                    try:
                        self.report_status(self._full_status(
                            status="processing",
                            active_task=task_id,
                            song_name=song_name,
                            progress=progress,
                            step=step,
                        ))
                    except Exception:
                        pass

                    # 进度日志（仅控制台，不推聊天）
                    if progress != last_progress or step != last_step:
                        last_progress = progress
                        last_step = step
                        # 进度变化 → 重置僵死计时器
                        last_progress_change_elapsed = elapsed
                        self.logger.info(
                            f"处理中... {progress}% | {step}"
                        )
                    elif elapsed - last_progress_change_elapsed > stale_timeout:
                        self.logger.warning(
                            f"僵死检测：任务进度 {stale_timeout}s 未变化 (task_id={task_id})，自动取消"
                        )
                        self._last_progress = 0
                        self._last_step = "僵死超时"
                        return Err(SdkError(
                            f"任务可能已经卡死（{stale_timeout // 60} 分钟进度未变化），"
                            "已自动释放锁，可以直接重新提交"
                        ))
                else:
                    # pending / queued / 其他中间状态 — 也上报让面板知道进展
                    queue_pos = data.get("queue_position", 0) if isinstance(data, dict) else 0
                    display_step = data.get("step", task_status) if isinstance(data, dict) else task_status
                    if display_step == "pending":
<<<<<<< HEAD
                        display_step = "排队中..." if queue_pos <= 0 else f"排队中...（第{queue_pos}位）"
=======
                        display_step = "排队中..."
>>>>>>> e585ebed650b45f41d264dd42ce638a8107c261e
                    try:
                        self.report_status(self._full_status(
                            status=task_status,
                            active_task=task_id,
                            song_name=song_name,
                            progress=0,
                            step=str(display_step),
                        ))
                    except Exception:
                        pass
            else:
                self.logger.warning(f"轮询异常 (将自动重试): status={status}")

            #   P2: 分段 sleep，每 1 秒检查一次取消信号，缩短取消响应延迟
            _sleep_remaining = dynamic_interval
            while _sleep_remaining > 0:
                if self._cancel_event.is_set():
                    self.logger.info(f"分段 sleep 中检测到取消信号: {task_id}")
                    return Err(SdkError("任务被取消"))
                _chunk = min(1.0, _sleep_remaining)
                await asyncio.sleep(_chunk)
                _sleep_remaining -= _chunk
            elapsed += dynamic_interval

        self._last_progress = 0
        self._last_step = "超时"
        return Err(SdkError("任务超时（10分钟），请重试"))

    async def _push_song_result(self, result_data: dict, song_name: str):
        """将歌曲结果推送到 NEKO 对话

        参照 music_pusher 官方插件模式：
        1. music_allowlist_add → 域名白名单
        2. music_play_url → 触发 NEKO 内置音乐播放器
        3. report_status → 歌词 + viseme 数据给前端面板（口型同步）
        """
        output_mp3_path = result_data.get("output_mp3_path", "")
        output_audio_path = result_data.get("output_audio_path", "")
        lyrics = result_data.get("lyrics", "") or ""
        duration = result_data.get("duration_seconds", 0) or 0
        lyric_lines = result_data.get("lyric_lines", []) or []
        viseme_data = result_data.get("viseme_data", []) or []
        mouth_open_y_data = result_data.get("mouth_open_y_data", []) or []
        lyrics_source = result_data.get("lyrics_source", "") or ""
        # asr_quality: "ok" (ASR/LRC/provided) / "degraded" (placeholder/fallback)
        asr_quality = "ok" if lyrics_source in ("provided", "lrc", "asr") else "degraded"

        # 选择实际存在的音频文件
        audio_file_path = ""

        if output_mp3_path and os.path.exists(output_mp3_path):
            audio_file_path = output_mp3_path
            self.logger.info("使用 MP3 文件: %s (%.1f MB)",
                             output_mp3_path,
                             os.path.getsize(output_mp3_path) / (1024 * 1024))
        elif output_audio_path and os.path.exists(output_audio_path):
            audio_file_path = output_audio_path
            self.logger.info("使用 WAV 文件（MP3 不存在）: %s (%.1f MB)",
                             output_audio_path,
                             os.path.getsize(output_audio_path) / (1024 * 1024))
        else:
            self.logger.warning(
                "音频文件不存在! mp3=%s, wav=%s",
                output_mp3_path,
                output_audio_path,
            )

        if audio_file_path:
            # 构造 HTTP URL（通过 B 端 /output/ 静态文件路由提供，避免 file:/// 被浏览器安全策略拦截）
            filename = os.path.basename(audio_file_path)
            merged_url = f"{self._studio_url}/output/{quote(filename, safe='')}"

            self.logger.info(
                "report_status 播放器数据: url=%s, lyrics=%d lines, visemes=%d items",
                merged_url, len(lyric_lines), len(viseme_data),
            )

            # ── 核心：通过 report_status 传数据给 Static UI 面板 ──
            # 播放器数据写入 _sticky_status（跨上报保留），
            # 并通过 _full_status 带全 studio_available/studio_url，
            # 修复完成后面板误显示"离线"的问题
            # P3: _sticky_status 在 try 外赋值，确保 report_status 失败时数据不丢失
            self._sticky_status = {
                "message": f"喵～《{song_name}》唱好啦！ 🎉",
                "model": self._last_model,
                "merged_audio_url": merged_url,
                "lyric_lines": lyric_lines,
                "viseme_data": viseme_data,
                "mouth_open_y_data": mouth_open_y_data,
                "duration_seconds": duration,
                "lyrics_preview": lyrics[:500] if lyrics else "",
                "lyrics_source": lyrics_source,
                "asr_quality": asr_quality,
                "mix_note": result_data.get("mix_note", ""),
                "compare_mode": False,
                "compare_results": [],
            }

            # P3: report_status 重试一次，防止偶发网络抖动导致面板无播放器
            status_pushed = False
            for attempt in (1, 2):
                try:
                    self.report_status(self._full_status(
                        status="completed",
                        progress=100,
                        step="完成",
                        song_name=song_name,
                    ))
                    status_pushed = True
                    self.logger.info("report_status 推送成功 (attempt=%d): url=%s", attempt, merged_url)
                    break
                except Exception as rs_err:
                    self.logger.error("report_status 失败 (attempt=%d): %s", attempt, rs_err)
                    if attempt == 1:
                        await asyncio.sleep(A_POLL_YIELD_INTERVAL)
            if not status_pushed:
                self.logger.error("report_status 重试后仍失败，面板可能无播放器")

            # ── P4: 推送歌词到悬浮歌词窗口（含逐字时间戳） ──
            if lyric_lines:
                try:
                    # 生成逐字时间戳（均分模式）
                    char_times = self._build_char_times(lyric_lines)
                    push_lyrics_data({
                        "action": "play",
                        "song_name": song_name,
                        "lyrics": lyric_lines,
                        "char_times": char_times,
                        "duration": duration,
                    })
                    self.logger.info("浮窗歌词推送成功: %d 行", len(lyric_lines))
                except Exception as lyw_err:
                    self.logger.debug("浮窗歌词推送失败（非致命）: %s", lyw_err)

            # ── P4: push_message 歌词到聊天区 ──
            if lyric_lines and len(lyric_lines) <= 30:
                # 歌词不长时，推送到聊天区方便查阅
                lyric_text = "\n".join(
                    f"`{self._fmt_lyric_line(line)}`" for line in lyric_lines
                )
                try:
                    self.push_message(
                        source="rvc_singer",
                        visibility=["chat"],
                        ai_behavior="blind",
                        parts=[{
                            "type": "text",
                            "text": f"📝 《{song_name}》歌词:\n{lyric_text}"
                        }],
                        priority=4,
                    )
                except Exception:
                    pass

            # ── 聊天窗口：白名单 + 文本通知（音频由面板 <audio> 播放，保障嘴型同步）──
            # Step 1: 域名加入播放白名单（方便面板 <audio> 加载音频）
            parsed = urlparse(self._studio_url)
            domain = parsed.netloc  # e.g. "127.0.0.1:9876"

            try:
                self.push_message(
                    source="rvc_singer",
                    message_type="music_allowlist_add",
                    ai_behavior="blind",
                    metadata={"domains": [domain]},
                    priority=7,
                )
            except Exception as allowlist_err:
                self.logger.error("music_allowlist_add 失败: %s", allowlist_err)

            # Step 2: 文本通知（面板 <audio> 驱动嘴型同步，music_play_url 保底出声）
            info_text = f"喵～**《{song_name}》演唱完毕！**请主人欣赏～ 🎤✨"
            if duration:
                info_text += f"\n时长: {int(duration // 60)}分{int(duration % 60)}秒"

            try:
                self.push_message(
                    source="rvc_singer",
                    visibility=["chat"],
                    ai_behavior="respond",
                    parts=[{"type": "text", "text": info_text}],
                    priority=9,
                )
            except Exception as text_err:
                self.logger.error("文本推送失败: %s", text_err)

            # Step 3: 触发 NEKO 内置音乐播放器（有 autoplay 被拦恢复机制，保底出声）
            try:
                self.push_message(
                    source="rvc_singer",
                    visibility=["chat"],
                    message_type="music_play_url",
                    ai_behavior="respond",
                    description=f"RVC翻唱《{song_name}》",
                    metadata={
                        "url": merged_url,
                        "name": song_name,
                        "artist": "RVC翻唱",
                    },
                    priority=10,
                )
            except Exception as play_err:
                self.logger.error("music_play_url 失败: %s", play_err)
        else:
            # 无音频文件时的兜底
            warn_text = f"喵呜... 《{song_name}》处理完了但找不到输出文件喵 😿\nmp3={output_mp3_path}\nwav={output_audio_path}"
            self.push_message(
                source="rvc_singer",
                visibility=["chat"],
                ai_behavior="blind",
                parts=[{"type": "text", "text": warn_text}],
                priority=5,
            )

        # ── 队列续推：当前歌完成 → 检查是否有排队歌曲 → 自动处理 ──
        # （无论是否有音频文件，都需要释放任务槽）
        await self._maybe_process_next_queued()

    # ═══════════════ 队列引擎辅助方法 ═══════════════

    async def _queue_submit_song(self, song_name: str, model: str, pitch_shift: int):
        """队列引擎的回调：提交歌曲任务到 B 端
        _submit_song_task 返回 Ok({"task_id":"..."}) 或 Err(SdkError)，队列引擎可直接消费
        """
        return await self._submit_song_task(
            song_name=song_name,
            model_name=model,
            pitch_shift=pitch_shift,
        )

    async def _queue_wait_task(self, task_id: str, song_name: str):
        """队列引擎的回调：等待任务完成"""
        return await self._wait_for_completion_with_progress(task_id, song_name)

    def _on_queue_snapshot(self, snapshot: QueueSnapshot):
        """队列变化回调：刷新队列浮窗（V2 引擎返回 dict 格式）"""
        try:
            push_queue_data({
                "action": "update",
                "snapshot": {
                    "items": [
                        {
                            "song_name": i["song_name"] if isinstance(i, dict) else i.song_name,
                            "status": i.get("status", "waiting") if isinstance(i, dict) else i.status.value,
                            "priority": i.get("priority", "NORMAL") if isinstance(i, dict) else i.priority.name,
                            "retry_count": i.get("retry_count", 0) if isinstance(i, dict) else i.retry_count,
                        }
                        for i in snapshot.items
                    ],
                    "now_playing": snapshot.now_playing,
                    "queue_size": snapshot.queue_size,
                    "progress": snapshot.progress,
                    "status": snapshot.status,
                },
            })
        except Exception:
            pass

    def _on_queue_progress(self, song_name: str, progress: float):
        """队列处理进度回调：刷新歌词窗进度条"""
        try:
            # 推进度给歌词窗口
            push_lyrics_data({
                "action": "progress",
                "elapsed": 0,
                "song_name": song_name,
            })
            # 同时刷新队列窗口的进度
            snapshot = self._queue_engine.get_snapshot()
            self._on_queue_snapshot(snapshot)
        except Exception:
            pass

    async def _maybe_process_next_queued(self):
        """当前歌曲处理完毕，检查队列并启动下一首"""
        snapshot = self._queue_engine.get_snapshot()
        if not snapshot.items:
            # 无排队歌曲
            # 推浮窗停止信号
            try:
                push_lyrics_data({"action": "stop"})
            except Exception:
                pass
            return
        # 有排队歌曲，队列引擎已在 process_next 中自动调度
        # 此处仅通知状态
        self.logger.info(
            "队列续推: %d 首等待中，即将自动开始下一首",
            snapshot.queue_size,
        )

    @staticmethod
    def _fmt_lyric_line(line: dict) -> str:
        """格式化歌词行用于聊天推送"""
        t = line.get("time", 0)
        text = line.get("text", "")
        m, s = divmod(int(t), 60)
        return f"[{m:02d}:{s:02d}] {text}"

    @staticmethod
    def _build_char_times(lyric_lines: list[dict]) -> list[dict]:
        """从歌词行数据生成逐字时间戳（均分模式）

        每行的 duration / 字符数 = 每个字的显示时长
        返回: [{chars: [offset1, offset2, ...]}, ...]
        """
        char_times = []
        for line in lyric_lines:
            text = line.get("text", "")
            duration = line.get("duration", 3.0)
            n_chars = max(len(text), 1)
            per_char = duration / n_chars
            chars = [i * per_char for i in range(n_chars)]
            char_times.append({"chars": chars})
        return char_times

    # ═══════════════ A/B 对比（NEKO 播放链路） ═══════════════

    async def _bg_wait_compare(self, task_id: str, song_name: str, models: list[str]):
        """后台等待对比任务完成，结果经 report_status 推送到 player.html 面板试听"""
        try:
            # 通知 LLM 静默
            await self._push_busy_signal()

            result = await self._wait_for_completion_with_progress(task_id, song_name)
            if isinstance(result, Err):
                err_msg = getattr(result.error, 'message', None) or str(result.error) if result.error else "未知错误"
                self.logger.info(f"对比失败，推送错误消息: {err_msg}")
                await self._send_log_to_studio("error", f"A/B 对比失败: {err_msg}")
                try:
                    self.push_message(
                        source="rvc_singer",
                        visibility=["chat"],
                        ai_behavior="blind",
                        parts=[{"type": "text", "text": (
                            f"喵呜... A/B 对比失败啦 😿 原因: {err_msg}\n"
                            "下一步：可以先 check_studio_status 看看状态，或换首歌再试哦～"
                        )}],
                        priority=5,
                    )
                except Exception as push_err:
                    self.logger.error(f"推送错误消息失败: {push_err}")
            else:
                await self._push_compare_result(result.value, song_name, models)
        except asyncio.CancelledError:
            self._cancel_event.clear()
            self.logger.info(f"对比后台任务被取消: {task_id}")
        except Exception as e:
            err_str = str(e) if str(e) else repr(e)
            self.logger.error(f"对比后台任务异常: {err_str}")
            try:
                self.push_message(
                    source="rvc_singer",
                    visibility=["chat"],
                    ai_behavior="blind",
                    parts=[{"type": "text", "text": f"喵... 对比《{song_name}》的时候出了点问题 😿: {err_str}"}],
                    priority=5,
                )
            except Exception as push_err:
                self.logger.error(f"推送错误消息失败: {push_err}")
        finally:
            # 恢复 LLM 对话
            try:
                await self._clear_busy_signal()
            except Exception:
                pass

            # 清理活跃任务标记（与 _bg_wait_and_push 一致的锁释放逻辑）
            async with self._task_lock:
                if self._active_task_id:
                    self._submitted_tasks.pop(self._active_task_id, None)
                    self._active_task_id = None
            self._submitted_tasks.pop(task_id, None)
            # 注意：这里不再上报"ready 空状态"覆盖，避免把刚推送的 compare_results 冲掉
            # （健康检查循环会在 30s 内自然刷新面板的连接状态字段）

    async def _push_compare_result(self, result_data: dict, song_name: str, models: list[str]):
        """将 A/B 对比结果推送到 NEKO 播放器面板（report_status）+ 聊天文本通知

        NEKO 播放链路（与整首歌 merged_audio_url 同一机制）：
        B 端出 MP3 → 此处拼完整 HTTP URL → report_status 下发 →
        player.html 面板渲染试听列表 → 面板内 <audio> 播放。
        """
        raw_results = result_data.get("compare_results", []) or []
        results = []
        ok_count = 0
        for r in raw_results:
            if not isinstance(r, dict):
                continue
            rel_url = r.get("url") or ""
            full_url = ""
            if rel_url:
                # rel_url 形如 /output/compare_xxx.mp3 → 拼 B 端完整地址（文件名 quote）
                filename = rel_url.rsplit("/", 1)[-1]
                full_url = f"{self._studio_url}/output/{quote(filename, safe='')}"
                ok_count += 1
            results.append({
                "model": r.get("model", "?"),
                "url": full_url,
                "audio_url": full_url,  # 面板消费端兼容字段
                "ok": bool(rel_url),    # 面板消费端兼容字段
                "error": (r.get("error") or "")[:200],
            })

        self.logger.info(
            "report_status 对比数据: song=%s, ok=%d/%d", song_name, ok_count, len(results),
        )
        await self._send_log_to_studio("info", f"A/B 对比完成: {ok_count}/{len(results)} 个模型成功")

        if ok_count > 0:
            # ── 核心：report_status 传对比结果给播放器面板（NEKO 播放）──
            # 对比结果写入 _sticky_status（跨上报保留），带全连接字段
            # P3: _sticky_status 在 try 外赋值，确保 report_status 失败时数据不丢失
            self._sticky_status = {
                "message": f"喵～《{song_name}》A/B 对比完成！点各模型「试听」按钮比较喵～",
                "compare_mode": True,
                "compare_results": results,
                "lyric_lines": [],  # 对比片段无歌词，清空歌词区
                "viseme_data": [],
                "mouth_open_y_data": [],
                "merged_audio_url": "",
            }

            # P3: report_status 重试一次
            for attempt in (1, 2):
                try:
                    self.report_status(self._full_status(
                        status="completed",
                        progress=100,
                        step="对比完成",
                        song_name=song_name,
                        active_task=None,
                    ))
                    self.logger.info("report_status 推送成功 (attempt=%d, compare)", attempt)
                    break
                except Exception as rs_err:
                    self.logger.error("report_status 失败 (attempt=%d): %s", attempt, rs_err)
                    if attempt == 1:
                        await asyncio.sleep(A_POLL_YIELD_INTERVAL)

            # ── 聊天窗口文本通知（不推音频，播放在面板内）──
            lines = [f"喵～**《{song_name}》A/B 对比完成！** 🎧"]
            for r in results:
                if r["url"]:
                    lines.append(f"✅ {r['model']}")
                else:
                    lines.append(f"❌ {r['model']}（失败: {r['error'][:60]}）")
            lines.append("请在「**演唱播放器**」面板点击各模型的「▶ 试听」按钮对比效果，"
                         "选中喜欢的音色后再让我唱整首哦喵～")
            try:
                self.push_message(
                    source="rvc_singer",
                    visibility=["chat"],
                    ai_behavior="respond",
                    parts=[{"type": "text", "text": "\n".join(lines)}],
                    priority=5,
                )
            except Exception as push_err:
                self.logger.error("对比结果推送失败: %s", push_err)
        else:
            # 全部失败兜底
            err_summary = "; ".join(
                f"{r['model']}: {r['error'][:60]}" for r in results if r["error"]
            ) or "未知原因"
            try:
                self.push_message(
                    source="rvc_singer",
                    visibility=["chat"],
                    ai_behavior="blind",
                    parts=[{"type": "text", "text": (
                        f"喵呜... 《{song_name}》的对比全都失败了 😿\n{err_summary}"
                    )}],
                    priority=5,
                )
            except Exception as push_err:
                self.logger.error("对比失败消息推送失败: %s", push_err)

    # ═══════════════ 日志桥接（A → B） ═══════════════

    async def _send_log_to_studio(self, level: str, message: str):
        """发送日志条目到 B 端 RVC Studio（异步 fire-and-forget）
        
        使用 aiohttp 异步发送，不会阻塞事件循环。
        即使 B 端不在线也不会抛异常。
        """
        if not self._studio_url or not self._http_client:
            return
        
        payload = {
            "level": level,
            "message": message,
            "source": "rvc_singer_a",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
        # 异步发送，不等待结果（短 timeout 防止连接池耗尽）
        try:
            await self._http_client.post("/api/neko/log", payload, timeout=3)
        except Exception:
            pass  # 静默失败

