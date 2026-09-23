# -*- coding: utf-8 -*-
"""
平台注册表

职责
----
1. 维护「平台标识 -> 解析器类」的映射；
2. 根据用户输入自动识别平台（用于批量添加时免手选）；
3. 提供统一的流地址获取入口 ``resolve_stream``，把
   「偏好画质 -> 降级画质 -> Playwright 兜底」三级策略收敛在一处。
"""

from __future__ import annotations

import threading
from typing import Dict, Optional, Type
from urllib.parse import urlparse

from ..utils.logger import get_logger
from .base import (
    BaseLiveParser,
    PlatformError,
    RoomUnavailableError,
    StreamInfo,
    StreamQuality,
)
from .douyin import DouyinParser
from .kuaishou import KuaishouParser
from .utils import extract_url

logger = get_logger(__name__)

# 平台标识 -> 解析器类
_PARSER_CLASSES: Dict[str, Type[BaseLiveParser]] = {
    "kuaishou": KuaishouParser,
    "douyin": DouyinParser,
}

# 界面展示名
PLATFORM_LABELS: Dict[str, str] = {
    "kuaishou": "快手",
    "douyin": "抖音",
}

# 各平台链接特征（host 关键字）
_HOST_HINTS: Dict[str, tuple] = {
    "kuaishou": ("kuaishou.com", "gifshow.com"),
    "douyin": ("douyin.com", "iesdouyin.com", "amemv.com"),
}

# 解析器实例缓存（httpx.Client 自带连接池与 Cookie，复用可减少握手开销）
_INSTANCES: Dict[str, BaseLiveParser] = {}
_INSTANCE_LOCK = threading.RLock()


def list_platforms() -> Dict[str, str]:
    """返回全部受支持平台：``{标识: 中文名}``。"""
    return dict(PLATFORM_LABELS)


def get_parser(platform: str, config=None) -> BaseLiveParser:
    """按平台标识获取解析器实例（单例复用）。"""
    key = (platform or "").strip().lower()
    if key not in _PARSER_CLASSES:
        raise PlatformError(f"暂不支持的平台：{platform}")
    with _INSTANCE_LOCK:
        parser = _INSTANCES.get(key)
        if parser is None:
            parser = _PARSER_CLASSES[key](config)
            _INSTANCES[key] = parser
        elif config is not None:
            parser.config = config
        return parser


def close_all() -> None:
    """关闭所有解析器的连接池（退出程序时调用）。"""
    with _INSTANCE_LOCK:
        for parser in _INSTANCES.values():
            parser.close()
        _INSTANCES.clear()


def detect_platform(text: str) -> Optional[str]:
    """根据输入文本自动识别平台标识，识别不出返回 None。

    识别顺序：先看 URL 域名，再看文本中的关键字。
    """
    text = (text or "").strip()
    if not text:
        return None

    # 1) 域名匹配
    url = extract_url(text)
    if url:
        host = urlparse(url).netloc.lower()
        for platform, hints in _HOST_HINTS.items():
            if any(hint in host for hint in hints):
                return platform

    # 2) 关键字匹配（分享口令里常出现平台名）
    lowered = text.lower()
    for platform, label in PLATFORM_LABELS.items():
        if label in text or platform in lowered:
            return platform

    # 3) 纯数字：两个平台都可能，交给用户指定（抖音 web_rid 为纯数字）
    return None


def resolve_stream(
    parser: BaseLiveParser,
    room_id: str,
    config,
) -> StreamInfo:
    """统一的流地址获取入口，实现三级降级策略。

    策略顺序
    --------
    1. 按用户偏好画质获取（原画优先）；
    2. 失败则降级到另一档画质（原画失败退超清，反之亦然）；
    3. 仍失败且开启 Playwright 时，用真实浏览器打开页面直接抓播放地址。

    这样上层调度层只需调用本函数，无需关心平台差异与降级细节。
    """
    prefer = StreamQuality.ORIGIN
    if config is not None and getattr(config, "quality_preference", "origin") == "high":
        prefer = StreamQuality.HIGH

    # --- 第 1、2 级：HTTP 接口 + 画质降级 ---
    stream: Optional[StreamInfo] = None
    # 记录失败原因，最终一并抛出：否则调用方只能看到笼统的「无法获取流地址」，
    # 无法知道究竟是未开播、风控还是接口变更。
    last_error: str = ""
    if config is None or getattr(config, "enable_fallback", True):
        try:
            stream = parser.fetch_stream_with_fallback(room_id, prefer)
        except RoomUnavailableError:
            # 平台已明确反馈「房间不可用 / 未开播」：
            # 画质降级与浏览器兜底都不会有结果，直接上抛让录制层立即收尾。
            raise
        except PlatformError as exc:
            last_error = str(exc)
            logger.warning(
                "%s 房间 %s HTTP 方式获取流地址失败：%s",
                parser.display_name,
                room_id,
                exc,
            )
    else:
        stream = parser.fetch_stream(room_id, prefer)

    if stream is not None:
        if stream.is_fallback:
            logger.warning(
                "[降级] %s 房间 %s 原画获取失败，已降级至 %s（%s）",
                parser.display_name,
                room_id,
                stream.quality.value,
                stream.quality_label,
            )
        return stream

    # --- 第 3 级：Playwright 兜底 ---
    if config is not None and getattr(config, "playwright_enabled", True):
        capture = getattr(parser, "capture_stream_via_browser", None)
        if callable(capture):
            logger.info(
                "%s 房间 %s 尝试使用浏览器兜底抓取播放地址……",
                parser.display_name,
                room_id,
            )
            try:
                info = capture(room_id)
            except PlatformError as exc:
                last_error = last_error or str(exc)
                logger.warning("浏览器兜底失败：%s", exc)
                info = None
            except Exception as exc:  # noqa: BLE001 - 兜底失败不影响主流程
                logger.warning("浏览器兜底异常：%s", exc)
                info = None
            if info is not None:
                logger.info(
                    "%s 房间 %s 浏览器兜底成功（%s）",
                    parser.display_name,
                    room_id,
                    info.quality_label,
                )
                return info

    # 把最初的失败原因一并抛出，避免调用方只看到笼统的「无法获取流地址」
    suffix = f"：{last_error}" if last_error else ""
    raise PlatformError(
        f"{parser.display_name} 房间 {room_id} 无法获取任何可用流地址{suffix}"
    )
