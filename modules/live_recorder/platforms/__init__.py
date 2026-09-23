# -*- coding: utf-8 -*-
"""平台解析层：负责把用户输入解析为直播间信息与可录制的流地址。"""

from .base import (
    BaseLiveParser,
    LiveStatus,
    PlatformError,
    RoomInfo,
    StreamFormat,
    StreamInfo,
    StreamQuality,
)
from .registry import PLATFORM_LABELS, detect_platform, get_parser, list_platforms

__all__ = [
    "BaseLiveParser",
    "LiveStatus",
    "PlatformError",
    "RoomInfo",
    "StreamFormat",
    "StreamInfo",
    "StreamQuality",
    "PLATFORM_LABELS",
    "detect_platform",
    "get_parser",
    "list_platforms",
]
