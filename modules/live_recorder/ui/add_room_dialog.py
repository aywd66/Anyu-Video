# -*- coding: utf-8 -*-
"""
「添加直播间」对话框

支持批量添加：每行一个分享链接或房间号，一次可粘贴数十条。
平台可选择「自动识别」，也可强制指定，避免歧义（例如纯数字房间号）。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config.settings import RoomEntry
from ..platforms.registry import PLATFORM_LABELS, detect_platform

# 平台下拉项：显示名 -> 平台标识（空串表示自动识别）
_PLATFORM_ITEMS = [("自动识别（推荐）", "")]
_PLATFORM_ITEMS += [(label, key) for key, label in PLATFORM_LABELS.items()]


class AddRoomDialog(QDialog):
    """添加监控对象对话框。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("添加直播间")
        self.setMinimumSize(620, 460)
        self._entries: List[RoomEntry] = []
        self._build_ui()

    # ------------------------------------------------------------------ 构建
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        tip = QLabel(
            "每行填写一个直播间的<b>分享链接</b>或<b>房间号</b>，支持一次粘贴多行批量添加。\n"
            "示例：<code>https://live.douyin.com/123456789</code>　"
            "<code>https://v.kuaishou.com/xxxxxx</code>　<code>123456789</code>"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #57606a;")
        layout.addWidget(tip)

        # ---- 输入区 ----
        input_group = QGroupBox("直播间清单")
        input_layout = QVBoxLayout(input_group)

        self._text_edit = QPlainTextEdit()
        self._text_edit.setPlaceholderText(
            "https://live.douyin.com/123456789\n"
            "https://v.kuaishou.com/abcdEF\n"
            "123456789"
        )
        input_layout.addWidget(self._text_edit)

        btn_row = QHBoxLayout()
        btn_import = QPushButton("从文本文件导入…")
        btn_import.clicked.connect(self._import_from_file)
        btn_row.addWidget(btn_import)
        btn_row.addStretch(1)
        self._count_label = QLabel("已输入 0 行")
        self._count_label.setStyleSheet("color: #57606a;")
        btn_row.addWidget(self._count_label)
        input_layout.addLayout(btn_row)

        self._text_edit.textChanged.connect(self._update_count)
        layout.addWidget(input_group, 1)

        # ---- 选项区 ----
        option_group = QGroupBox("选项")
        form = QFormLayout(option_group)

        self._platform_combo = QComboBox()
        for label, value in _PLATFORM_ITEMS:
            self._platform_combo.addItem(label, value)
        form.addRow("平台：", self._platform_combo)

        self._remark_edit = QLineEdit()
        self._remark_edit.setPlaceholderText("可选，用于自己标注（例如「游戏区主播」）")
        form.addRow("备注：", self._remark_edit)

        hint = QLabel(
            "提示：纯数字房间号建议手动指定平台；含链接的输入可交给自动识别。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #6e7781; font-size: 11px;")
        form.addRow("", hint)

        layout.addWidget(option_group)

        # ---- 按钮 ----
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("添加")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------ 交互
    def _update_count(self) -> None:
        self._count_label.setText(f"已输入 {len(self._raw_lines())} 行")

    def _raw_lines(self) -> List[str]:
        return [
            line.strip()
            for line in self._text_edit.toPlainText().splitlines()
            if line.strip()
        ]

    def _import_from_file(self) -> None:
        """从文本文件导入清单（每行一条）。"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择清单文件", "", "文本文件 (*.txt);;所有文件 (*.*)"
        )
        if not file_path:
            return
        try:
            content = Path(file_path).read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            QMessageBox.warning(self, "读取失败", f"无法读取文件：\n{exc}")
            return
        self._text_edit.setPlainText(content)
        self._update_count()

    def _on_accept(self) -> None:
        """校验并生成待添加的条目列表。"""
        lines = self._raw_lines()
        if not lines:
            QMessageBox.information(self, "提示", "请至少填写一个分享链接或房间号。")
            return

        forced_platform = self._platform_combo.currentData() or ""
        remark = self._remark_edit.text().strip()

        entries: List[RoomEntry] = []
        seen: set = set()
        for line in lines:
            platform = forced_platform or (detect_platform(line) or "")
            entry = RoomEntry(
                platform=platform,
                raw_input=line,
                remark=remark,
            )
            # 同批次内去重
            dedup_key = f"{platform}:{line}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            entries.append(entry)

        if not entries:
            QMessageBox.information(self, "提示", "没有可添加的有效条目。")
            return
        self._entries = entries
        self.accept()

    # ------------------------------------------------------------------ 结果
    def entries(self) -> List[RoomEntry]:
        """返回用户确认添加的条目列表。"""
        return self._entries
