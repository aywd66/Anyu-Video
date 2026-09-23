# -*- coding: utf-8 -*-
"""录制层：ffmpeg 拉流录制、分片合并、存储与磁盘管理。"""

from .ffmpeg_recorder import RecorderState, RecorderStats, StreamRecorder
from .ffmpeg_utils import ffmpeg_available, ffmpeg_version, resolve_ffmpeg
from .merger import merge_segments, remux_to_mp4
from .storage import (
    build_output_dir,
    build_segment_path,
    ensure_dir,
    get_free_space_gb,
    has_enough_space,
    sanitize_name,
)

__all__ = [
    "RecorderState",
    "RecorderStats",
    "StreamRecorder",
    "ffmpeg_available",
    "ffmpeg_version",
    "resolve_ffmpeg",
    "merge_segments",
    "remux_to_mp4",
    "build_output_dir",
    "build_segment_path",
    "ensure_dir",
    "get_free_space_gb",
    "has_enough_space",
    "sanitize_name",
]
