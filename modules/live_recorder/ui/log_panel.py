# -*- coding: utf-8 -*-
"""
日志窗口组件

订阅 ``utils.logger.LOG_BUFFER``，把业务层（包括录制线程、调度线程）产生的日志
实时显示到界面。由于订阅回调发生在业务线程，这里通过 Qt 信号跨线程投递，
保证所有控件操作都在主线程执行。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..utils.logger import LOG_BUFFER, LogRecord

# 各日志级别的显示颜色（浅色主题）
_LEVEL_COLORS = {
    "DEBUG": "#8a8f98",
    "INFO": "#1f2328",
    "WARNING": "#b26a00",
    "ERROR": "#c0392b",
    "CRITICAL": "#8e1b1b",
}


class _LogBridge(QObject):
    """把业务线程的日志回调转成 Qt 信号，投递到主线程。"""

    record_ready = pyqtSignal(object)


class LogPanel(QWidget):
    """日志显示面板：滚动输出 + 自动滚动 + 级别过滤 + 导出。"""

    def __init__(self, parent: Optional[QWidget] = None, max_lines: int = 3000) -> None:
        super().__init__(parent)
        self._max_lines = max_lines
        self._min_level = "INFO"
        self._bridge = _LogBridge()
        self._bridge.record_ready.connect(self._append_record)

        self._build_ui()

        # 订阅全局日志缓冲区
        LOG_BUFFER.subscribe(self._on_log)

    # ------------------------------------------------------------------ 构建
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        header = QHBoxLayout()
        title = QLabel("运行日志")
        title.setStyleSheet("font-weight: 600;")
        header.addWidget(title)
        header.addStretch(1)

        self._level_label = QLabel("级别：INFO 及以上")
        self._level_label.setStyleSheet("color: #57606a;")
        header.addWidget(self._level_label)

        self._btn_debug = QPushButton("显示调试")
        self._btn_debug.setCheckable(True)
        self._btn_debug.setToolTip("勾选后显示 DEBUG 级日志，便于排查问题")
        self._btn_debug.toggled.connect(self._toggle_debug)
        header.addWidget(self._btn_debug)

        self._btn_clear = QPushButton("清空")
        self._btn_clear.clicked.connect(self.clear)
        header.addWidget(self._btn_clear)

        layout.addLayout(header)

        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setMaximumBlockCount(self._max_lines)
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(9)
        self._view.setFont(font)
        self._view.setStyleSheet(
            "QPlainTextEdit { background: #fbfbfc; border: 1px solid #d8dee4; }"
        )
        layout.addWidget(self._view, 1)

    # ------------------------------------------------------------------ 订阅
    def _on_log(self, record: LogRecord) -> None:
        """业务线程回调：转成信号投递到主线程。"""
        self._bridge.record_ready.emit(record)

    def _toggle_debug(self, enabled: bool) -> None:
        self._min_level = "DEBUG" if enabled else "INFO"
        self._level_label.setText(
            "级别：DEBUG 及以上" if enabled else "级别：INFO 及以上"
        )

    def _append_record(self, record: LogRecord) -> None:
        """在主线程中把一条日志写入控件。"""
        if not self._passes_filter(record.level):
            return

        cursor = self._view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        fmt = QTextCharFormat()
        fmt.setForeground(QColor(_LEVEL_COLORS.get(record.level, "#1f2328")))
        if record.level in ("ERROR", "CRITICAL"):
            fmt.setFontWeight(QFont.Weight.Bold)

        cursor.insertText(record.format_line() + "\n", fmt)

        # 自动滚动到底部
        scrollbar = self._view.verticalScrollBar()
        if scrollbar is not None:
            scrollbar.setValue(scrollbar.maximum())

    def _passes_filter(self, level: str) -> bool:
        """按最低级别过滤。"""
        order = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
        return order.get(level, 1) >= order.get(self._min_level, 1)

    # ------------------------------------------------------------------ 操作
    def clear(self) -> None:
        """清空界面日志（不影响已落盘文件）。"""
        self._view.clear()

    def replay_buffer(self) -> None:
        """把缓冲区中已有的日志回填到界面（用于启动时展示历史日志）。"""
        self._view.clear()
        for record in LOG_BUFFER.snapshot():
            self._append_record(record)

    def export_to_file(self, file_path: str | Path) -> int:
        """导出当前缓冲区日志到文件，返回条数。"""
        return LOG_BUFFER.export(file_path)

    def close_panel(self) -> None:
        """关闭时取消订阅，避免悬挂引用。"""
        LOG_BUFFER.unsubscribe(self._on_log)
