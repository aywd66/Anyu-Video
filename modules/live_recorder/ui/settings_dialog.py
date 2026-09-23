# -*- coding: utf-8 -*-
"""
「设置」对话框

集中管理轮询间隔、保存路径、画质偏好、重连参数、磁盘阈值、ffmpeg 路径等配置。
点击「确定」后由主窗口写回 ``ConfigStore`` 并落盘。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config.settings import (
    POLL_INTERVAL_MAX,
    POLL_INTERVAL_MIN,
    RECONNECT_INTERVAL_MAX,
    RECONNECT_INTERVAL_MIN,
    AppConfig,
)
from ..platforms.qr_login import platform_label
from ..recorder.ffmpeg_utils import ffmpeg_available, ffmpeg_version
from ..utils.logger import get_logger
from .login_dialog import LoginDialog

logger = get_logger(__name__)


class SettingsDialog(QDialog):
    """设置对话框。"""

    def __init__(self, config: AppConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumSize(560, 520)
        self._config = config
        self._build_ui()
        self._load_from_config(config)

    # ------------------------------------------------------------------ 构建
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        tabs = QTabWidget()
        tabs.addTab(self._build_monitor_tab(), "监控")
        tabs.addTab(self._build_record_tab(), "录制")
        tabs.addTab(self._build_resilience_tab(), "异常处理")
        tabs.addTab(self._build_account_tab(), "账号")
        tabs.addTab(self._build_env_tab(), "环境")
        layout.addWidget(tabs, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # --------------------------------------------------------------- 监控页
    def _build_monitor_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self._poll_spin = QSpinBox()
        self._poll_spin.setRange(POLL_INTERVAL_MIN, POLL_INTERVAL_MAX)
        self._poll_spin.setSuffix(" 秒")
        self._poll_spin.setToolTip("检测各直播间开播状态的间隔，间隔过短易触发平台风控")
        form.addRow("轮询间隔：", self._poll_spin)

        self._concurrency_spin = QSpinBox()
        self._concurrency_spin.setRange(1, 20)
        self._concurrency_spin.setSuffix(" 个")
        self._concurrency_spin.setToolTip("同时进行的检测请求数，建议 4~8，过高易触发风控")
        form.addRow("并发检测数：", self._concurrency_spin)

        self._max_rooms_spin = QSpinBox()
        self._max_rooms_spin.setRange(50, 1000)
        self._max_rooms_spin.setSuffix(" 个")
        form.addRow("监控对象上限：", self._max_rooms_spin)

        note = QLabel(
            "轮询采用「随机化请求头 + 请求间隔抖动」策略降低风控风险。\n"
            "监控对象较多时建议把轮询间隔调大（如 120 秒以上）。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #57606a; font-size: 11px;")
        form.addRow("", note)
        return page

    # --------------------------------------------------------------- 录制页
    def _build_record_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        # 保存路径
        path_row = QHBoxLayout()
        self._path_edit = QLineEdit()
        path_row.addWidget(self._path_edit, 1)
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._choose_save_path)
        path_row.addWidget(btn_browse)
        path_container = QWidget()
        path_container.setLayout(path_row)
        form.addRow("保存路径：", path_container)

        self._quality_combo = QComboBox()
        self._quality_combo.addItem("原画优先（1080P，最高码率）", "origin")
        self._quality_combo.addItem("超清优先（720P，更省带宽）", "high")
        form.addRow("画质偏好：", self._quality_combo)

        self._fallback_check = QCheckBox("原画获取失败时自动降级录制超清（720P）")
        form.addRow("", self._fallback_check)

        self._max_size_spin = QDoubleSpinBox()
        self._max_size_spin.setRange(0.5, 64.0)
        self._max_size_spin.setSingleStep(0.5)
        self._max_size_spin.setSuffix(" GB")
        self._max_size_spin.setToolTip("单个分片文件达到该体积后自动分卷，避免单个文件过大")
        form.addRow("单分片大小上限：", self._max_size_spin)

        self._max_duration_spin = QSpinBox()
        self._max_duration_spin.setRange(0, 1440)
        self._max_duration_spin.setSuffix(" 分钟")
        self._max_duration_spin.setSpecialValueText("不限")
        self._max_duration_spin.setToolTip("单个分片录制达到该时长后自动分卷，0 表示不限制")
        form.addRow("单分片时长上限：", self._max_duration_spin)

        self._max_record_spin = QSpinBox()
        self._max_record_spin.setRange(1, 50)
        self._max_record_spin.setSuffix(" 个")
        form.addRow("同时录制上限：", self._max_record_spin)

        self._mp4_check = QCheckBox("录制完成后转封装为 MP4（-c copy，不重编码）")
        form.addRow("", self._mp4_check)

        note = QLabel(
            "录制全程使用 <code>-c copy</code> 直存原始流，不转码，画质无损且几乎不占 CPU。\n"
            "输出容器按流类型自动选择：FLV 流存为 .flv，HLS 流存为 .ts。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #57606a; font-size: 11px;")
        form.addRow("", note)
        return page

    # ------------------------------------------------------------ 异常处理页
    def _build_resilience_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self._reconnect_spin = QSpinBox()
        self._reconnect_spin.setRange(RECONNECT_INTERVAL_MIN, RECONNECT_INTERVAL_MAX)
        self._reconnect_spin.setSuffix(" 秒")
        self._reconnect_spin.setToolTip("拉流中断或卡顿后，等待多久重新拉流（默认 15 秒）")
        form.addRow("重连间隔：", self._reconnect_spin)

        self._stall_spin = QSpinBox()
        self._stall_spin.setRange(10, 300)
        self._stall_spin.setSuffix(" 秒")
        self._stall_spin.setToolTip("输出文件连续多久没有增长即判定为卡顿并重连")
        form.addRow("卡顿判定阈值：", self._stall_spin)

        self._max_reconnect_spin = QSpinBox()
        self._max_reconnect_spin.setRange(0, 999)
        self._max_reconnect_spin.setSuffix(" 次")
        self._max_reconnect_spin.setSpecialValueText("不限")
        self._max_reconnect_spin.setToolTip("连续重连失败多少次后放弃该场直播，0 表示无限重连")
        form.addRow("最大重连次数：", self._max_reconnect_spin)

        self._disk_spin = QDoubleSpinBox()
        self._disk_spin.setRange(0.0, 500.0)
        self._disk_spin.setSingleStep(1.0)
        self._disk_spin.setSuffix(" GB")
        self._disk_spin.setToolTip("磁盘剩余低于该值时暂停全部录制任务并告警")
        form.addRow("磁盘剩余下限：", self._disk_spin)

        note = QLabel(
            "重连成功后会自动写入<b>新的分片文件</b>，已录内容不会丢失；\n"
            "下播后所有分片会自动用 ffmpeg 无损合并为一个完整文件。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #57606a; font-size: 11px;")
        form.addRow("", note)
        return page

    # --------------------------------------------------------------- 账号页
    def _build_account_tab(self) -> QWidget:
        """账号页：支持「扫码登录自动获取 Cookie」，也保留手动粘贴的入口。"""
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setSpacing(8)

        intro = QLabel(
            "<b>为什么需要登录？</b><br>"
            "快手网页端对<b>未登录访客</b>关闭了「按房间查询」的接口，"
            "未登录时程序只能监控<b>推荐流里被推荐到的房间</b>，"
            "无法监控你指定的任意主播。登录一次后即可查询任意指定直播间。"
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #57606a; font-size: 11px;")
        layout.addWidget(intro)

        # ---- 快手 ----
        layout.addWidget(self._section_title("快手账号"))

        ks_row = QHBoxLayout()
        self._btn_ks_login = QPushButton("📱 扫码登录快手（自动获取 Cookie）")
        self._btn_ks_login.setToolTip(
            "会弹出一个二维码和一个小浏览器窗口。\n"
            "用手机快手 App → 左上角侧边栏 → 扫一扫，扫完在手机上确认即可。"
        )
        self._btn_ks_login.clicked.connect(lambda: self._scan_login("kuaishou"))
        ks_row.addWidget(self._btn_ks_login)
        btn_ks_clear = QPushButton("清空")
        btn_ks_clear.setFixedWidth(72)
        btn_ks_clear.setToolTip("清除已保存的快手 Cookie")
        btn_ks_clear.clicked.connect(lambda: self._clear_cookie("kuaishou"))
        ks_row.addWidget(btn_ks_clear)
        layout.addLayout(ks_row)

        self._ks_cookie_edit = QPlainTextEdit()
        self._ks_cookie_edit.setPlaceholderText(
            "扫码登录成功后会自动填到这里；也可手动粘贴形如 "
            "did=web_xxx; kuaishou.live.web_st=xxx; kuaishou.live.web_ph=xxx 的整串 Cookie"
        )
        self._ks_cookie_edit.setMaximumHeight(84)
        self._ks_cookie_edit.setToolTip(
            "手动获取：浏览器登录 live.kuaishou.com 后按 F12 → 网络（Network）\n"
            "→ 刷新页面 → 点任意请求 → 复制「请求标头」中 Cookie 一行的完整值。"
        )
        layout.addWidget(self._ks_cookie_edit)

        self._ks_status = self._status_label()
        layout.addWidget(self._ks_status)

        reco_row = QHBoxLayout()
        self._ks_reco_pages_spin = QSpinBox()
        self._ks_reco_pages_spin.setRange(1, 10)
        self._ks_reco_pages_spin.setSuffix(" 页")
        self._ks_reco_pages_spin.setToolTip(
            "推荐流是快手唯一仍可用的「批量获取在播房间 + 流地址」入口，\n"
            "每页约 10 个房间。未登录时这是唯一可用的监控途径，页数越多覆盖越广。"
        )
        reco_row.addWidget(self._ks_reco_pages_spin)
        reco_row.addWidget(QLabel("（每页约 10 个房间）"))
        reco_row.addStretch(1)
        layout.addWidget(QLabel("快手推荐流拉取页数："))
        layout.addLayout(reco_row)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #d8dee4;")
        layout.addWidget(line)

        # ---- 抖音 ----
        layout.addWidget(self._section_title("抖音账号（可选）"))

        dy_row = QHBoxLayout()
        self._btn_dy_login = QPushButton("📱 扫码登录抖音")
        self._btn_dy_login.setToolTip(
            "抖音通常无需登录即可解析直播间，此项可选。\n"
            "若抖音侧出现风控提示，可以扫码登录后再试。"
        )
        self._btn_dy_login.clicked.connect(lambda: self._scan_login("douyin"))
        dy_row.addWidget(self._btn_dy_login)
        btn_dy_clear = QPushButton("清空")
        btn_dy_clear.setFixedWidth(72)
        btn_dy_clear.setToolTip("清除已保存的抖音 Cookie")
        btn_dy_clear.clicked.connect(lambda: self._clear_cookie("douyin"))
        dy_row.addWidget(btn_dy_clear)
        layout.addLayout(dy_row)

        self._dy_cookie_edit = QPlainTextEdit()
        self._dy_cookie_edit.setPlaceholderText("抖音通常无需登录即可解析，此项可留空")
        self._dy_cookie_edit.setMaximumHeight(64)
        layout.addWidget(self._dy_cookie_edit)

        self._dy_status = self._status_label()
        layout.addWidget(self._dy_status)

        warn = QLabel(
            "⚠ Cookie 等同于账号登录凭证，请勿分享给他人，也不要提交到任何公开仓库。"
            "本程序仅把 Cookie 用于向你关注的平台发起只读的直播间查询请求。"
        )
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #9a6700; font-size: 11px;")
        layout.addWidget(warn)
        layout.addStretch(1)

        # 内容较高，放进滚动区，避免小窗口下被挤扁
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(inner)
        return scroll

    # ------------------------------------------------------------ 账号页小件
    @staticmethod
    def _section_title(text: str) -> QLabel:
        label = QLabel(f"<b>{text}</b>")
        label.setStyleSheet("font-size: 12px; margin-top: 4px;")
        return label

    @staticmethod
    def _status_label() -> QLabel:
        label = QLabel("")
        label.setWordWrap(True)
        label.setStyleSheet("font-size: 11px;")
        return label

    def _cookie_widgets(self, platform: str):
        """返回某平台对应的（输入框, 状态标签）。"""
        if platform == "kuaishou":
            return self._ks_cookie_edit, self._ks_status
        return self._dy_cookie_edit, self._dy_status

    def _scan_login(self, platform: str) -> None:
        """打开扫码登录对话框；成功后回填 Cookie。"""
        label = platform_label(platform)
        edit, status = self._cookie_widgets(platform)
        status.setStyleSheet("font-size: 11px; color: #57606a;")
        status.setText(f"正在打开{label}登录窗口，请稍候……")

        dialog = LoginDialog(platform, self)
        # exec() 期间界面阻塞，浏览器窗口是独立进程，不受影响
        result = dialog.exec()

        if result != QDialog.DialogCode.Accepted or not dialog.cookie:
            status.setText(f"未获取到{label}登录凭证（已取消或超时）。")
            status.setStyleSheet("font-size: 11px; color: #9a6700;")
            return

        edit.setPlainText(dialog.cookie)
        status.setText(
            f"✅ 已获取{label}登录凭证，点击下方「确定」保存后生效。"
        )
        status.setStyleSheet("font-size: 11px; color: #1a7f37;")
        logger.info("已通过扫码获取 %s Cookie（长度 %d）", label, len(dialog.cookie))

    def _clear_cookie(self, platform: str) -> None:
        """清空某平台的 Cookie。"""
        edit, status = self._cookie_widgets(platform)
        edit.setPlainText("")
        label = platform_label(platform)
        status.setText(f"已清空{label} Cookie，点击「确定」后生效。")
        status.setStyleSheet("font-size: 11px; color: #9a6700;")


    # --------------------------------------------------------------- 环境页
    def _build_env_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        ffmpeg_row = QHBoxLayout()
        self._ffmpeg_edit = QLineEdit()
        self._ffmpeg_edit.setPlaceholderText("ffmpeg（留空表示使用系统 PATH 中的 ffmpeg）")
        ffmpeg_row.addWidget(self._ffmpeg_edit, 1)
        btn_pick = QPushButton("浏览…")
        btn_pick.clicked.connect(self._choose_ffmpeg)
        ffmpeg_row.addWidget(btn_pick)
        btn_test = QPushButton("检测")
        btn_test.clicked.connect(self._test_ffmpeg)
        ffmpeg_row.addWidget(btn_test)
        ffmpeg_container = QWidget()
        ffmpeg_container.setLayout(ffmpeg_row)
        form.addRow("ffmpeg 路径：", ffmpeg_container)

        self._ffmpeg_status = QLabel("")
        self._ffmpeg_status.setWordWrap(True)
        form.addRow("", self._ffmpeg_status)

        self._playwright_check = QCheckBox(
            "允许使用浏览器（Playwright）兜底解析动态签名的流地址"
        )
        self._playwright_check.setToolTip(
            "部分平台（如抖音）接口带动态签名，纯 HTTP 可能失败；\n"
            "开启后会在必要时启动无头浏览器直接抓取播放地址。\n"
            "需另行安装：pip install playwright && playwright install chromium"
        )
        form.addRow("", self._playwright_check)

        self._headless_check = QCheckBox("浏览器无头模式运行（不显示窗口）")
        form.addRow("", self._headless_check)

        note = QLabel(
            "若未把 ffmpeg 加入系统 PATH，可在此指定 <code>ffmpeg.exe</code> 的完整路径，\n"
            "或把 ffmpeg.exe 放到程序目录下的 <code>bin/</code> 文件夹中。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #57606a; font-size: 11px;")
        form.addRow("", note)
        return page

    # ------------------------------------------------------------ 载入/读取
    def _load_from_config(self, config: AppConfig) -> None:
        """把配置值填入控件。"""
        self._poll_spin.setValue(config.poll_interval)
        self._concurrency_spin.setValue(config.check_concurrency)
        self._max_rooms_spin.setValue(config.max_monitor_rooms)

        self._path_edit.setText(config.save_path)
        index = self._quality_combo.findData(config.quality_preference)
        self._quality_combo.setCurrentIndex(max(0, index))
        self._fallback_check.setChecked(config.enable_fallback)
        self._max_size_spin.setValue(config.max_file_size_gb)
        self._max_duration_spin.setValue(config.max_duration_minutes)
        self._max_record_spin.setValue(config.max_concurrent_recordings)
        self._mp4_check.setChecked(config.convert_to_mp4)

        self._reconnect_spin.setValue(config.reconnect_interval)
        self._stall_spin.setValue(config.stall_timeout)
        self._max_reconnect_spin.setValue(config.max_reconnect_attempts)
        self._disk_spin.setValue(config.min_free_disk_gb)

        self._ffmpeg_edit.setText(config.ffmpeg_path)
        self._playwright_check.setChecked(config.playwright_enabled)
        self._headless_check.setChecked(config.headless_browser)

        self._ks_cookie_edit.setPlainText(config.kuaishou_cookie or "")
        self._dy_cookie_edit.setPlainText(config.douyin_cookie or "")
        self._ks_reco_pages_spin.setValue(int(config.kuaishou_reco_pages or 3))
        self._refresh_cookie_status()

        self._update_ffmpeg_status(check=False)

    def _refresh_cookie_status(self) -> None:
        """根据当前输入框内容刷新账号状态提示。"""
        pairs = (
            ("快手", self._ks_cookie_edit, self._ks_status),
            ("抖音", self._dy_cookie_edit, self._dy_status),
        )
        for label, edit, status in pairs:
            length = len(edit.toPlainText().strip())
            if length:
                status.setText(f"✅ 已保存{label} Cookie（{length} 个字符），监控将使用该账号。")
                status.setStyleSheet("font-size: 11px; color: #1a7f37;")
            elif label == "快手":
                status.setText(
                    "⚠ 尚未登录快手：只能监控推荐流覆盖的房间，无法监控指定主播。"
                )
                status.setStyleSheet("font-size: 11px; color: #9a6700;")
            else:
                status.setText("抖音通常无需登录，可留空。")
                status.setStyleSheet("font-size: 11px; color: #8c959f;")

    def apply_to(self, config: AppConfig) -> AppConfig:
        """把控件值写回配置对象（返回同一个对象，便于链式调用）。"""
        config.poll_interval = self._poll_spin.value()
        config.check_concurrency = self._concurrency_spin.value()
        config.max_monitor_rooms = self._max_rooms_spin.value()

        config.save_path = self._path_edit.text().strip() or config.save_path
        config.quality_preference = self._quality_combo.currentData() or "origin"
        config.enable_fallback = self._fallback_check.isChecked()
        config.max_file_size_gb = self._max_size_spin.value()
        config.max_duration_minutes = self._max_duration_spin.value()
        config.max_concurrent_recordings = self._max_record_spin.value()
        config.convert_to_mp4 = self._mp4_check.isChecked()

        config.reconnect_interval = self._reconnect_spin.value()
        config.stall_timeout = self._stall_spin.value()
        config.max_reconnect_attempts = self._max_reconnect_spin.value()
        config.min_free_disk_gb = self._disk_spin.value()

        config.ffmpeg_path = self._ffmpeg_edit.text().strip() or "ffmpeg"
        config.playwright_enabled = self._playwright_check.isChecked()
        config.headless_browser = self._headless_check.isChecked()

        config.kuaishou_cookie = self._ks_cookie_edit.toPlainText().strip()
        config.douyin_cookie = self._dy_cookie_edit.toPlainText().strip()
        config.kuaishou_reco_pages = self._ks_reco_pages_spin.value()

        config.normalize()
        return config

    # ------------------------------------------------------------------ 交互
    def _choose_save_path(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择保存目录", self._path_edit.text() or str(Path.home())
        )
        if directory:
            self._path_edit.setText(directory)

    def _choose_ffmpeg(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择 ffmpeg 可执行文件", "", "可执行文件 (*.exe);;所有文件 (*.*)"
        )
        if file_path:
            self._ffmpeg_edit.setText(file_path)
            self._update_ffmpeg_status(check=True)

    def _test_ffmpeg(self) -> None:
        self._update_ffmpeg_status(check=True)

    def _update_ffmpeg_status(self, check: bool = True) -> None:
        """检测并展示 ffmpeg 可用状态。"""
        path = self._ffmpeg_edit.text().strip() or "ffmpeg"
        if not check:
            self._ffmpeg_status.setText("")
            return
        if ffmpeg_available(path):
            version = ffmpeg_version(path)
            self._ffmpeg_status.setText(f"✅ 检测通过：{version[:80]}")
            self._ffmpeg_status.setStyleSheet("color: #1a7f37;")
        else:
            self._ffmpeg_status.setText(
                "❌ 未检测到可用的 ffmpeg，请检查路径或加入系统 PATH。"
            )
            self._ffmpeg_status.setStyleSheet("color: #c0392b;")

    def _on_accept(self) -> None:
        """校验关键项后关闭。"""
        save_path = self._path_edit.text().strip()
        if not save_path:
            QMessageBox.warning(self, "提示", "请先设置录制文件的保存路径。")
            return
        try:
            Path(save_path).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "路径不可用", f"无法创建保存目录：\n{exc}")
            return

        ffmpeg_path = self._ffmpeg_edit.text().strip() or "ffmpeg"
        if not ffmpeg_available(ffmpeg_path):
            answer = QMessageBox.question(
                self,
                "ffmpeg 不可用",
                "未检测到可用的 ffmpeg，录制功能将无法工作。\n是否仍要保存设置？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.accept()
