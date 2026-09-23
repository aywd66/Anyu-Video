# -*- coding: utf-8 -*-
"""
ffmpeg 调用工具

集中处理：
- ffmpeg / ffprobe 可执行文件定位与可用性检测
- 统一的子进程调用封装（含编码处理、超时、日志记录）
- 媒体时长探测（用于计算已录时长与进度）
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence, Tuple

from ..utils.logger import get_logger

logger = get_logger(__name__)

# Windows 下隐藏子进程控制台窗口
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _subprocess_flags() -> int:
    """返回创建子进程时需要的平台相关标志。"""
    return _CREATE_NO_WINDOW


def resolve_ffmpeg(path: str = "ffmpeg") -> Optional[str]:
    """定位可用的 ffmpeg 可执行文件。

    查找顺序：
    1. 配置中的显式路径（若存在且可执行）
    2. 同目录下的 ``ffmpeg.exe`` / ``bin/ffmpeg.exe``（便携部署场景）
    3. 系统 PATH

    返回可执行文件路径；找不到返回 None。
    """
    if path:
        candidate = Path(path)
        if candidate.is_file():
            return str(candidate)
        # 配置里只写了目录的情形
        if candidate.is_dir():
            for name in ("ffmpeg.exe", "ffmpeg"):
                inner = candidate / name
                if inner.is_file():
                    return str(inner)

    # 项目内置目录（把 ffmpeg 放进项目 bin/ 即可免配置）
    project_bin = Path(__file__).resolve().parent.parent.parent / "bin"
    for name in ("ffmpeg.exe", "ffmpeg"):
        candidate = project_bin / name
        if candidate.is_file():
            return str(candidate)

    # 系统 PATH
    found = shutil.which("ffmpeg")
    return found


def ffmpeg_available(path: str = "ffmpeg") -> bool:
    """检测 ffmpeg 是否可用（执行 -version）。"""
    exe = resolve_ffmpeg(path)
    if not exe:
        return False
    try:
        result = subprocess.run(
            [exe, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            creationflags=_subprocess_flags(),
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ffmpeg_version(path: str = "ffmpeg") -> str:
    """返回 ffmpeg 版本号字符串（用于 README/日志展示）。"""
    exe = resolve_ffmpeg(path)
    if not exe:
        return ""
    try:
        result = subprocess.run(
            [exe, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            creationflags=_subprocess_flags(),
        )
        first_line = (result.stdout or b"").decode("utf-8", "ignore").splitlines()
        return first_line[0] if first_line else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def run_ffmpeg(
    args: Sequence[str],
    ffmpeg_path: str = "ffmpeg",
    timeout: Optional[float] = None,
) -> Tuple[int, str]:
    """同步执行一条 ffmpeg 命令。

    参数
    ----
    args:        ffmpeg 参数列表（不含可执行文件本身）
    ffmpeg_path: ffmpeg 路径或命令名
    timeout:     超时秒数，None 表示不限

    返回 ``(returncode, 合并后的输出文本)``。
    """
    exe = resolve_ffmpeg(ffmpeg_path) or ffmpeg_path
    cmd = [exe, *args]
    logger.debug("执行 ffmpeg：%s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            creationflags=_subprocess_flags(),
        )
        output = (result.stdout or b"").decode("utf-8", "ignore")
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return -1, "ffmpeg 执行超时"
    except OSError as exc:
        return -1, f"ffmpeg 执行失败：{exc}"


def probe_duration(file_path: str | Path, ffmpeg_path: str = "ffmpeg") -> float:
    """用 ffprobe 探测媒体文件时长（秒），失败返回 0。

    ffprobe 通常与 ffmpeg 同目录，找不到时按 ffmpeg 的目录推测。
    """
    path = Path(file_path)
    if not path.exists():
        return 0.0

    exe = resolve_ffmpeg(ffmpeg_path) or "ffmpeg"
    # 由 ffmpeg 路径推导 ffprobe 路径
    directory = Path(exe).parent
    probe = None
    for name in ("ffprobe.exe", "ffprobe"):
        candidate = directory / name
        if candidate.is_file():
            probe = str(candidate)
            break
    if probe is None:
        probe = shutil.which("ffprobe")
    if probe is None:
        return 0.0

    try:
        result = subprocess.run(
            [
                probe,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            creationflags=_subprocess_flags(),
        )
        payload = json.loads((result.stdout or b"{}").decode("utf-8", "ignore"))
        return float(payload.get("format", {}).get("duration") or 0.0)
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return 0.0


def parse_ffmpeg_error(output: str) -> str:
    """从 ffmpeg 输出里提炼一条人类可读的错误摘要。"""
    if not output:
        return ""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    # 优先取包含关键字的那一行
    for keyword in ("error", "Error", "failed", "Failed", "Invalid", "No such"):
        for line in reversed(lines):
            if keyword in line:
                return line[:300]
    return lines[-1][:300] if lines else ""
