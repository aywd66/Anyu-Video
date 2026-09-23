# -*- coding: utf-8 -*-
"""
平台层通用工具：随机化请求头、带节流的 HTTP 客户端、URL 处理。

风控对抗策略（合规范围内）
--------------------------
1. 每次请求从 UA 池随机挑选一个真实浏览器 User-Agent；
2. 自动补齐 Referer / Origin / Accept-Language / sec-ch-ua 等常见头；
3. 请求之间加入随机延时（默认 0.5~1.5 秒），避免固定频率特征；
4. 维护 Cookie（部分平台需要 __ac_nonce、ttwid 等首访 Cookie）；
5. 失败自动退避重试，最多 3 次。
"""

from __future__ import annotations

import random
import re
import threading
import time
from typing import Dict, Optional
from urllib.parse import urlparse, urlunparse

from ..utils.logger import get_logger

logger = get_logger(__name__)

# --------------------------------------------------------------------------
# User-Agent 池（面向 Windows / Chrome / Edge，尽量贴近真实桌面浏览器）
# --------------------------------------------------------------------------
USER_AGENTS = [
    # Chrome / Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36",
    # Edge / Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Chrome / macOS（为未来跨平台预留）
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36",
]

# 各平台 Web 端根地址，用于生成 Referer / Origin
PLATFORM_ORIGINS: Dict[str, str] = {
    "kuaishou": "https://live.kuaishou.com",
    "douyin": "https://live.douyin.com",
}

_LOCK = threading.Lock()
_LAST_REQUEST_TS = 0.0


def _brotli_supported() -> bool:
    """httpx 是否具备 Brotli 解码能力。

    这一步很关键：``Accept-Encoding`` 里声明了 ``br`` 却无法解码时，
    httpx 会把 Brotli 原始字节当作文本返回（不报错），
    表现为「接口返回 200 但正则在 HTML 里什么都找不到」这类诡异问题。
    """
    try:
        import brotli  # noqa: F401
    except ImportError:
        try:
            import brotlicffi  # noqa: F401
        except ImportError:
            return False
    return True


_BROTLI_OK = _brotli_supported()

# 仅在实际具备解码能力时才声明 br，避免拿到无法解码的响应体
_ACCEPT_ENCODING = "gzip, deflate, br" if _BROTLI_OK else "gzip, deflate"


def random_user_agent() -> str:
    """随机返回一个 User-Agent。"""
    return random.choice(USER_AGENTS)


def random_profile() -> Dict[str, str]:
    """生成一套**会话级固定**的浏览器指纹。

    重要：真实浏览器在同一会话内 UA、Accept-Language 都是固定的。
    如果每个请求都重新随机（尤其与首次获取 Cookie 时的 UA 不一致），
    反而形成明显的自动化特征，更容易被风控识别。
    因此每次新建会话时随机一次，之后整个会话复用。
    """
    return {
        "User-Agent": random_user_agent(),
        "Accept-Language": random.choice(
            ["zh-CN,zh;q=0.9,en;q=0.8", "zh-CN,zh;q=0.9", "zh-CN,zh-Hans;q=0.9,en;q=0.7"]
        ),
    }


def build_headers(
    platform: str = "",
    referer: Optional[str] = None,
    extra: Optional[Dict[str, str]] = None,
    profile: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """构造一套贴近真实浏览器的请求头。

    参数
    ----
    platform: 平台标识，用于推导 Origin / Referer 默认值
    referer:  显式指定 Referer
    extra:    额外头，优先级最高
    profile:  会话级固定指纹（``random_profile()`` 的返回值）。
              不传则每次随机，仅适合一次性请求。
    """
    origin = PLATFORM_ORIGINS.get(platform, "")
    if referer is None and origin:
        referer = origin + "/"

    fingerprint = profile or random_profile()

    headers: Dict[str, str] = {
        "User-Agent": fingerprint["User-Agent"],
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": fingerprint["Accept-Language"],
        "Accept-Encoding": _ACCEPT_ENCODING,
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none" if referer is None else "same-origin",
        "Sec-Fetch-User": "?1",
    }
    if referer:
        headers["Referer"] = referer
    if origin:
        headers["Origin"] = origin
    if extra:
        headers.update(extra)
    return headers


def throttle(min_seconds: float = 0.5, max_seconds: float = 1.5) -> None:
    """全局节流：保证两次请求之间至少有 min_seconds 的间隔。

    使用全局锁 + 时间戳，多个工作线程共用同一节流窗口，
    避免并发检测时瞬间打出大量请求。
    """
    global _LAST_REQUEST_TS
    with _LOCK:
        now = time.monotonic()
        elapsed = now - _LAST_REQUEST_TS
        target_gap = random.uniform(min_seconds, max_seconds)
        if elapsed < target_gap:
            time.sleep(target_gap - elapsed)
        _LAST_REQUEST_TS = time.monotonic()


# --------------------------------------------------------------------------
# URL 工具
# --------------------------------------------------------------------------
_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)


def extract_url(text: str) -> Optional[str]:
    """从一段文本（例如分享口令）中提取第一个 URL。"""
    match = _URL_RE.search(text or "")
    return match.group(0).rstrip("，。,.；;）)") if match else None


def strip_query(url: str) -> str:
    """去掉 URL 的 query 与 fragment，便于做路径级匹配。"""
    parts = urlparse(url)
    return urlunparse((parts.scheme, parts.netloc, parts.path, "", "", ""))


def safe_json(response) -> Optional[dict]:
    """尽力把响应解析为 JSON，失败返回 None。"""
    try:
        return response.json()
    except Exception:  # noqa: BLE001 - 平台返回非 JSON 时属正常情况
        return None


def dig(data: object, *path, default=None):
    """按路径安全地深入嵌套字典/列表。

    例：``dig(payload, "data", "rooms", 0, "owner", "nickname")``
    """
    current = data
    for key in path:
        try:
            if isinstance(key, int):
                if not isinstance(current, (list, tuple)) or key >= len(current):
                    return default
                current = current[key]
            else:
                if not isinstance(current, dict) or key not in current:
                    return default
                current = current[key]
        except (IndexError, KeyError, TypeError):
            return default
    return current
