"""
Floating Song Queue Window V2 — 独立桌面悬浮歌曲队列窗口
磨砂背板 + 可自由拖动 + 交互式取消 + 窗口动画

布局:
┌───────────────────────────────────┐
│  📋 歌曲队列  (3首等待)     清空 ✕│  ← 标题栏（可拖拽）
│                                   │
│  ▶ 正在处理: 歌A   ████░░ 65%  ✕ │  ← 进度条 + 可取消
│                                   │
│  ⏳ #1 歌B  等待中...         ✕  │  ← 可单独取消
│  ⏳ #2 歌C  等待中...         ✕  │
│                                   │
│  ──── 历史记录 ────              │  ← 分隔线
│  ✅ 歌D  已完成  03:45           │
│  ❌ 歌E  失败: 错误原因          │
└───────────────────────────────────┘

数据格式:
  push_queue_data({
    "action": "update",
    "snapshot": {
      "now_playing": "歌A",
      "items": [{"song_name": "歌B"}, {"song_name": "歌C"}],
      "queue_size": 2,
      "progress": 0.65,   # 当前处理进度
      "status": "processing"
    }
  })
  
  push_queue_data({
    "action": "history_add",
    "item": {"song_name": "歌D", "status": "completed", "time": "03:45"}
  })
"""
from __future__ import annotations

from typing import Optional, Callable
from queue import Queue, Empty
import time

from PySide6.QtCore import (
    Qt, QTimer, Signal, QObject, QPoint,
    QPropertyAnimation, QEasingCurve, Property,
)
from PySide6.QtGui import (
    QColor, QMouseEvent, QPainter, QBrush, QPen,
    QLinearGradient, QFont,
)
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QApplication, QFrame, QScrollArea, QSizePolicy,
    QGraphicsOpacityEffect, QPushButton,
)


# ════════════════════════════════════════════════
# 设计令牌
# ════════════════════════════════════════════════
class QueueTokens:
    BG_GLASS = QColor(20, 22, 30, 200)
    TEXT_MAIN = QColor(220, 220, 240)
    TEXT_DIM = QColor(120, 120, 140)
    TEXT_ACTIVE = QColor(120, 220, 255)
    SUCCESS = QColor(100, 220, 140)
    ERROR = QColor(255, 100, 120)
    WARNING = QColor(240, 200, 80)
    PROGRESS_BG = QColor(50, 55, 70)
    PROGRESS_FG = QColor(120, 220, 255)


# ════════════════════════════════════════════════
# 迷你内联进度条组件
# ════════════════════════════════════════════════
class _InlineProgressBar(QWidget):
    """内嵌在列表行中的进度条"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._progress = 0.0
        self.setFixedSize(60, 4)

    def set_progress(self, p: float):
        self._progress = max(0.0, min(1.0, p))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        # 背景
        p.setPen(Qt.NoPen)
        p.setBrush(QueueTokens.PROGRESS_BG)
        p.drawRoundedRect(0, 0, w, h, 2, 2)
        # 进度
        pw = int(w * self._progress)
        if pw > 0:
            grad = QLinearGradient(0, 0, w, 0)
            grad.setColorAt(0, QColor(80, 180, 240))
            grad.setColorAt(1, QueueTokens.PROGRESS_FG)
            p.setBrush(grad)
            p.drawRoundedRect(0, 0, pw, h, 2, 2)
        p.end()


# ════════════════════════════════════════════════
# 线程安全数据队列
# ════════════════════════════════════════════════
class _QueueDataQueue(QObject):
    data_received = Signal(dict)

    def __init__(self):
        super().__init__()
        self._queue = Queue()
        self._timer = QTimer()
        self._timer.timeout.connect(self._drain)
        self._timer.start(100)

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
_queue_data: Optional[_QueueDataQueue] = None
_queue_window: Optional["QueueFloatingWindow"] = None
_cancel_callback: Optional[Callable] = None


def set_cancel_callback(cb: Callable):
    """设置取消回调: cb(song_name: str) -> bool"""
    global _cancel_callback
    _cancel_callback = cb


def show_queue_window():
    """显示或创建队列窗口（带淡入动画）"""
    global _queue_window, _queue_data
    if _queue_window is None:
        app = QApplication.instance()
        if app is None:
            return
        _queue_data = _QueueDataQueue()
        _queue_window = QueueFloatingWindow(_queue_data)
    _queue_window.show_with_animation()


def hide_queue_window():
    """隐藏队列窗口（带淡出动画）"""
    global _queue_window
    if _queue_window:
        _queue_window.hide_with_animation()


def push_queue_data(data: dict):
    """向队列窗口推送数据"""
    global _queue_data
    if _queue_data:
        _queue_data.put(data)


# ════════════════════════════════════════════════
# 主窗口
# ════════════════════════════════════════════════
class QueueFloatingWindow(QMainWindow):
    """独立悬浮歌曲队列窗口 V2"""

    WIDTH = 420
    HEIGHT = 320
    HISTORY_MAX = 10

    def __init__(self, data_queue: _QueueDataQueue, parent=None):
        super().__init__(parent)
        self._data_queue = data_queue
        self._history: list[dict] = []
        self._opacity_effect = None
        self._row_widgets: list[QFrame] = []  # 跟踪行组件
        self._setup_ui()
        self._setup_connections()

    def _setup_ui(self):
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMinimumWidth(self.WIDTH)
        self.resize(self.WIDTH, self.HEIGHT)
        self.setMinimumHeight(200)

        # 窗口透明度动画
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(1.0)
        self.setGraphicsEffect(self._opacity_effect)
        self._fade_anim = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._fade_anim.setDuration(250)
        self._fade_anim.setEasingCurve(QEasingCurve.OutCubic)

        # 中央容器
        container = QFrame(self)
        container.setObjectName("q-glass-container")
        container.setStyleSheet("""
            #q-glass-container {
                background: rgba(20, 22, 30, 0.85);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 16px;
            }
        """)
        self.setCentralWidget(container)

        main_layout = QVBoxLayout(container)
        main_layout.setContentsMargins(16, 12, 16, 12)
        main_layout.setSpacing(8)

        # ── 标题栏 ──
        title_layout = QHBoxLayout()
        self._title_label = QLabel("📋 歌曲队列")
        self._title_label.setStyleSheet(
            "color: rgba(180,190,220,0.9); font-size:13px; font-weight:700; "
            "background: transparent;"
        )
        self._count_label = QLabel("")
        self._count_label.setStyleSheet(
            "color: rgba(160,170,200,0.6); font-size:11px; background: transparent;"
        )

        # 清空按钮
        clear_btn = QLabel("清空")
        clear_btn.setStyleSheet(
            "color: rgba(255,130,130,0.6); font-size:11px; background: transparent; "
            "padding:2px 8px; border:1px solid rgba(255,130,130,0.2); border-radius:8px;"
        )
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.mousePressEvent = lambda e: self._on_clear_all()

        close_btn = QLabel("✕")
        close_btn.setFixedSize(24, 24)
        close_btn.setAlignment(Qt.AlignCenter)
        close_btn.setStyleSheet(
            "color: rgba(180,190,220,0.6); font-size:14px; background: transparent; "
            "border-radius:12px;"
        )
        close_btn.mousePressEvent = lambda e: self.hide_with_animation()

        def on_close_enter(e):
            close_btn.setStyleSheet(
                "color: #ff6666; font-size:14px; background: rgba(255,100,100,0.15); "
                "border-radius:12px;"
            )

        def on_close_leave(e):
            close_btn.setStyleSheet(
                "color: rgba(180,190,220,0.6); font-size:14px; background: transparent; "
                "border-radius:12px;"
            )

        close_btn.enterEvent = on_close_enter
        close_btn.leaveEvent = on_close_leave

        title_layout.addWidget(self._title_label)
        title_layout.addWidget(self._count_label)
        title_layout.addStretch()
        title_layout.addWidget(clear_btn)
        title_layout.addWidget(close_btn)
        main_layout.addLayout(title_layout)

        # ── 内容滚动区 ──
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border:none; }"
            "QScrollBar:vertical { width:4px; background:transparent; }"
            "QScrollBar::handle:vertical { background:rgba(255,255,255,0.1); "
            "border-radius:2px; min-height:20px; }"
            "QScrollBar::add-line, QScrollBar::sub-line { height:0; }"
        )

        self._content_widget = QWidget()
        self._content_layout = QVBoxLayout(self._content_widget)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(4)
        self._content_layout.addStretch()
        scroll.setWidget(self._content_widget)
        main_layout.addWidget(scroll)

    def _setup_connections(self):
        self._data_queue.data_received.connect(self._on_data)

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
        self._fade_anim.finished.connect(self._on_fadeout_done)
        self._fade_anim.start()

    def _on_fadeout_done(self):
        self._fade_anim.finished.disconnect(self._on_fadeout_done)
        self.hide()
        self._opacity_effect.setOpacity(1.0)

    # ── 拖拽 ──
    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event: QMouseEvent):
        if hasattr(self, '_drag_pos') and event.buttons() == Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def contextMenuEvent(self, event):
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background: #1e1e2e; color: #cdd6f4; border:1px solid #313244; border-radius:8px; }
            QMenu::item { padding:6px 20px; }
            QMenu::item:selected { background:#45475a; }
        """)
        menu.addAction("清空队列", self._on_clear_all)
        menu.addAction("隐藏队列窗口", self.hide_with_animation)
        menu.exec(event.globalPos())

    # ── 清空队列 ──
    def _on_clear_all(self):
        global _cancel_callback
        if _cancel_callback:
            try:
                _cancel_callback("__clear_all__")
            except Exception:
                pass

    # ── 取消单首 ──
    def _on_cancel_song(self, song_name: str):
        global _cancel_callback
        if _cancel_callback:
            try:
                _cancel_callback(song_name)
            except Exception:
                pass

    # ── 数据更新 ──
    def _on_data(self, data: dict):
        action = data.get("action", "update")
        snapshot = data.get("snapshot", {})

        if action == "update":
            self._rebuild_from_snapshot(snapshot)
        elif action == "history_add":
            self._history.insert(0, data.get("item", {}))
            if len(self._history) > self.HISTORY_MAX:
                self._history = self._history[:self.HISTORY_MAX]
            self._rebuild_from_snapshot(snapshot)

    def _rebuild_from_snapshot(self, snapshot: dict):
        """根据快照增量更新 UI（减少重建开销）"""
        # 清空现有内容
        for i in reversed(range(self._content_layout.count())):
            w = self._content_layout.itemAt(i).widget()
            if w:
                w.deleteLater()
            else:
                self._content_layout.removeItem(self._content_layout.itemAt(i))
        self._row_widgets.clear()

        now_playing = snapshot.get("now_playing")
        items = snapshot.get("items", []) or []
        qsize = snapshot.get("queue_size", len(items))
        progress = snapshot.get("progress", 0)

        self._count_label.setText(f"({qsize} 首等待)" if qsize else "")

        # ── 正在处理（进度条 + 取消按钮）──
        if now_playing:
            row = self._make_processing_row(now_playing, progress)
            self._content_layout.addWidget(row)
            self._row_widgets.append(row)

        # ── 排队中（每行带 ✕ 取消按钮）──
        for i, item in enumerate(items):
            name = item.get("song_name", str(item))
            status = item.get("status", "waiting")
            if status == "waiting":
                row = self._make_queue_row(i + 1, name)
                self._content_layout.addWidget(row)
                self._row_widgets.append(row)
            elif status == "processing":
                iprog = item.get("progress", 0)
                row = self._make_processing_row(name, iprog, cancelable=False)
                self._content_layout.addWidget(row)
                self._row_widgets.append(row)

        # ── 分隔线 + 历史 ──
        if self._history:
            sep = QLabel("─" * 35)
            sep.setStyleSheet(
                "color: rgba(255,255,255,0.05); font-size:9px; background:transparent; "
                "padding:2px 0;"
            )
            self._content_layout.addWidget(sep)

        for h in self._history:
            row = self._make_history_row(h)
            self._content_layout.addWidget(row)
            self._row_widgets.append(row)

        self._content_layout.addStretch()

    # ── 行组件构建 ──
    def _make_processing_row(self, name: str, progress: float = 0,
                             cancelable: bool = True) -> QFrame:
        """正在处理行：图标 + 名称 + 进度条 + 取消按钮"""
        row = QFrame()
        row.setStyleSheet("background: rgba(120,220,255,0.06); border-radius: 8px;")

        layout = QHBoxLayout(row)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)

        icon = QLabel("▶")
        icon.setStyleSheet(
            f"font-size:13px; color: rgb({QueueTokens.TEXT_ACTIVE.red()},"
            f"{QueueTokens.TEXT_ACTIVE.green()},{QueueTokens.TEXT_ACTIVE.blue()}); "
            "background: transparent; font-weight: bold;"
        )
        icon.setFixedWidth(20)

        name_lbl = QLabel(name[:20] + ("..." if len(name) > 20 else ""))
        name_lbl.setStyleSheet(
            f"color: rgb({QueueTokens.TEXT_ACTIVE.red()},"
            f"{QueueTokens.TEXT_ACTIVE.green()},{QueueTokens.TEXT_ACTIVE.blue()}); "
            "font-size:13px; font-weight:600; background: transparent;"
        )
        name_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        # 进度条
        pbar = _InlineProgressBar()
        pbar.set_progress(progress)

        # 百分比
        pct_label = QLabel(f"{int(progress * 100)}%")
        pct_label.setStyleSheet(
            "color: rgba(160,170,200,0.5); font-size:10px; background: transparent;"
        )
        pct_label.setFixedWidth(30)

        layout.addWidget(icon)
        layout.addWidget(name_lbl)
        layout.addWidget(pbar)
        layout.addWidget(pct_label)

        if cancelable:
            cancel_btn = self._make_cancel_button(name)
            layout.addWidget(cancel_btn)

        return row

    def _make_queue_row(self, position: int, name: str) -> QFrame:
        """排队行：序号 + 名称 + 状态 + 取消按钮"""
        row = QFrame()
        row.setStyleSheet("background: transparent; border-radius: 6px;")

        layout = QHBoxLayout(row)
        layout.setContentsMargins(8, 3, 8, 3)
        layout.setSpacing(6)

        num = QLabel(f"#{position}")
        num.setStyleSheet(
            "color: rgba(160,170,200,0.4); font-size:12px; background: transparent;"
        )
        num.setFixedWidth(22)

        name_lbl = QLabel(name[:22] + ("..." if len(name) > 22 else ""))
        name_lbl.setStyleSheet(
            "color: rgba(200,210,230,0.8); font-size:13px; background: transparent;"
        )
        name_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        status_lbl = QLabel("等待中...")
        status_lbl.setStyleSheet(
            "color: rgba(160,170,200,0.4); font-size:10px; background: transparent;"
        )

        cancel_btn = self._make_cancel_button(name)

        layout.addWidget(num)
        layout.addWidget(name_lbl)
        layout.addWidget(status_lbl)
        layout.addWidget(cancel_btn)

        return row

    def _make_history_row(self, item: dict) -> QFrame:
        """历史行：状态图标 + 名称 + 详情"""
        row = QFrame()
        row.setStyleSheet("background: transparent;")

        layout = QHBoxLayout(row)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(6)

        name = item.get("song_name", "?")
        status = item.get("status", "completed")

        if status == "completed":
            icon = "✅"
            color = QueueTokens.SUCCESS
            detail = item.get("time", "")
            detail = f"已完成 {detail}" if detail else "已完成"
        elif status == "failed":
            icon = "❌"
            color = QueueTokens.ERROR
            detail = item.get("error", "")[:30] or "失败"
        else:
            icon = "⏹"
            color = QueueTokens.TEXT_DIM
            detail = status

        icon_lbl = QLabel(icon)
        icon_lbl.setStyleSheet(
            f"font-size:12px; color: {color.name()}; background: transparent;"
        )
        icon_lbl.setFixedWidth(22)

        name_lbl = QLabel(name[:22] + ("..." if len(name) > 22 else ""))
        name_lbl.setStyleSheet(
            f"color: rgba(180,190,210,0.6); font-size:12px; background: transparent;"
        )
        name_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        detail_lbl = QLabel(detail)
        detail_lbl.setStyleSheet(
            "color: rgba(140,150,170,0.4); font-size:10px; background: transparent;"
        )

        layout.addWidget(icon_lbl)
        layout.addWidget(name_lbl)
        layout.addWidget(detail_lbl)

        return row

    def _make_cancel_button(self, song_name: str) -> QLabel:
        """创建取消 ✕ 按钮"""
        btn = QLabel("✕")
        btn.setFixedSize(20, 20)
        btn.setAlignment(Qt.AlignCenter)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip(f"取消《{song_name}》")
        btn.setStyleSheet(
            "color: rgba(180,190,220,0.3); font-size:12px; "
            "background: transparent; border-radius:10px;"
        )
        _name = song_name

        def on_enter(e):
            btn.setStyleSheet(
                "color: #ff8888; font-size:12px; "
                "background: rgba(255,100,100,0.15); border-radius:10px;"
            )

        def on_leave(e):
            btn.setStyleSheet(
                "color: rgba(180,190,220,0.3); font-size:12px; "
                "background: transparent; border-radius:10px;"
            )

        def on_click(e):
            self._on_cancel_song(_name)

        btn.enterEvent = on_enter
        btn.leaveEvent = on_leave
        btn.mousePressEvent = on_click

        return btn

    def closeEvent(self, event):
        event.ignore()
        self.hide_with_animation()
