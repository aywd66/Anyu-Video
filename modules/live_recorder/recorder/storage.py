# -*- coding: utf-8 -*-
"""
存储管理：目录组织、文件名生成、磁盘空间检查。

目录结构
--------
::

    <保存根目录>/
        └── <平台中文名>/           例：抖音
            └── <主播昵称>/          例：某某主播
                └── <日期>/          例：2026-09-23
                    ├── 抖音_某某主播_20260923_141530_001.flv
                    ├── 抖音_某某主播_20260923_141530_002.flv   # 重连/分卷产生
                    └── 抖音_某某主播_20260923_141530_merged.flv # 合并结果

文件名规则：``{平台}_{主播昵称}_{YYYYMMDD_HHMMSS}_{分段序号}.{容器}``
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..utils.logger import get_logger
from ..platforms.registry import PLATFORM_LABELS

logger = get_logger(__name__)

# Windows 文件名非法字符
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')
# 合并后文件的后缀标记
MERGED_SUFFIX = "merged"


def sanitize_name(name: str, fallback: str = "未知主播", max_length: int = 60) -> str:
    """把昵称清洗为合法且安全的目录/文件名片段。

    - 去掉 Windows 非法字符
    - 去掉首尾空白与点（Windows 不允许目录名以点结尾）
    - 限制长度，避免路径过长
    """
    cleaned = _ILLEGAL_CHARS.sub("_", (name or "").strip())
    cleaned = cleaned.strip(" .")
    if not cleaned:
        cleaned = fallback
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .")
    return cleaned or fallback


def ensure_dir(path: str | Path) -> Path:
    """确保目录存在并返回 Path 对象。"""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def build_output_dir(
    root: str | Path,
    platform: str,
    nickname: str,
    when: Optional[datetime] = None,
) -> Path:
    """按「平台/主播昵称/日期」三级目录组织，返回该目录路径。"""
    when = when or datetime.now()
    platform_label = PLATFORM_LABELS.get(platform, platform or "未知平台")
    directory = (
        Path(root)
        / sanitize_name(platform_label, "未知平台", 20)
        / sanitize_name(nickname)
        / when.strftime("%Y-%m-%d")
    )
    return ensure_dir(directory)


def build_segment_path(
    directory: str | Path,
    platform: str,
    nickname: str,
    session_start: datetime,
    segment_index: int,
    container: str = "flv",
) -> Path:
    """生成一个分片文件的完整路径。

    参数
    ----
    session_start: 本场直播的起始时间（同一场直播的所有分片共用，便于归组）
    segment_index: 分段序号，从 1 开始，固定 3 位
    container:     容器格式，flv / ts
    """
    platform_label = PLATFORM_LABELS.get(platform, platform or "未知平台")
    file_name = (
        f"{sanitize_name(platform_label, '未知平台', 20)}_"
        f"{sanitize_name(nickname)}_"
        f"{session_start.strftime('%Y%m%d_%H%M%S')}_"
        f"{segment_index:03d}.{container}"
    )
    return Path(directory) / file_name


def build_merged_path(
    directory: str | Path,
    platform: str,
    nickname: str,
    session_start: datetime,
    container: str = "flv",
) -> Path:
    """生成本场直播合并后的目标文件路径。"""
    platform_label = PLATFORM_LABELS.get(platform, platform or "未知平台")
    file_name = (
        f"{sanitize_name(platform_label, '未知平台', 20)}_"
        f"{sanitize_name(nickname)}_"
        f"{session_start.strftime('%Y%m%d_%H%M%S')}_"
        f"{MERGED_SUFFIX}.{container}"
    )
    return Path(directory) / file_name


# --------------------------------------------------------------------------
# 磁盘空间
# --------------------------------------------------------------------------
def get_free_space_gb(path: str | Path) -> float:
    """返回指定路径所在磁盘的剩余空间（GB）。

    路径不存在时，向上回溯到最近的已存在父目录再统计。
    """
    target = Path(path)
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        usage = shutil.disk_usage(str(target))
        return usage.free / (1024 ** 3)
    except OSError as exc:
        logger.warning("获取磁盘剩余空间失败（%s）：%s", path, exc)
        return float("inf")  # 统计失败时不阻塞录制


def has_enough_space(path: str | Path, min_free_gb: float) -> bool:
    """判断磁盘剩余空间是否满足下限要求。"""
    return get_free_space_gb(path) >= float(min_free_gb)
