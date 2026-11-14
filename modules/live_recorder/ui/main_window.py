# -*- coding: utf-8 -*-
"""
主窗口

布局
----
::

    ┌──────────────────────────────────────────────┐
    │ 工具栏：添加 / 删除 / 启停 / 开始监控 / 设置… │
    ├──────────────────────────────────────────────┤
    │ 直播间列表（平台、昵称、状态、时长、体积…）   │
    ├──────────────────────────────────────────────┤
    │ 运行日志（可折叠 Dock，支持导出）             │
    ├──────────────────────────────────────────────┤
    │ 状态栏：监控状态 / 对象数 / 录制中 / 磁盘剩余 │
    └──────────────────────────────────────────────┘

线程约定
--------
业务层（调度线程、录制线程）通过 ``EventBus`` 与 ``LOG_BUFFER`` 通知界面，
两者都在本窗口内被桥接为 Qt 信号后投递到主线程，
因此所有控件操作都发生在主线程，无需额外加锁。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QCursor, QGuiApplication, QKeySequence
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDockWidget,
    QFileDialog,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
)

from .. import __app_name__, __version__
from ..config.settings import ConfigStore
from ..core.events import ENGINE_EVENTS, EventBus
from ..core.monitor import MonitorEngine
from ..platforms.qr_login import SPECS, platform_label
from ..recorder.ffmpeg_utils import ffmpeg_available, ffmpeg_version
from ..recorder.storage import ensure_dir
from ..utils.logger import get_logger
from .add_room_dialog import AddRoomDialog
from .log_panel import LogPanel
from .login_dialog import LoginDialog
from .settings_dialog import SettingsDialog

logger = get_logger(__name__)

# 表格列定义：(标题, 建议宽度)
_COLUMNS = [
    ("平台", 70),
    ("主播昵称", 170),
    ("房间标识", 150),
    ("状态", 80),
    ("已录时长", 90),
    ("文件大小", 90),
    ("分片", 50),
    ("当前文件 / 成品", 260),
    ("最近检测", 90),
]

# 状态 -> 前景色（浅色主题）
_STATUS_COLORS = {
    "recording": "#1a7f37",
    "reconnecting": "#b26a00",
    "merging": "#0969da",
    "live": "#cf222e",
    "offline": "#57606a",
    "pending": "#8250df",
    "paused": "#b26a00",
    "error": "#c0392b",
    "disabled": "#8a8f98",
}


class _UiBridge(QObject):
    """把业务线程的事件转成 Qt 信号。"""

    event_ready = pyqtSignal(str, dict)


class MainWindow(QMainWindow):
    """应用主窗口。"""

    def __init__(self, store: ConfigStore) -> None:
        super().__init__()
        self.store = store
        self.bus = EventBus()
        self.engine = MonitorEngine(store, self.bus)

        # 事件桥接：业务线程 -> 主线程
        self._bridge = _UiBridge()
        self._bridge.event_ready.connect(self._on_engine_event)
        self.bus.subscribe(self._bridge.event_ready.emit)

        self._row_keys: List[str] = []
        self._last_alert_at: Optional[datetime] = None

        # 记录 config.json 的 mtime，用于探测「WebUI 侧写进来的外部新增房间」
        try:
            self._config_mtime = self.store.path.stat().st_mtime
        except OSError:
            self._config_mtime = None

        self._build_ui()
        self._build_toolbar()
        self._setup_timer()

        # 启动提示
        self._check_environment()
        self._log_panel.replay_buffer()
        self._refresh_table()

        if self.store.config.auto_start_monitor and self.store.rooms:
            logger.info("检测到上次退出时监控处于运行状态，自动恢复监控任务")
            QTimer.singleShot(800, self._start_monitor)

    # ------------------------------------------------------------------ 构建
    def _build_ui(self) -> None:
        self.setWindowTitle(f"{__app_name__} · 直播流自动录制工具 v{__version__}")
        self.resize(1180, 760)

        # ---- 表格 ----
        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels([name for name, _ in _COLUMNS])
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_context_menu)
        self._table.doubleClicked.connect(lambda _index: self._open_selected_folder())

        header = self._table.horizontalHeader()
        for index, (_, width) in enumerate(_COLUMNS):
            if index == len(_COLUMNS) - 2:
                header.setSectionResizeMode(index, QHeaderView.ResizeMode.Stretch)
            else:
                header.setSectionResizeMode(index, QHeaderView.ResizeMode.Interactive)
            self._table.setColumnWidth(index, width)

        self.setCentralWidget(self._table)

        # ---- 日志 Dock ----
        self._log_panel = LogPanel(self)
        dock = QDockWidget("运行日志", self)
        dock.setWidget(self._log_panel)
        dock.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        dock.setMinimumHeight(180)
        self._log_dock = dock

        # ---- 状态栏 ----
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._label_monitor = QLabel("监控：已停止")
        self._label_rooms = QLabel("对象：0")
        self._label_recording = QLabel("录制中：0")
        self._label_disk = QLabel("磁盘剩余：--")
        for label in (
            self._label_monitor,
            self._label_rooms,
            self._label_recording,
            self._label_disk,
        ):
            label.setMinimumWidth(140)
            self._status_bar.addPermanentWidget(label)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("主工具栏")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(toolbar)

        def add_action(text: str, slot, shortcut: str = "", tooltip: str = "") -> QAction:
            action = QAction(text, self)
            action.triggered.connect(slot)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            if tooltip:
                action.setToolTip(tooltip)
            toolbar.addAction(action)
            return action

        add_action("添加直播间", self._add_rooms, "Ctrl+N", "批量添加监控对象")
        add_action("删除选中", self._remove_selected, "Delete", "删除选中的监控对象")
        toolbar.addSeparator()

        self._action_start = add_action("▶ 开始监控", self._start_monitor, "F5", "开始轮询并自动录制")
        self._action_stop = add_action("■ 停止监控", self._stop_monitor, "F6", "停止轮询并收尾所有录制")
        self._action_stop.setEnabled(False)
        add_action("立即检测", self._check_now, "F7", "不等下一个轮询周期，立刻检测一次")
        toolbar.addSeparator()

        add_action("设置", self._open_settings, "Ctrl+,", "打开设置")
        add_action("扫码登录", self._open_login_menu, "", "扫码登录快手/抖音，自动获取并保存 Cookie")
        add_action("打开保存目录", self._open_save_dir, "", "在文件管理器中打开录制文件根目录")
        add_action("导出日志", self._export_log, "Ctrl+E", "把当前日志导出到文本文件")
        toolbar.addSeparator()
        add_action("使用说明", self._show_about, "F1", "查看使用说明与免责声明")

    def _setup_timer(self) -> None:
        """界面刷新定时器：每秒同步一次引擎状态快照。"""
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._refresh_table)
        self._timer.start()

    # ------------------------------------------------------------ 环境自检
    def _check_environment(self) -> None:
        """启动时检查 ffmpeg 与保存目录，并给出提示。"""
        config = self.store.config
        if ffmpeg_available(config.ffmpeg_path):
            logger.info("ffmpeg 检测通过：%s", ffmpeg_version(config.ffmpeg_path))
        else:
            logger.error(
                "未检测到可用的 ffmpeg！录制功能不可用。请安装 ffmpeg 或加入 PATH，"
                "也可在「设置 → 环境」中指定 ffmpeg.exe 路径。"
            )
            QTimer.singleShot(500, self._warn_ffmpeg_missing)

        try:
            ensure_dir(config.save_path)
        except OSError as exc:
            logger.error("保存目录不可用：%s（%s）", config.save_path, exc)

        logger.info("配置文件：%s", self.store.path)
        logger.info("录制文件保存目录：%s", config.save_path)
        logger.info(
            "本工具仅供个人学习与离线备份使用，请勿用于商业用途或内容二次分发。"
        )

    def _warn_ffmpeg_missing(self) -> None:
        QMessageBox.warning(
            self,
            "缺少 ffmpeg",
            "未检测到可用的 ffmpeg，录制功能将无法工作。\n\n"
            "解决办法（任选其一）：\n"
            "1. 从 https://www.gyan.dev/ffmpeg/builds/ 下载，解压后把 bin 目录加入系统 PATH；\n"
            "2. 把 ffmpeg.exe 放到程序目录下的 bin 文件夹；\n"
            "3. 在「设置 → 环境」中直接指定 ffmpeg.exe 的完整路径。",
        )

    # ------------------------------------------------------------------ 表格
    def _sync_external_rooms(self) -> None:
        """把 WebUI 侧写入 config.json 的新监控对象并入当前实例（仅增量，绝不删除）。

        WebUI「识别并分流」/「直播录制」页把直播链接 POST 给桥接服务，桥接服务直接写
        live_recorder/config.json。若录制器此刻已在运行，它不会自动感知外部改动，
        于是这里每秒用 mtime 探测一次，发现外部新增就复用 engine.add_room 并入，
        打通「网页里贴链接 → 桌面录制器立刻出现该房间」的闭环。

        只增不删：删除房间里正在录制的对象会引发停止/合并，风险远大于收益，
        因此外部删除不在此同步（用户在录制器窗口内删即可）。
        """
        try:
            mtime = self.store.path.stat().st_mtime
        except OSError:
            return
        if self._config_mtime == mtime:
            return
        self._config_mtime = mtime

        try:
            fresh = ConfigStore(self.store.path).load()
        except Exception:  # noqa: BLE001 - 配置读失败不影响主流程
            return

        added = 0
        for entry in fresh.rooms:
            if self.engine.room_runtime(entry.key) is None:
                if self.engine.add_room(entry):
                    added += 1
        if added:
            logger.info("检测到外部新增 %d 个监控对象，已自动并入", added)

    def _refresh_table(self) -> None:
        """刷新表格与状态栏。"""
        self._sync_external_rooms()
        try:
            rows = self.engine.snapshot()
        except Exception as exc:  # noqa: BLE001 - 界面刷新不能崩
            logger.debug("刷新表格失败：%s", exc)
            return

        keys = [row["key"] for row in rows]
        if keys != self._row_keys:
            self._rebuild_table(rows, keys)
        else:
            for index, row in enumerate(rows):
                self._update_row(index, row)

        self._refresh_status_bar()

    def _rebuild_table(self, rows: List[Dict], keys: List[str]) -> None:
        """表格结构变化时重建（保留选中项）。"""
        selected = set(self._selected_keys())
        self._table.setUpdatesEnabled(False)
        self._table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            for column in range(len(_COLUMNS)):
                if self._table.item(index, column) is None:
                    item = QTableWidgetItem("")
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
                    )
                    self._table.setItem(index, column, item)
            self._update_row(index, row)
        self._table.setUpdatesEnabled(True)
        self._row_keys = keys

        # 还原选中状态
        for index, row in enumerate(rows):
            if row["key"] in selected:
                self._table.selectRow(index)

    def _update_row(self, index: int, row: Dict) -> None:
        """更新一行数据。"""
        status = row["status"]
        values = [
            row["platform_label"],
            row["nickname"],
            row["room_id"] or row["raw_input"][:30],
            row["status_label"],
            _format_duration(row["recorded_seconds"]),
            _format_size(row["total_bytes"]),
            str(row["segment_count"]) if row["segment_count"] else "-",
            self._file_column_text(row),
            row["last_check"].strftime("%H:%M:%S") if row["last_check"] else "-",
        ]
        for column, text in enumerate(values):
            item = self._table.item(index, column)
            if item is None:
                item = QTableWidgetItem("")
                self._table.setItem(index, column, item)
            if item.text() != text:
                item.setText(text)

            # 状态列着色并加粗
            if column == 3:
                item.setForeground(QColor(_STATUS_COLORS.get(status, "#1f2328")))
                font = item.font()
                font.setBold(status in ("recording", "reconnecting", "merging"))
                item.setFont(font)

        # 提示信息挂到整行，便于鼠标悬停查看错误原因
        tooltip_parts = [f"输入：{row['raw_input']}"]
        if row["title"]:
            tooltip_parts.append(f"直播间标题：{row['title']}")
        if row["online_count"]:
            tooltip_parts.append(f"在线人数：{row['online_count']}")
        if row["output_dir"]:
            tooltip_parts.append(f"保存目录：{row['output_dir']}")
        if row.get("note"):
            tooltip_parts.append(f"状态说明：{row['note']}")
        if row["last_error"]:
            tooltip_parts.append(f"最近错误：{row['last_error']}")
        tooltip = "\n".join(tooltip_parts)
        for column in range(len(_COLUMNS)):
            item = self._table.item(index, column)
            if item is not None:
                item.setToolTip(tooltip)

    @staticmethod
    def _file_column_text(row: Dict) -> str:
        """「当前文件 / 成品」列的内容。"""
        if row["current_file"]:
            return row["current_file"]
        if row["final_path"]:
            return f"✔ {Path(row['final_path']).name}"
        if row["status"] == "pending":
            return "等待解析…"
        # 没录到东西时，直接把原因显示出来，避免用户只看到「失败」却不知为何
        if row["status"] in ("error", "offline"):
            if row.get("note"):
                return row["note"][:60]
            if row["status"] == "error" and row["last_error"]:
                return row["last_error"][:60]
        return "-"

    def _refresh_status_bar(self) -> None:
        """刷新状态栏统计。"""
        counts = self.engine.counts()
        running = self.engine.is_running
        self._label_monitor.setText("监控：运行中" if running else "监控：已停止")
        self._label_monitor.setStyleSheet(
            "color: #1a7f37; font-weight: 600;" if running else "color: #57606a;"
        )
        self._label_rooms.setText(f"对象：{counts['enabled']}/{counts['total']}")
        self._label_recording.setText(f"录制中：{counts['recording']}")

        free_gb = counts["free_gb"]
        threshold = self.store.config.min_free_disk_gb
        self._label_disk.setText(f"磁盘剩余：{free_gb:.1f} GB")
        if counts["paused"]:
            self._label_disk.setStyleSheet("color: #c0392b; font-weight: 600;")
            self._label_disk.setText(f"磁盘剩余：{free_gb:.1f} GB（已暂停）")
        elif free_gb < threshold + 5:
            self._label_disk.setStyleSheet("color: #b26a00;")
        else:
            self._label_disk.setStyleSheet("color: #57606a;")

    # ------------------------------------------------------------- 选中辅助
    def _selected_keys(self) -> List[str]:
        """返回当前选中行的 key 列表。"""
        keys: List[str] = []
        for index in sorted({i.row() for i in self._table.selectedIndexes()}):
            if 0 <= index < len(self._row_keys):
                keys.append(self._row_keys[index])
        return keys

    def _selected_row(self) -> Optional[Dict]:
        """返回当前选中行的数据（多选时取第一行）。"""
        keys = self._selected_keys()
        if not keys:
            return None
        for row in self.engine.snapshot():
            if row["key"] == keys[0]:
                return row
        return None

    # ------------------------------------------------------------------ 操作
    def _add_rooms(self) -> None:
        """添加监控对象（支持批量）。"""
        if len(self.store.rooms) >= self.store.config.max_monitor_rooms:
            QMessageBox.information(
                self,
                "已达上限",
                f"监控对象数量已达上限（{self.store.config.max_monitor_rooms} 个），"
                "可在「设置 → 监控」中调整。",
            )
            return

        dialog = AddRoomDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        added, duplicated = 0, 0
        for entry in dialog.entries():
            if self.engine.add_room(entry):
                added += 1
            else:
                duplicated += 1

        message = f"已添加 {added} 个监控对象。"
        if duplicated:
            message += f"\n{duplicated} 个已存在，已自动跳过。"
        if added:
            message += "\n新增对象将在下一轮检测中自动完成解析。"
        QMessageBox.information(self, "添加完成", message)
        self._refresh_table()

    def _remove_selected(self) -> None:
        """删除选中的监控对象。"""
        keys = self._selected_keys()
        if not keys:
            QMessageBox.information(self, "提示", "请先在列表中选择要删除的直播间。")
            return

        recording = [
            key
            for key in keys
            if (runtime := self.engine.room_runtime(key)) is not None and runtime.is_recording()
        ]
        extra = (
            f"\n\n注意：其中 {len(recording)} 个正在录制，删除后会立即停止并合并已录内容。"
            if recording
            else ""
        )
        answer = QMessageBox.question(
            self,
            "确认删除",
            f"确定删除选中的 {len(keys)} 个监控对象吗？{extra}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        for key in keys:
            self.engine.remove_room(key)
        self._refresh_table()

    def _toggle_enabled(self) -> None:
        """启用/停用选中的监控对象。"""
        keys = self._selected_keys()
        if not keys:
            return
        # 以第一行的当前状态决定本次操作方向
        row = self._selected_row()
        target = not (row["enabled"] if row else True)
        for key in keys:
            self.engine.set_room_enabled(key, target)
        logger.info("%s %d 个监控对象", "已启用" if target else "已停用", len(keys))
        self._refresh_table()

    def _start_monitor(self) -> None:
        """开始监控。"""
        if not self.store.rooms:
            QMessageBox.information(
                self, "提示", "还没有监控对象，请先点击「添加直播间」。"
            )
            return
        if not ffmpeg_available(self.store.config.ffmpeg_path):
            answer = QMessageBox.question(
                self,
                "ffmpeg 不可用",
                "未检测到可用的 ffmpeg，开始监控后将无法录制。\n是否仍然继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self.engine.start()
        self._action_start.setEnabled(False)
        self._action_stop.setEnabled(True)
        self._refresh_status_bar()

    def _stop_monitor(self) -> None:
        """停止监控（异步收尾，不阻塞界面）。"""
        if not self.engine.is_running:
            return
        count = self.engine.counts()["recording"]
        if count:
            answer = QMessageBox.question(
                self,
                "停止监控",
                f"当前有 {count} 个直播间正在录制。\n"
                "停止后会自动结束录制并合并分片，是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self.engine.stop()
        self._action_start.setEnabled(True)
        self._action_stop.setEnabled(False)
        self._refresh_status_bar()

    def _check_now(self) -> None:
        """立即触发一次检测。"""
        if not self.engine.is_running:
            QMessageBox.information(self, "提示", "请先点击「开始监控」。")
            return
        self.engine.force_check_now()
        logger.info("已触发立即检测")

    def _stop_selected_recording(self) -> None:
        """手动停止选中行的录制。"""
        keys = self._selected_keys()
        stopped = 0
        for key in keys:
            if self.engine.stop_room_recording(key):
                stopped += 1
        if stopped:
            logger.info("已手动停止 %d 个录制任务，正在合并分片……", stopped)
        else:
            QMessageBox.information(self, "提示", "选中的直播间当前没有正在进行的录制。")

    def _open_settings(self) -> None:
        """打开设置对话框。"""
        dialog = SettingsDialog(self.store.config, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        dialog.apply_to(self.store.config)
        self.store.save()
        logger.info(
            "设置已保存：轮询 %ds / 重连 %ds / 画质 %s / 保存至 %s",
            self.store.config.poll_interval,
            self.store.config.reconnect_interval,
            self.store.config.quality_preference,
            self.store.config.save_path,
        )
        # 让新的轮询间隔尽快生效
        if self.engine.is_running:
            self.engine.force_check_now()
        self._refresh_status_bar()

    def _open_login_menu(self) -> None:
        """弹出「扫码登录」菜单，选择要登录的平台。"""
        menu = QMenu(self)
        action_ks = menu.addAction("扫码登录快手（推荐）")
        action_ks.setToolTip("登录后才能监控你指定的任意快手主播")
        action_dy = menu.addAction("扫码登录抖音（可选）")
        action_dy.setToolTip("抖音通常无需登录即可解析")
        chosen = menu.exec(QCursor.pos())
        if chosen is action_ks:
            self._scan_login("kuaishou")
        elif chosen is action_dy:
            self._scan_login("douyin")

    def _scan_login(self, platform: str) -> None:
        """扫码登录并把 Cookie 写入配置。"""
        label = platform_label(platform)
        dialog = LoginDialog(platform, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.cookie:
            logger.info("已取消%s扫码登录", label)
            return
        setattr(self.store.config, SPECS[platform]["config_field"], dialog.cookie)
        self.store.config.normalize()
        self.store.save()
        logger.info("已保存%s登录凭证（%d 字符），可查询任意指定直播间", label, len(dialog.cookie))
        QMessageBox.information(
            self,
            "登录成功",
            f"{label}登录凭证已保存。\n\n"
            f"现在可以监控你指定的任意{label}主播了。\n"
            f"（凭证保存在本机 config.json，仅用于只读的直播间查询请求。）",
        )
        if self.engine.is_running:
            self.engine.force_check_now()

    def _open_save_dir(self) -> None:
        """打开录制文件根目录。"""
        _open_path_in_explorer(Path(self.store.config.save_path))

    def _open_selected_folder(self) -> None:
        """打开选中行对应的录制目录。"""
        row = self._selected_row()
        if not row or not row["output_dir"]:
            QMessageBox.information(self, "提示", "请先选择一行。")
            return
        target = Path(row["output_dir"])
        if not target.exists():
            target = Path(self.store.config.save_path)
        _open_path_in_explorer(target)

    def _copy_selected(self) -> None:
        """复制选中行的原始输入到剪贴板。"""
        row = self._selected_row()
        if not row:
            return
        QGuiApplication.clipboard().setText(row["raw_input"])
        self._status_bar.showMessage("已复制到剪贴板", 2000)

    def _export_log(self) -> None:
        """导出日志到文件。"""
        default_name = f"live_recorder_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        default_path = str(Path(self.store.config.save_path) / default_name)
        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出日志", default_path, "文本文件 (*.txt);;所有文件 (*.*)"
        )
        if not file_path:
            return
        try:
            count = self._log_panel.export_to_file(file_path)
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", f"无法写入文件：\n{exc}")
            return
        logger.info("已导出 %d 条日志到 %s", count, file_path)
        QMessageBox.information(self, "导出完成", f"已导出 {count} 条日志到：\n{file_path}")

    # ------------------------------------------------------------------ 事件
    def _on_engine_event(self, event: str, payload: Dict) -> None:
        """处理引擎事件（已在主线程执行）。"""
        if event == ENGINE_EVENTS.MONITOR_STARTED:
            self._action_start.setEnabled(False)
            self._action_stop.setEnabled(True)

        elif event == ENGINE_EVENTS.MONITOR_STOPPED:
            self._action_start.setEnabled(True)
            self._action_stop.setEnabled(False)

        elif event == ENGINE_EVENTS.RECORDING_FINISHED:
            file_path = payload.get("file", "")
            if file_path:
                self._status_bar.showMessage(
                    f"录制完成：{Path(file_path).name}", 8000
                )

        elif event == ENGINE_EVENTS.ALERT:
            self._show_alert(payload)

        elif event == ENGINE_EVENTS.QUALITY_FALLBACK:
            self._status_bar.showMessage(
                f"{payload.get('nickname', '')} 原画获取失败，已降级为 "
                f"{payload.get('quality_label', '超清')}",
                8000,
            )

        self._refresh_table()

    def _show_alert(self, payload: Dict) -> None:
        """显示告警弹窗（10 秒内不重复弹窗，避免刷屏）。"""
        now = datetime.now()
        if self._last_alert_at and (now - self._last_alert_at).total_seconds() < 10:
            return
        self._last_alert_at = now
        QMessageBox.warning(
            self,
            payload.get("title", "提示"),
            payload.get("message", ""),
        )

    def _show_context_menu(self, position) -> None:
        """表格右键菜单。"""
        if not self._table.itemAt(position):
            return
        menu = QMenu(self)
        menu.addAction("复制原始输入", self._copy_selected)
        menu.addAction("打开录制目录", self._open_selected_folder)
        menu.addSeparator()
        menu.addAction("停止该房间录制", self._stop_selected_recording)
        menu.addAction("启用/停用", self._toggle_enabled)
        menu.addSeparator()
        menu.addAction("删除", self._remove_selected)
        menu.exec(self._table.viewport().mapToGlobal(position))

    # ------------------------------------------------------------------ 说明
    def _show_about(self) -> None:
        """使用说明与免责声明。"""
        QMessageBox.information(
            self,
            "使用说明 / 免责声明",
            f"<b>{__app_name__} v{__version__}</b><br><br>"
            "<b>使用步骤</b><br>"
            "1. 点击「添加直播间」，粘贴分享链接或房间号（支持批量，每行一条）；<br>"
            "2. 点击「开始监控」，程序会按设定间隔检测开播状态；<br>"
            "3. 检测到开播后自动以原画画质开始录制，下播后自动停止并合并分片。<br><br>"
            "<b>文件组织</b><br>"
            "保存路径 / 平台 / 主播昵称 /<br>"
            "分片：平台-主播昵称-日期_时刻-分段序号.flv<br>"
            "成品：平台-主播昵称-日期_时刻-merged.flv<br><br>"
            "<b>命名说明</b><br>"
            "· 日期时间<b>直接写在文件名里</b>（如 2026-08-28_141530），"
            "不再为日期单独建文件夹；<br>"
            "· 同一个主播的所有录像都放在同一个文件夹下，按文件名排序即为时间顺序；<br>"
            "· 同一秒的多场直播靠分段序号与 -2 / -3 去重区分，不会互相覆盖；<br>"
            "· 昵称识别失败时用「未知主播」占位，目录追加房间号消歧"
            "（未知主播-房间号），绝不会把房间号伪装成昵称；<br>"
            "· 同名文件自动追加 -2 / -3 …，<b>任何情况下都不会覆盖已有录像</b>。<br><br>"
            "<b>免责声明</b><br>"
            "本工具仅供个人学习、技术研究与<b>离线备份</b>使用。<br>"
            "请勿用于商业用途，请勿对录制内容进行二次分发；<br>"
            "使用本工具产生的一切后果由使用者自行承担，请遵守相关平台的服务条款与法律法规。",
        )

    # ------------------------------------------------------------------ 关闭
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名约定
        """退出前询问并完成收尾。"""
        if self.engine.is_running or self.engine.counts()["recording"]:
            answer = QMessageBox.question(
                self,
                "退出确认",
                "监控仍在运行，退出将停止全部录制并合并已录分片。\n是否确定退出？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

        self._timer.stop()
        self._log_panel.close_panel()
        self.bus.unsubscribe(self._bridge.event_ready.emit)

        logger.info("正在退出，正在收尾录制任务……")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.engine.shutdown()
            self.store.save()
        finally:
            QApplication.restoreOverrideCursor()

        logger.info("已安全退出")
        event.accept()


# --------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------
def _format_duration(seconds: float) -> str:
    """把秒数格式化为 HH:MM:SS 或 MM:SS。"""
    total = int(seconds or 0)
    if total <= 0:
        return "-"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _format_size(num_bytes: int) -> str:
    """把字节数格式化为人类可读的体积。"""
    size = float(num_bytes or 0)
    if size <= 0:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def _open_path_in_explorer(path: Path) -> None:
    """在系统文件管理器中打开目录（跨平台）。"""
    target = Path(path)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    try:
        if sys.platform == "win32":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except Exception as exc:  # noqa: BLE001 - 打不开目录不影响主流程
        logger.warning("打开目录失败：%s（%s）", target, exc)
