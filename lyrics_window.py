"""
Floating Lyrics Window — 独立桌面悬浮歌词窗口 V2
磨砂背板 + 可自由拖动 + 卡拉OK式逐字高亮 + 平滑过渡

布局:
┌──────────────────────────────────┐
│  🎵 正在播放: 歌名               │  ← 顶部标题栏（可拖拽区域）
│                                  │
│    上一句歌词（变淡透明）         │
│  ▶ ████████░░░░░░░░ 当前句      │  ← 逐字卡拉OK高亮 + 发光效果
│    下一句歌词（变得更淡）         │
│                                  │
│  ─────────○──────────  02:45    │  ← 底部进度条 + 时间
└──────────────────────────────────┘

数据格式:
  push_lyrics_data({
    "action": "play",
    "song_name": "歌名",
    "duration": 180.0,
    "lyrics": [
      {"time": 0.0, "text": "第一句歌词", "duration": 3.5},
      {"time": 3.8, "text": "第二句歌词", "duration": 4.2},
      ...
    ],
    # 可选: 逐字时间戳（用于卡拉OK逐字高亮）
    "char_times": [
      {"time": 0.0, "chars": [0, 0.1, 0.2, 0.3, ...]},  # 每个字的开始时间（相对time的偏移）
      ...
    ]
  })
"""
from __future__ import annotations

import time
from queue import Empty, Queue
from typing import Optional

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QObject,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QVBoxLayout,
    QWidget,
)


# ════════════════════════════════════════════════
# 设计令牌
# ════════════════════════════════════════════════
class LyricsTokens:
    BG_GLASS = QColor(30, 30, 40, 200)
    TEXT_MAIN = QColor(220, 220, 240)
    TEXT_ACTIVE = QColor(120, 220, 255)   # 荧光青蓝
    TEXT_GLOW = QColor(80, 200, 255, 80)  # 发光晕
    TEXT_DIM = QColor(120, 120, 140)
    ACCENT = QColor(120, 220, 255)
    PROGRESS_BG = QColor(60, 60, 80)
    PROGRESS_FG = QColor(120, 220, 255)


# ════════════════════════════════════════════════
# 自定义逐字高亮标签（卡拉OK核心）
# ════════════════════════════════════════════════
class KaraokeLabel(QWidget):
    """支持逐字渐进高亮的歌词标签
    
    - text: 完整文本
    - progress: 0.0~1.0，高亮进度
    - char_progress: list[float]，每个字的独立进度（更精细）
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._progress = 0.0  # 整体进度
        self._char_progress = []  # 逐字进度 0~1
        self._active_color = LyricsTokens.TEXT_ACTIVE
        self._inactive_color = QColor(160, 170, 200, 80)
        self._font_size = 18
        self._bold = True
        self.setMinimumHeight(36)
        self.setStyleSheet("background: transparent;")

    def set_params(self, text: str, progress: float = 0.0,
                   char_progress: list = None,
                   font_size: int = 18, bold: bool = True):
        self._text = text
        self._progress = max(0.0, min(1.0, progress))
        self._char_progress = char_progress or []
        self._font_size = font_size
        self._bold = bold
        self.update()

    def paintEvent(self, event):
        if not self._text:
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)

        w = self.width()
        h = self.height()
        fm = QFontMetrics(self.font())

        # 使用等宽感知字体
        font = self.font()
        font.setPixelSize(self._font_size)
        font.setBold(self._bold)
        p.setFont(font)
        fm = QFontMetrics(font)

        text_w = fm.horizontalAdvance(self._text)
        x_start = max(0, (w - text_w) // 2)
        y_center = h // 2 + fm.ascent() // 2 - 2

        chars = list(self._text)
        char_widths = [fm.horizontalAdvance(ch) for ch in chars]

        # 计算高亮到的位置
        if self._char_progress and len(self._char_progress) >= len(chars):
            # 逐字进度模式
            highlight_count = 0
            for cp in self._char_progress:
                if cp >= 1.0:
                    highlight_count += 1
                else:
                    break
            partial_progress = (self._char_progress[highlight_count]
                                if highlight_count < len(self._char_progress) else 1.0)
        else:
            # 整体进度模式
            highlight_count = int(len(chars) * self._progress)
            partial_progress = (len(chars) * self._progress) - highlight_count

        # ── 逐字绘制 ──
        x = x_start
        for i, ch in enumerate(chars):
            ch_w = char_widths[i]

            if i < highlight_count:
                # 已高亮的字：荧光色 + 发光
                glow = QColor(self._active_color.red(),
                              self._active_color.green(),
                              self._active_color.blue(), 60)
                p.setPen(QPen(glow, 4))
                p.drawText(QRectF(x - 2, y_center - fm.ascent(),
                                   ch_w + 4, fm.height()), Qt.AlignCenter, ch)

                p.setPen(self._active_color)
                p.drawText(QRectF(x, y_center - fm.ascent(),
                                   ch_w, fm.height()), Qt.AlignCenter, ch)

            elif i == highlight_count and partial_progress > 0:
                # 当前高亮中的字：渐变（一部分亮、一部分暗）
                clip_w = int(ch_w * partial_progress)
                
                # 高亮部分
                p.setPen(self._active_color)
                p.drawText(QRectF(x, y_center - fm.ascent(),
                                   clip_w, fm.height()),
                           Qt.AlignLeft | Qt.AlignVCenter, ch)
                
                # 暗色部分
                p.setPen(self._inactive_color)
                p.drawText(QRectF(x + clip_w, y_center - fm.ascent(),
                                   ch_w - clip_w, fm.height()),
                           Qt.AlignLeft | Qt.AlignVCenter, ch)

            else:
                # 尚未高亮
                p.setPen(self._inactive_color)
                p.drawText(QRectF(x, y_center - fm.ascent(),
                                   ch_w, fm.height()), Qt.AlignCenter, ch)

            x += ch_w

        p.end()


# ════════════════════════════════════════════════
# 线程安全数据队列
# ════════════════════════════════════════════════
class _LyricsQueue(QObject):
    """线程安全的数据队列"""
    data_received = Signal(dict)

    def __init__(self):
        super().__init__()
        self._queue = Queue()
        self._timer = QTimer()
        self._timer.timeout.connect(self._drain)
        self._timer.start(50)

    def put(self, data: dict):
        self._queue.put(data)

    def _drain(self):
        try:
            while True:
                data = self._queue.get_nowait()
                self.data_received.emit(data)
        except Empty:
            pass


# 全局句柄
_lyrics_queue: Optional[_LyricsQueue] = None
_window: Optional["LyricsFloatingWindow"] = None


def show_lyrics_window():
    """显示或创建歌词窗口（带淡入动画）"""
    global _window, _lyrics_queue
    if _window is None:
        app = QApplication.instance()
        if app is None:
            return
        _lyrics_queue = _LyricsQueue()
        _window = LyricsFloatingWindow(_lyrics_queue)
    _window.show_with_animation()


def hide_lyrics_window():
    """隐藏歌词窗口（带淡出动画）"""
    global _window
    if _window:
        _window.hide_with_animation()


def destroy_lyrics_window():
    """销毁歌词窗口（完全释放 Qt 资源，防止僵尸窗口阻塞退出）"""
    global _window, _lyrics_queue
    if _window:
        try:
            _window.hide()
            _window.close()
            _window.deleteLater()
        except Exception:
            pass
        _window = None
        _lyrics_queue = None


def push_lyrics_data(data: dict):
    """向歌词窗口推送数据"""
    global _lyrics_queue
    if _lyrics_queue:
        _lyrics_queue.put(data)


# ════════════════════════════════════════════════
# 迷你进度条
# ════════════════════════════════════════════════
class _ProgressBarMini(QWidget):
    def __init__(self):
        super().__init__()
        self._progress = 0.0
        self._target = 0.0
        self._anim = QPropertyAnimation(self, b"_smooth_p")
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    def _get_smooth_p(self):
        return self._progress

    def _set_smooth_p(self, v):
        self._progress = v
        self.update()

    _smooth_p = Property(float, _get_smooth_p, _set_smooth_p)

    def set_progress(self, p: float, animate: bool = True):
        """设置进度，可选动画平滑过渡"""
        target = max(0.0, min(1.0, p))
        if animate and abs(target - self._progress) > 0.01:
            self._target = target
            self._anim.stop()
            self._anim.setStartValue(self._progress)
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._progress = target
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.NoPen)
        p.setBrush(LyricsTokens.PROGRESS_BG)
        p.drawRoundedRect(0, 0, w, h, 2, 2)
        pw = int(w * self._progress)
        if pw > 0:
            # 渐变进度
            grad = QLinearGradient(0, 0, w, 0)
            grad.setColorAt(0, QColor(80, 180, 240))
            grad.setColorAt(1, QColor(120, 220, 255))
            p.setBrush(grad)
            p.drawRoundedRect(0, 0, pw, h, 2, 2)
        p.end()


# ════════════════════════════════════════════════
# 主窗口
# ════════════════════════════════════════════════
class LyricsFloatingWindow(QMainWindow):
    """独立悬浮歌词窗口 V2 — 卡拉OK逐字高亮"""

    WIDTH = 560
    HEIGHT = 180

    def __init__(self, data_queue: _LyricsQueue, parent=None):
        super().__init__(parent)
        self._data_queue = data_queue
        self._lyrics: list[dict] = []
        self._char_times: list[dict] = []  # 逐字时间戳
        self._current_index = -1
        self._start_time = 0.0
        self._duration = 0.0
        self._song_name = ""
        self._playing = False
        self._opacity_effect = None

        self._setup_ui()
        self._setup_connections()

    def _setup_ui(self):
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMinimumWidth(self.WIDTH)
        self.setFixedHeight(self.HEIGHT)

        # 窗口透明动画
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(1.0)
        self.setGraphicsEffect(self._opacity_effect)
        self._fade_anim = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._fade_anim.setDuration(250)
        self._fade_anim.setEasingCurve(QEasingCurve.OutCubic)

        # 中央容器
        container = QFrame(self)
        container.setObjectName("glass-container")
        container.setStyleSheet("""
            #glass-container {
                background: rgba(20, 22, 30, 0.85);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 16px;
            }
        """)
        self.setCentralWidget(container)

        layout = QVBoxLayout(container)
        layout.setContentsMargins(20, 12, 20, 12)
        layout.setSpacing(6)

        # ── 顶部标题栏 ──
        title_layout = QHBoxLayout()
        self._title_label = QLabel("等待播放...")
        self._title_label.setStyleSheet(
            "color: rgba(180,190,220,0.9); font-size:12px; font-weight:600; "
            "background: transparent;"
        )

        # 固定按钮
        self._pin_btn = QLabel("📌")
        self._pin_btn.setFixedSize(24, 24)
        self._pin_btn.setAlignment(Qt.AlignCenter)
        self._pin_btn.setToolTip("固定窗口/取消固定")
        self._pin_btn.setStyleSheet(
            "color: rgba(180,190,220,0.4); font-size:12px; background: transparent; "
            "border-radius:12px;"
        )
        self._pinned = False
        self._pin_btn.mousePressEvent = self._toggle_pin

        self._close_btn = QLabel("✕")
        self._close_btn.setFixedSize(24, 24)
        self._close_btn.setAlignment(Qt.AlignCenter)
        self._close_btn.setStyleSheet(
            "color: rgba(180,190,220,0.6); font-size:14px; background: transparent; "
            "border-radius:12px;"
        )
        self._close_btn.mousePressEvent = lambda e: self.hide_with_animation()

        # Hover 效果
        def on_enter_btn(b: QLabel, color: str, bg: str):
            def handler(e):
                b.setStyleSheet(
                    f"color: {color}; font-size:14px; background: {bg}; border-radius:12px;"
                )
            return handler

        def on_leave_btn(b: QLabel, color: str):
            def handler(e):
                b.setStyleSheet(
                    f"color: {color}; font-size:14px; background: transparent; border-radius:12px;"
                )
            return handler

        self._close_btn.enterEvent = on_enter_btn(
            self._close_btn, "#ff6666", "rgba(255,100,100,0.15)")
        self._close_btn.leaveEvent = on_leave_btn(
            self._close_btn, "rgba(180,190,220,0.6)")

        title_layout.addWidget(self._title_label)
        title_layout.addStretch()
        title_layout.addWidget(self._pin_btn)
        title_layout.addWidget(self._close_btn)
        layout.addLayout(title_layout)

        # ── 歌词显示区 ──
        lyrics_area = QWidget()
        lyrics_area.setStyleSheet("background: transparent;")
        lyrics_layout = QVBoxLayout(lyrics_area)
        lyrics_layout.setContentsMargins(0, 0, 0, 0)
        lyrics_layout.setSpacing(4)

        # 上一句
        self._prev_label = QLabel("")
        self._prev_label.setAlignment(Qt.AlignCenter)
        self._prev_label.setStyleSheet(
            "color: rgba(160,170,200,0.35); font-size:13px; font-weight:400; "
            "background: transparent;"
        )
        self._prev_label.setWordWrap(True)
        self._prev_label.setMaximumHeight(24)

        # 当前句 — 使用自定义逐字高亮组件
        self._current_label = KaraokeLabel()
        self._current_label.set_params("🎵 等待播放中...", progress=0.0)

        # 下一句
        self._next_label = QLabel("")
        self._next_label.setAlignment(Qt.AlignCenter)
        self._next_label.setStyleSheet(
            "color: rgba(160,170,200,0.3); font-size:12px; font-weight:400; "
            "background: transparent;"
        )
        self._next_label.setWordWrap(True)
        self._next_label.setMaximumHeight(22)

        lyrics_layout.addWidget(self._prev_label)
        lyrics_layout.addWidget(self._current_label)
        lyrics_layout.addWidget(self._next_label)
        layout.addWidget(lyrics_area)

        # ── 进度条 ──
        self._progress_bar = _ProgressBarMini()
        self._progress_bar.setFixedHeight(4)
        layout.addWidget(self._progress_bar)

        # ── 时间 ──
        self._time_label = QLabel("00:00 / 00:00")
        self._time_label.setAlignment(Qt.AlignRight)
        self._time_label.setStyleSheet(
            "color: rgba(160,170,200,0.5); font-size:10px; background: transparent;"
        )
        layout.addWidget(self._time_label)

    def _setup_connections(self):
        self._data_queue.data_received.connect(self._on_data)
        self._sync_timer = QTimer(self)
        self._sync_timer.timeout.connect(self._sync_lyrics)
        self._sync_timer.start(50)  # 20fps 流畅同步

    # ── 固定/取消固定 ──
    def _toggle_pin(self, event):
        self._pinned = not self._pinned
        if self._pinned:
            self._pin_btn.setStyleSheet(
                "color: rgb(120,220,255); font-size:12px; "
                "background: rgba(120,220,255,0.15); border-radius:12px;"
            )
            self._pin_btn.setToolTip("已固定（不会自动隐藏）")
            self.setWindowFlags(
                Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
            )
        else:
            self._pin_btn.setStyleSheet(
                "color: rgba(180,190,220,0.4); font-size:12px; "
                "background: transparent; border-radius:12px;"
            )
            self._pin_btn.setToolTip("固定窗口/取消固定")
        self.show()

    # ── 拖拽 ──
    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event: QMouseEvent):
        if hasattr(self, '_drag_pos') and event.buttons() == Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    # ── 右键菜单 ──
    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background: #1e1e2e; color: #cdd6f4; border:1px solid #313244; border-radius:8px; }
            QMenu::item { padding:6px 20px; }
            QMenu::item:selected { background:#45475a; }
        """)
        action_pin = menu.addAction(
            "📍 取消固定" if self._pinned else "📌 固定窗口")
        action_pin.triggered.connect(lambda: self._toggle_pin(None))
        menu.addAction("隐藏歌词窗口", self.hide_with_animation)
        menu.addAction("退出", lambda: self.close())
        menu.exec(event.globalPos())

    # ── 动画：显示/隐藏 ──
    def show_with_animation(self):
        self.show()
        self.raise_()
        self._fade_anim.stop()
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.start()

    def hide_with_animation(self):
        self._fade_anim.stop()
        self._fade_anim.setStartValue(self._opacity_effect.opacity())
        self._fade_anim.setEndValue(0.0)
        # 先断开旧连接再重连，防止快速重复调用导致信号累积 → disconnect 异常
        try:
            self._fade_anim.finished.disconnect(self._on_fadeout_done)
        except (TypeError, RuntimeError):
            pass
        self._fade_anim.finished.connect(self._on_fadeout_done)
        self._fade_anim.start()

    def _on_fadeout_done(self):
        self._fade_anim.finished.disconnect(self._on_fadeout_done)
        self.hide()
        self._opacity_effect.setOpacity(1.0)

    # ── 数据接收 ──
    def _on_data(self, data: dict):
        action = data.get("action", "update")

        if action == "play":
            self._lyrics = data.get("lyrics", [])
            self._char_times = data.get("char_times", [])
            self._start_time = time.time()
            self._duration = data.get("duration", 0)
            self._song_name = data.get("song_name", "")
            self._current_index = -1
            self._playing = True
            self._title_label.setText(
                f"🎵 {self._song_name}" if self._song_name else "正在播放...")
            self._prev_label.setText("")
            self._next_label.setText("")
            self._current_label.set_params("", progress=0)
            self._time_label.setText(
                f"00:00 / {self._fmt_time(self._duration)}")
            self._progress_bar.set_progress(0, animate=False)

            # 如果之前隐藏了，自动显示
            if not self.isVisible():
                self.show_with_animation()

        elif action == "pause":
            self._playing = False
            self._sync_timer.stop()

        elif action == "resume":
            self._playing = True
            self._sync_timer.start(50)
            # 恢复时调整起始时间偏移
            elapsed = data.get("elapsed", 0)
            if elapsed > 0:
                self._start_time = time.time() - elapsed

        elif action == "progress":
            # 外部时间同步（seek 后）
            elapsed = data.get("elapsed", 0)
            if elapsed > 0:
                self._start_time = time.time() - elapsed

        elif action == "stop":
            self._playing = False
            self._sync_timer.stop()
            self._lyrics = []
            self._char_times = []
            self._current_index = -1
            self._current_label.set_params("🎵 播放结束", progress=0)
            self._prev_label.setText("")
            self._next_label.setText("")
            self._progress_bar.set_progress(0, animate=False)
            self._title_label.setText("等待播放...")
            # 延迟 2s 后自动隐藏（如果没固定）
            if not self._pinned:
                QTimer.singleShot(3000, self._auto_hide_if_idle)

    def _auto_hide_if_idle(self):
        """播放结束 3 秒后，如果还是空闲且没固定，自动隐藏"""
        if not self._playing and not self._pinned and self.isVisible():
            self.hide_with_animation()

    # ── 歌词同步（核心） ──
    def _sync_lyrics(self):
        if not self._playing or not self._lyrics:
            return

        elapsed = time.time() - self._start_time
        elapsed = max(0, elapsed)

        # 进度条 + 时间
        if self._duration > 0:
            progress = min(1.0, elapsed / self._duration)
            self._progress_bar.set_progress(progress, animate=True)
        self._time_label.setText(
            f"{self._fmt_time(elapsed)} / {self._fmt_time(self._duration)}"
        )

        # 查找当前歌词行
        new_idx = self._find_current_index(elapsed)
        if new_idx != self._current_index:
            self._current_index = new_idx
            if new_idx >= 0:
                self._refresh_lyrics_display()
                # 行切换时短暂闪烁效果（通过属性动画）
                self._pulse_current_line()

        # 逐字进度更新（始终刷新，不只在切换行时）
        self._update_karaoke_progress(elapsed)

    def _find_current_index(self, elapsed: float) -> int:
        """二分查找当前时间对应的歌词索引"""
        if not self._lyrics:
            return -1
        current = -1
        for i, line in enumerate(self._lyrics):
            line_time = line.get("time", 0)
            if line_time <= elapsed:
                current = i
            else:
                break
        return current

    def _refresh_lyrics_display(self):
        """切换歌词行时刷新显示文本"""
        idx = self._current_index
        if idx < 0:
            return
        lines = self._lyrics

        # 上一句（保留最近 2 行历史）
        prev_texts = []
        if idx - 2 >= 0:
            prev_texts.append(lines[idx - 2]["text"])
        if idx - 1 >= 0:
            prev_texts.append(lines[idx - 1]["text"])
        self._prev_label.setText("  ← ".join(prev_texts))

        # 当前句
        cur_text = lines[idx]["text"] if idx < len(lines) else ""
        font_size = 18 if len(cur_text) <= 15 else (14 if len(cur_text) <= 25 else 12)
        self._current_label.set_params(cur_text, progress=0, font_size=font_size)

        # 下一句
        next_texts = []
        if idx + 1 < len(lines):
            next_texts.append(lines[idx + 1]["text"])
        if idx + 2 < len(lines):
            next_texts.append(lines[idx + 2]["text"])
        self._next_label.setText("  → ".join(next_texts))

    def _update_karaoke_progress(self, elapsed: float):
        """更新当前行的逐字高亮进度"""
        idx = self._current_index
        if idx < 0 or idx >= len(self._lyrics):
            return

        line = self._lyrics[idx]
        line_start = line.get("time", 0)
        line_duration = line.get("duration", 3.0)
        elapsed_in_line = elapsed - line_start

        cur_text = line.get("text", "")
        n_chars = len(cur_text)

        # 检查是否有逐字时间戳
        char_times_data = None
        if idx < len(self._char_times):
            char_times_data = self._char_times[idx]

        if char_times_data and "chars" in char_times_data:
            # 逐字时间戳模式
            offsets = char_times_data["chars"]
            offsets = offsets[:n_chars] + [line_duration] * max(0, n_chars - len(offsets))
            char_progress = [
                max(0.0, min(1.0, elapsed_in_line / (offset + 0.001)))
                if offset > 0 else 1.0
                for offset in offsets[:n_chars]
            ]
            self._current_label.set_params(cur_text, progress=1.0,
                                           char_progress=char_progress)
        else:
            # 均分模式：均匀分配持续时间
            char_dur = line_duration / max(n_chars, 1)
            char_progress = [
                max(0.0, min(1.0, (elapsed_in_line - i * char_dur) / char_dur))
                for i in range(n_chars)
            ]
            overall = min(1.0, elapsed_in_line / max(line_duration, 0.01))
            self._current_label.set_params(cur_text, progress=overall,
                                           char_progress=char_progress)

    def _pulse_current_line(self):
        """行切换时短暂放大效果"""
        # 简单实现：不做脉冲，避免过度复杂
        # 如需脉冲，可在此添加 QPropertyAnimation 缩放标签
        pass

    @staticmethod
    def _fmt_time(seconds: float) -> str:
        if seconds <= 0:
            return "00:00"
        m, s = divmod(int(seconds), 60)
        return f"{m:02d}:{s:02d}"

    def closeEvent(self, event):
        event.ignore()
        self.hide_with_animation()
