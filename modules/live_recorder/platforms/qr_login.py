# -*- coding: utf-8 -*-
"""
扫码登录会话（基于 Playwright）

为什么需要登录
--------------
快手网页端对**未登录访客**关闭了「按房间查询」的接口（``livedetail`` 对任意
房间号都返回同一份占位响应），并以「请求过快」拦截自动化访问。实测：无头
Chromium、有头 Chromium、有头真实 Chrome **三者都会被拦截**，说明这不是浏览器
指纹问题，而是访客级别的访问限制。因此要监控**任意指定主播**，只能带上登录态。

为什么单独写一个模块
--------------------
快手首页 ``live.kuaishou.com`` **不会自动弹出二维码**：必须先点击右上角的
「登录」，才会出现「快手APP登录 / 手机号登录」弹窗，二维码在弹窗里。
本模块把「打开页面 → 点击登录入口 → 定位二维码 → 轮询登录态 → 导出 Cookie」
固化下来，供命令行（``login.py``）和图形界面（``ui/login_dialog.py``）共用。

使用方式::

    session = QrLoginSession("kuaishou")
    png, hint = session.open()          # png 为二维码图片字节
    while not session.is_logged_in():
        time.sleep(1)
    cookie = session.cookie_string()
    session.close()

仅供个人学习与离线备份使用，禁止用于商业用途或内容二次分发。
"""

from __future__ import annotations

import base64
import re
import time
from typing import Callable, Dict, List, Optional, Tuple

from ..utils.logger import get_logger
from .playwright_helper import PlaywrightUnavailable

logger = get_logger(__name__)

__all__ = [
    "PlaywrightUnavailable",
    "QrLoginSession",
    "SPECS",
    "decode_data_url",
    "platform_label",
]


#: 在页面里定位二维码：优先 ``data:`` 图片，其次未被 CSS 模糊的 canvas。
#: 只接受接近正方形、边长 100~420px 的元素，避免把直播画面误认为二维码。
_QR_SCRIPT = """
() => {
    const square = (r) => {
        if (r.width < 100 || r.height < 100) return false;
        if (r.width > 420 || r.height > 420) return false;
        return Math.abs(r.width - r.height) <= Math.max(r.width, r.height) * 0.25;
    };
    const found = [];
    document.querySelectorAll('img').forEach((el) => {
        const src = el.src || '';
        const r = el.getBoundingClientRect();
        if (src.startsWith('data:image') && square(r)) {
            found.push({src: src, area: r.width * r.height});
        }
    });
    if (found.length === 0) {
        document.querySelectorAll('canvas').forEach((el) => {
            const cls = (el.className || '').toString();
            if (/blur|cover|video/i.test(cls)) return;
            const r = el.getBoundingClientRect();
            if (!square(r)) return;
            try {
                found.push({src: el.toDataURL('image/png'), area: r.width * r.height});
            } catch (err) { /* 跨域画布，忽略 */ }
        });
    }
    found.sort((a, b) => b.area - a.area);
    return found.length ? found[0].src : null;
}
"""

_DATA_URL_RE = re.compile(r"^data:image/[a-zA-Z0-9.+-]+;base64,(.*)$", re.S)


def decode_data_url(data_url: Optional[str]) -> Optional[bytes]:
    """把 ``data:image/png;base64,...`` 解析为原始字节；失败返回 ``None``。"""
    if not data_url:
        return None
    match = _DATA_URL_RE.match(data_url.strip())
    if not match:
        return None
    try:
        return base64.b64decode(match.group(1))
    except Exception:  # noqa: BLE001
        return None


#: 平台规格。``login_clicks`` 按顺序尝试，命中即停。
SPECS: Dict[str, dict] = {
    "kuaishou": {
        "label": "快手",
        "home": ["https://live.kuaishou.com/"],
        "config_field": "kuaishou_cookie",
        "cookie_domain": "kuaishou.com",
        # 这些 Cookie 只会在登录后出现，用于判定登录成功
        "login_markers": ("passToken", "userId", "kuaishou.live.web_st"),
        "login_clicks": ["div.no-login", "span.login", "xpath=//*[text()='登录']"],
        "hint": "打开快手 App（或快手极速版）→ 左上角侧边栏 → 扫一扫",
    },
    "douyin": {
        "label": "抖音",
        "home": ["https://live.douyin.com/", "https://www.douyin.com/"],
        "config_field": "douyin_cookie",
        "cookie_domain": "douyin.com",
        "login_markers": ("sessionid", "sid_tt", "passport_csrf_token"),
        "login_clicks": [
            "xpath=//button[contains(., '登录')]",
            "xpath=//*[normalize-space(text())='登录']",
            "div[class*='login']",
        ],
        "hint": "打开抖音 App → 右上角「扫一扫」",
    },
}


def platform_label(platform: str) -> str:
    """返回平台中文名。"""
    return SPECS.get(platform, {}).get("label", platform)


class QrLoginSession:
    """一次扫码登录会话。

    本对象**不是线程安全的**，Playwright 的同步接口要求在同一个线程里创建和使用，
    因此请让「一个线程负责一个会话」。
    """

    def __init__(
        self,
        platform: str,
        *,
        headless: bool = False,
        on_status: Optional[Callable[[str], None]] = None,
        window_width: int = 1180,
        window_height: int = 800,
    ) -> None:
        if platform not in SPECS:
            raise ValueError(f"暂不支持的平台：{platform}")
        self.platform = platform
        self.spec = SPECS[platform]
        self.headless = headless
        self.on_status = on_status
        self.window_width = window_width
        self.window_height = window_height

        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self.hint: str = self.spec["hint"]
        self.opened: bool = False

    # ------------------------------------------------------------------ 工具
    def _notify(self, message: str) -> None:
        logger.info("[%s] %s", self.spec["label"], message)
        if self.on_status is not None:
            try:
                self.on_status(message)
            except Exception:  # noqa: BLE001
                logger.debug("登录进度回调异常（已忽略）", exc_info=True)

    # ------------------------------------------------------------------ 启动
    def open(self) -> Optional[bytes]:
        """启动浏览器、进入登录二维码界面，返回二维码 PNG 字节（未取到则 ``None``）。"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise PlaywrightUnavailable(
                "未安装 playwright，请先执行：\n"
                "    pip install playwright\n"
                "    playwright install chromium"
            ) from exc

        label = self.spec["label"]
        self._notify(f"正在启动浏览器并打开{label}登录页……")
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=self.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    f"--window-size={self.window_width},{self.window_height}",
                ],
            )
        except Exception as exc:  # noqa: BLE001
            self.close()
            raise PlaywrightUnavailable(
                "启动浏览器失败。若从未安装过内核，请先执行：\n"
                "    playwright install chromium\n"
                f"原始错误：{exc}"
            ) from exc

        self._context = self._browser.new_context(
            viewport={"width": self.window_width, "height": self.window_height - 90},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        self._page = self._context.new_page()

        # 依次尝试候选首页，直到点开登录弹窗并拿到二维码
        for home in self.spec["home"]:
            try:
                self._set_status("页面加载中……")
                self._page.goto(home, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:  # noqa: BLE001
                logger.warning("打开 %s 失败：%s", home, exc)
                continue
            time.sleep(2.5)

            if self.is_logged_in():
                self._notify(f"{label}已经是登录状态，无需扫码。")
                self.opened = True
                return None

            if self._click_login_entry():
                code = self._wait_for_qr(timeout=20.0)
                self.opened = True
                if code:
                    self._notify("二维码已就绪，请用手机 App 扫码并在手机上确认登录。")
                else:
                    self._notify(
                        "页面未自动显示二维码，请在弹出的浏览器窗口里点击「登录」后扫码。"
                    )
                return code

        self.opened = True
        return None

    def _set_status(self, text: str) -> None:
        self._notify(text)

    def _click_login_entry(self) -> bool:
        """点击「登录」入口，打开二维码弹窗。"""
        assert self._page is not None
        for selector in self.spec["login_clicks"]:
            try:
                self._page.wait_for_selector(selector, timeout=6000, state="visible")
                self._page.click(selector, timeout=4000)
                time.sleep(2.0)
                logger.info("已点击登录入口：%s", selector)
                return True
            except Exception:  # noqa: BLE001
                logger.debug("登录入口未命中：%s", selector)
        return False

    def _wait_for_qr(self, timeout: float = 20.0) -> Optional[bytes]:
        """轮询等待二维码出现。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code = self.current_qr()
            if code:
                return code
            time.sleep(0.6)
        return None

    # -------------------------------------------------------------- 二维码/状态
    def current_qr(self) -> Optional[bytes]:
        """读取当前页面上的二维码图片字节（无则 ``None``）。"""
        if self._page is None:
            return None
        try:
            data_url = self._page.evaluate(_QR_SCRIPT)
        except Exception:  # noqa: BLE001
            return None
        return decode_data_url(data_url)

    def is_logged_in(self) -> bool:
        """判断是否已登录（依据登录后才出现的 Cookie）。"""
        markers = self.spec["login_markers"]
        current = self._cookie_map()
        return any(name in current for name in markers)

    def raw_cookies(self) -> List[dict]:
        """返回原始 Cookie 列表（含 domain，便于排查）。"""
        if self._context is None:
            return []
        try:
            return list(self._context.cookies())
        except Exception:  # noqa: BLE001
            return []

    def _cookie_map(self) -> Dict[str, str]:
        """按 Cookie 名去重：同名时优先「跨子域」的那一份（domain 以点开头）。"""
        chosen: Dict[str, str] = {}
        preferred: Dict[str, bool] = {}
        for item in self.raw_cookies():
            name = item.get("name")
            if not name:
                continue
            is_shared = str(item.get("domain", "")).startswith(".")
            if name not in chosen or (is_shared and not preferred.get(name, False)):
                chosen[name] = item.get("value", "")
                preferred[name] = is_shared
        return chosen

    def cookie_string(self, *, only_related: bool = True) -> str:
        """导出 ``k=v; k2=v2`` 形式的 Cookie 串。

        ``only_related`` 为真时只保留目标平台域下的 Cookie，避免把无关站点的
        标识一起塞进请求头。
        """
        suffix = str(self.spec.get("cookie_domain", ""))
        items = self.raw_cookies()
        if only_related and suffix:
            related = [c for c in items if suffix in str(c.get("domain", ""))]
            if related:
                items = related

        chosen: Dict[str, str] = {}
        preferred: Dict[str, bool] = {}
        for item in items:
            name = item.get("name")
            if not name:
                continue
            is_shared = str(item.get("domain", "")).startswith(".")
            if name not in chosen or (is_shared and not preferred.get(name, False)):
                chosen[name] = item.get("value", "")
                preferred[name] = is_shared
        return "; ".join(f"{k}={v}" for k, v in chosen.items())

    def cookie_count(self) -> int:
        """参与导出的 Cookie 数量。"""
        return len(self._cookie_map())

    # ------------------------------------------------------------------ 收尾
    def close(self) -> None:
        """关闭浏览器与 Playwright，可重复调用。"""
        for closer, name in (
            (self._context, "context"),
            (self._browser, "browser"),
            (self._pw, "playwright"),
        ):
            if closer is None:
                continue
            try:
                closer.stop() if name == "playwright" else closer.close()
            except Exception:  # noqa: BLE001
                logger.debug("关闭 %s 时出错（已忽略）", name, exc_info=True)
        self._context = None
        self._browser = None
        self._pw = None
        self._page = None
