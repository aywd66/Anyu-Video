# -*- coding: utf-8 -*-
"""录制层：ffmpeg 拉流录制、分片合并、存储与磁盘管理。"""

from .ffmpeg_recorder import RecorderState, RecorderStats, StreamRecorder
from .ffmpeg_utils import ffmpeg_available, ffmpeg_version, resolve_ffmpeg
from .merger import merge_segments, remux_to_mp4, verify_decodable
from .storage import (
    UNKNOWN_NICKNAME,
    SessionNaming,
    build_merged_path,
    build_output_dir,
    build_output_dir_from_naming,
    build_segment_path,
    ensure_dir,
    get_free_space_gb,
    has_enough_space,
    is_meaningful_name,
    make_session_naming,
    reserve_path,
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
    "verify_decodable",
    "UNKNOWN_NICKNAME",
    "SessionNaming",
    "build_merged_path",
    "build_output_dir",
    "build_output_dir_from_naming",
    "build_segment_path",
    "ensure_dir",
    "get_free_space_gb",
    "has_enough_space",
    "is_meaningful_name",
    "make_session_naming",
    "reserve_path",
    "sanitize_name",
]
