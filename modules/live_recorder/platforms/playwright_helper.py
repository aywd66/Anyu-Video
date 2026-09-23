# -*- coding: utf-8 -*-
"""
Playwright 辅助模块（可选依赖）

用途
----
部分平台（典型如抖音）的网页接口带有动态签名参数（``a_bogus`` / ``X-Bogus`` /
``msToken``），且依赖浏览器指纹与 Cookie。纯 HTTP 请求很难稳定复现。

本模块提供两种兜底手段：
1. ``BrowserSession``       —— 启动真实 Chromium，访问直播间页面，
   通过监听网络请求直接抓到 FLV/HLS 播放地址与必要的 Cookie。
2. ``fetch_rendered_html``  —— 渲染页面后取回 HTML，交给解析器继续提取。

未安装 playwright 或未安装浏览器内核时，所有方法都会抛出
``PlaywrightUnavailable``，由调用方决定是否退回纯 HTTP 方案。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

from ..utils.logger import get_logger

logger = get_logger(__name__)


class PlaywrightUnavailable(RuntimeError):
    """playwright 不可用（未安装 / 未安装浏览器内核）。"""


# 命中这些关键字即认为是候选播放地址
_STREAM_KEYWORDS = (".flv", ".m3u8", "/hls/", "pull_url", "replay")

#: 页面正文中出现这些字样，说明被平台风控/限流拦截，而不是「未开播」。
#: 必须与「未开播」区分：把风控当成未开播会让用户看到「明明在播却显示未开播」。
_BLOCK_TEXT_MARKERS = (
    "请求过快", "请稍后重试", "操作过于频繁", "访问过于频繁",
    "安全验证", "验证码", "滑动验证", "captcha",
)

#: 跳转到这些域名也说明被拦截（滑块验证 / 验证中心）。
_BLOCK_HOSTS = ("captcha.", "verify.", "passport.", "secsdk.")


class BrowserSession:
    """一次性的浏览器会话，用于抓取直播间播放地址。

    典型用法::

        with BrowserSession(headless=True) as session:
            streams = session.capture_streams(
                url="https://live.douyin.com/123456",
                wait_seconds=8,
            )
    """

    def __init__(
        self,
        headless: bool = True,
        timeout_ms: int = 20000,
        cookies: Optional[Dict[str, str]] = None,
        cookie_domain: str = "",
    ) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        # 需要预先注入的 Cookie（用户从浏览器复制而来）。
        # 带上登录态可以绕过「未登录访客被限制」的页面，是快手等平台的关键。
        self._cookies: Dict[str, str] = dict(cookies or {})
        self._cookie_domain = cookie_domain
        self._playwright = None
        self._browser = None
        self._context = None
        self._lock = threading.RLock()
        # 最近一次抓取中「地址 -> 资源类型」，供日志排查
        self._last_detail: Dict[str, str] = {}
        # 最近一次抓取结束时页面**实际停留**的地址。
        # 部分平台会把「已下播」的直播间页面自动跳转/自动播放成推荐直播，
        # 此时抓到的流并不属于目标房间，调用方必须借此校验后丢弃。
        self._last_url: str = ""
        # 最近一次抓取时页面是否被风控/限流拦截（而不是「未开播」）
        self._last_blocked: bool = False
        # 最近一次抓取时页面正文片段，便于排查
        self._last_text: str = ""

    # ------------------------------------------------------------------ 生命周期
    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def start(self) -> None:
        """启动 Chromium。失败时抛出 ``PlaywrightUnavailable``。"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise PlaywrightUnavailable(
                "未安装 playwright，请执行：pip install playwright && playwright install chromium"
            ) from exc

        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=self.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            self._context = self._browser.new_context(
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                # 隐藏 webdriver 特征，降低被识别为自动化的概率
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
            )
            self._context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            self._inject_cookies()
        except PlaywrightUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PlaywrightUnavailable(
                f"启动浏览器失败（可能未安装内核，请执行 playwright install chromium）：{exc}"
            ) from exc

    def _inject_cookies(self) -> None:
        """把外部传入的 Cookie 注入浏览器上下文。

        这是绕过「未登录访客受限」的关键手段：快手在未登录状态下会把
        直播间页面挡住（提示「请求过快」），带上登录 Cookie 后页面即正常。
        """
        if not self._cookies or self._context is None:
            return
        domains = [self._cookie_domain] if self._cookie_domain else []
        domains += [".kuaishou.com", ".gifshow.com", ".douyin.com", ".amemv.com"]
        payload = []
        for domain in domains:
            for name, value in self._cookies.items():
                payload.append(
                    {"name": name, "value": value, "domain": domain, "path": "/"}
                )
        try:
            self._context.add_cookies(payload)
            logger.info("已在浏览器上下文中注入 %d 个 Cookie", len(self._cookies))
        except Exception as exc:  # noqa: BLE001 - 注入失败不应中断流程
            logger.debug("注入浏览器 Cookie 失败：%s", exc)

    def close(self) -> None:
        """释放浏览器资源（幂等）。"""
        with self._lock:
            for closer in (self._context, self._browser):
                try:
                    if closer is not None:
                        closer.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                if self._playwright is not None:
                    self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._context = self._browser = self._playwright = None

    # ------------------------------------------------------------------ 抓取
    def capture_streams(
        self,
        url: str,
        wait_seconds: float = 8.0,
        extra_wait_after_load: float = 3.0,
        predicate: Optional[Callable[[str], bool]] = None,
        host_hints: Optional[tuple] = None,
        warmup_url: Optional[str] = None,
    ) -> List[str]:
        """打开直播间页面，监听网络请求，返回**按可信度排序**的候选播放地址。

        排序规则的用意：页面上除了当前直播流，还可能存在「推荐位」的流地址
        （例如快手 ``liveroom/reco`` 推荐接口返回的一批流）。若只取第一个命中的
        地址，很可能录到别的房间。因此按下列优先级排序：

        1. 浏览器以 ``media`` 资源类型发起的请求（即真正在解码播放的那一路流）；
        2. 命中 ``host_hints`` 中 CDN 域名（形如 ``xxx.pull.yximgs.com``）的地址；
        3. 其余命中关键字的地址，按出现先后。

        参数
        ----
        url:                    直播间页面地址
        wait_seconds:           最长等待秒数（抓到候选地址即可提前结束）
        extra_wait_after_load:  页面加载完成后额外等待时间，给播放器留初始化时间
        predicate:              自定义过滤函数，返回 True 表示该地址可用
        host_hints:             CDN 域名关键字，用于提升排序权重
        warmup_url:             可选。先在**同一会话**里打开该地址（通常是站点首页）
                                以建立 Cookie 与站点状态，再打开目标直播间。
                                实测可显著降低被判定为「异常访问」的概率。
                                预热期间捕获的请求会被丢弃，不会污染结果。
        """
        if self._context is None:
            raise PlaywrightUnavailable("浏览器会话尚未启动")

        # url -> (resource_type, 出现序号)
        found: Dict[str, tuple] = {}
        lock = threading.Lock()
        counter = {"n": 0}

        def _on_request(request) -> None:
            try:
                req_url = request.url
                resource_type = request.resource_type
            except Exception:  # noqa: BLE001
                return
            if predicate is not None:
                hit = predicate(req_url)
            else:
                lowered = req_url.lower()
                hit = any(key in lowered for key in _STREAM_KEYWORDS)
            if not hit:
                return
            with lock:
                if req_url not in found:
                    counter["n"] += 1
                    found[req_url] = (resource_type or "", counter["n"])

        page = self._context.new_page()
        page.on("request", _on_request)

        # 预热：先访问站点首页，建立 Cookie / 站点状态后再进直播间。
        # 捕获到的首页请求必须丢弃——首页常常自己就在播放某个推荐流。
        if warmup_url:
            try:
                page.goto(
                    warmup_url, timeout=self.timeout_ms, wait_until="domcontentloaded"
                )
                page.wait_for_timeout(2500)
            except Exception as exc:  # noqa: BLE001
                logger.debug("playwright 预热页面异常（不致命）：%s", exc)
            with lock:
                found.clear()
                counter["n"] = 0

        try:
            page.goto(url, timeout=self.timeout_ms, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001 - 页面加载超时不致命
            logger.debug("playwright 打开页面异常（继续尝试抓包）：%s", exc)

        # 等待播放器发起拉流请求：
        # - 抓到 media 类型请求即可确认是正在播放的流，立即结束；
        # - 只抓到其它类型时，再给播放器留 extra_wait_after_load 秒再结束，
        #   避免每次都死等到超时。
        started = time.monotonic()
        deadline = started + max(wait_seconds, extra_wait_after_load)
        while time.monotonic() < deadline:
            with lock:
                has_media = any(rt == "media" for rt, _ in found.values())
                has_any = bool(found)
            if has_media:
                break
            if has_any and (time.monotonic() - started) >= extra_wait_after_load:
                break
            try:
                page.wait_for_timeout(500)
            except Exception:  # noqa: BLE001
                break

        try:
            final_url = page.url
        except Exception:  # noqa: BLE001
            final_url = url
        # 读取页面正文片段，用于判定「被风控」还是「确实未开播」。
        # 必须在 close 之前取。
        try:
            page_text = (page.inner_text("body") or "")[:1500]
        except Exception:  # noqa: BLE001
            page_text = ""
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass

        with lock:
            self._last_detail = {url: rt for url, (rt, _) in found.items()}
        self._last_url = final_url
        self._last_text = page_text
        self._last_blocked = self._detect_blocked(final_url, page_text)
        if self._last_blocked:
            logger.warning(
                "页面疑似被风控拦截（最终地址 %s，正文片段 %r）",
                final_url[:120],
                page_text[:120],
            )
        return self._rank(found, host_hints)

    @staticmethod
    def _detect_blocked(final_url: str, page_text: str) -> bool:
        """判断页面是否被风控/限流拦截。

        这一步的意义：被拦截时页面上往往仍有「推荐位」在播放，
        会让我们抓到**别的房间**的流。若不识别，就会出现
        「显示未开播 / 录到别人的直播」这类糟糕结果。
        """
        try:
            host = (urlparse(final_url).netloc or "").lower()
        except Exception:  # noqa: BLE001
            host = ""
        if any(h in host for h in _BLOCK_HOSTS):
            return True
        text = page_text or ""
        return any(marker in text for marker in _BLOCK_TEXT_MARKERS)

    def page_blocked(self) -> bool:
        """最近一次抓取时页面是否被风控拦截。"""
        return self._last_blocked

    def page_text(self) -> str:
        """最近一次抓取时的页面正文片段（排查用）。"""
        return self._last_text

    @staticmethod
    def _rank(found: Dict[str, tuple], host_hints: Optional[tuple]) -> List[str]:
        """把抓到的地址按可信度排序后返回。"""
        hints = tuple(h.lower() for h in (host_hints or ()))

        def score(item) -> tuple:
            req_url, (resource_type, order) = item
            lowered = req_url.lower()
            is_media = 0 if resource_type == "media" else 1
            on_hint = 0 if (hints and any(h in lowered for h in hints)) else 1
            return (is_media, on_hint, order)

        return [req_url for req_url, _ in sorted(found.items(), key=score)]

    def captured_detail(self) -> Dict[str, str]:
        """返回最近一次抓取中「地址 -> 资源类型」的映射，便于日志排查。"""
        return dict(self._last_detail)

    def final_url(self) -> str:
        """最近一次抓取结束时页面实际停留的地址（可能因跳转而变化）。"""
        return self._last_url

    def cookies_dict(self) -> Dict[str, str]:
        """导出当前上下文的 Cookie（供 ffmpeg / httpx 复用）。"""
        if self._context is None:
            return {}
        try:
            return {c["name"]: c["value"] for c in self._context.cookies()}
        except Exception:  # noqa: BLE001
            return {}


def playwright_available() -> bool:
    """快速探测 playwright 是否可导入（不启动浏览器）。"""
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True
