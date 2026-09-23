# -*- coding: utf-8 -*-
"""
「扫码登录」对话框

在程序里直接完成快手/抖音的扫码登录，无需手动去浏览器里翻 Cookie：

1. 后台线程启动一个可见浏览器，打开平台登录页并点出二维码弹窗；
2. 把页面里的二维码图片抓出来，**直接显示在对话框里**，用手机 App 扫即可；
3. 每 2 秒轮询登录态，同时自动同步二维码刷新（二维码过期时会自动换新）；
4. 登录成功后自动导出 Cookie 并回填到「设置 → 账号」。

扫码用的是同一次会话，所以在对话框里扫、在浏览器窗口里扫，效果完全一样。
"""

from __future__ import annotations

import time
from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..platforms.qr_login import SPECS, PlaywrightUnavailable, QrLoginSession, platform_label
from ..utils.logger import get_logger

logger = get_logger(__name__)

#: 对话框里二维码的显示边长（页面原图约 155px，放大后更好扫）
QR_DISPLAY_SIZE = 232


class LoginWorker(QThread):
    """后台执行扫码登录流程的线程。"""

    qr_ready = pyqtSignal(object)      # 二维码 PNG 字节（可能为 None）
    status = pyqtSignal(str)           # 进度文字
    succeeded = pyqtSignal(str)        # Cookie 字符串
    failed = pyqtSignal(str)           # 失败原因

    def __init__(self, platform: str, *, timeout: float = 300.0, parent=None) -> None:
        super().__init__(parent)
        self.platform = platform
        self.timeout = timeout
        self._cancelled = False
        self._force_check = False

    # ------------------------------------------------------------- 外部控制
    def cancel(self) -> None:
        """请求停止（线程会自行收尾并关闭浏览器）。"""
        self._cancelled = True

    def force_check(self) -> None:
        """立即复查一次登录态（用户点击「我已完成登录」时调用）。"""
        self._force_check = True

    # ------------------------------------------------------------------ 主体
    def run(self) -> None:  # noqa: D102
        session: Optional[QrLoginSession] = None
        try:
            session = QrLoginSession(
                self.platform, headless=False, on_status=self.status.emit
            )
            code = session.open()
            self.qr_ready.emit(code)
            if session.is_logged_in():
                self._finish(session)
                return

            deadline = time.monotonic() + self.timeout
            last_qr = code
            next_qr_poll = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if self._cancelled:
                    self.status.emit("已取消登录。")
                    return

                if self._force_check:
                    self._force_check = False
                    if session.is_logged_in():
                        self._finish(session)
                        return
                    self.status.emit("仍未检测到登录状态，请在浏览器窗口完成扫码后重试。")

                if session.is_logged_in():
                    self._finish(session)
                    return

                now = time.monotonic()
                if now >= next_qr_poll:
                    next_qr_poll = now + 2.0
                    fresh = session.current_qr()
                    if fresh and fresh != last_qr:
                        last_qr = fresh
                        self.qr_ready.emit(fresh)
                        self.status.emit("二维码已刷新，请用手机重新扫描。")

                remaining = int(deadline - now)
                if remaining in (240, 180, 120, 60, 30):
                    self.status.emit(f"等待扫码中……剩余 {remaining} 秒")

                time.sleep(0.4)

            self.failed.emit("等待登录超时，请重新点击「扫码登录」再试一次。")
        except PlaywrightUnavailable as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("扫码登录失败")
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    logger.debug("关闭登录会话失败", exc_info=True)

    def _finish(self, session: QrLoginSession) -> None:
        """导出 Cookie 并通知成功。"""
        cookie = session.cookie_string()
        if not cookie:
            self.failed.emit("登录成功但未取到有效 Cookie，请重试。")
            return
        logger.info("扫码登录成功（%d 个 Cookie）", session.cookie_count())
        self.succeeded.emit(cookie)


class LoginDialog(QDialog):
    """扫码登录对话框。成功后 ``self.cookie`` 为 Cookie 字符串。"""

    def __init__(self, platform: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.platform = platform
        self.label = platform_label(platform)
        self.cookie: str = ""
        self._worker: Optional[LoginWorker] = None

        self.setWindowTitle(f"扫码登录 · {self.label}")
        self.setModal(True)
        self.setMinimumWidth(360)
        self._build_ui()
        self._start_worker()

    # ------------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        tip = QLabel(
            f"请用<b>手机{self.label} App</b>扫描下方二维码并确认登录。<br>"
            f"{SPECS[self.platform]['hint']}"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #57606a; font-size: 12px;")
        layout.addWidget(tip)

        self._qr_label = QLabel("正在获取二维码……")
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setFixedSize(QR_DISPLAY_SIZE + 16, QR_DISPLAY_SIZE + 16)
        self._qr_label.setStyleSheet(
            "border: 1px solid #d0d7de; border-radius: 8px; "
            "background: #ffffff; color: #8c959f; font-size: 12px;"
        )
        qr_row = QHBoxLayout()
        qr_row.addStretch(1)
        qr_row.addWidget(self._qr_label)
        qr_row.addStretch(1)
        layout.addLayout(qr_row)

        self._status_label = QLabel("准备中……")
        self._status_label.setWordWrap(True)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setStyleSheet("color: #57606a; font-size: 12px;")
        layout.addWidget(self._status_label)

        hint = QLabel(
            "同时会打开一个浏览器窗口，两处二维码是同一张，扫任意一处都可以。\n"
            "登录凭证只保存在本机 config.json，仅用于只读的直播间查询请求。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8c959f; font-size: 11px;")
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        self._btn_done = QPushButton("我已完成登录")
        self._btn_done.setToolTip("若长时间没有自动检测到登录状态，可点此手动复查一次")
        self._btn_done.clicked.connect(self._on_force_check)
        buttons.addWidget(self._btn_done)

        buttons.addStretch(1)

        self._btn_cancel = QPushButton("取消")
        self._btn_cancel.clicked.connect(self.reject)
        buttons.addWidget(self._btn_cancel)
        layout.addLayout(buttons)

    # ------------------------------------------------------------ 线程与事件
    def _start_worker(self) -> None:
        self._worker = LoginWorker(self.platform, parent=self)
        self._worker.qr_ready.connect(self._on_qr)
        self._worker.status.connect(self._on_status)
        self._worker.succeeded.connect(self._on_succeeded)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_qr(self, data: object) -> None:
        if not isinstance(data, (bytes, bytearray)):
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(bytes(data)):
            return
        scaled = pixmap.scaled(
            QR_DISPLAY_SIZE,
            QR_DISPLAY_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._qr_label.setPixmap(scaled)
        self._qr_label.setStyleSheet(
            "border: 1px solid #d0d7de; border-radius: 8px; background: #ffffff;"
        )

    def _on_status(self, message: str) -> None:
        self._status_label.setText(message)

    def _on_succeeded(self, cookie: str) -> None:
        self.cookie = cookie
        self._status_label.setText("✅ 登录成功，正在保存凭证……")
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._status_label.setText(f"❌ {message}")
        self._status_label.setStyleSheet("color: #c0392b; font-size: 12px;")
        self._btn_done.setEnabled(False)

    def _on_force_check(self) -> None:
        if self._worker is not None:
            self._worker.force_check()

    # ------------------------------------------------------------------ 收尾
    def _stop_worker(self) -> None:
        """停止后台线程（等待它把浏览器关掉）。"""
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        worker.cancel()
        if not worker.wait(20000):
            logger.warning("扫码登录线程未在 20 秒内退出，界面已关闭")
            worker.terminate()
            worker.wait(3000)

    def reject(self) -> None:  # noqa: D102
        self._stop_worker()
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802, D102
        self._stop_worker()
        super().closeEvent(event)
