# -*- coding: utf-8 -*-
"""
ffmpeg 拉流录制核心

单个直播间的录制生命周期由 ``StreamRecorder`` 负责，工作流程如下：

    ┌─ 取流地址（每个分片都重新解析，避免平台播放地址过期）
    │
    ├─ 启动 ffmpeg（-c copy 直存，不转码）
    │      └─ 监控循环：停止信号 / 分卷触发 / 卡顿检测 / 磁盘保护
    │
    ├─ 分片结束 ─┬─ 正常停止 ────────────► 跳出，进入收尾
    │            ├─ 达到 4GB 或时长上限 ─► 序号+1，立即开新分片（不丢内容）
    │            ├─ 拉流中断 / 卡顿 ─────► 等待重连间隔，序号+1，开新分片
    │            └─ 磁盘不足 ────────────► 立即停止并上报，由调度层暂停全部任务
    │
    └─ 收尾：合并分片 ─►（可选）转封装 MP4 ─► 上报结束事件

所有对外通知都通过 ``on_event(event, payload)`` 回调，不直接依赖界面层，
便于单元测试与后续替换通知方式。
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..config.settings import AppConfig, RoomEntry
from ..platforms.base import StreamInfo
from ..platforms.registry import PLATFORM_LABELS
from ..utils.logger import get_logger
from .ffmpeg_utils import parse_ffmpeg_error, resolve_ffmpeg
from .merger import merge_segments, remux_to_mp4
from .storage import (
    build_merged_path,
    build_output_dir,
    build_segment_path,
    get_free_space_gb,
)

logger = get_logger(__name__)

# Windows 下隐藏 ffmpeg 控制台窗口
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# --------------------------------------------------------------------------
# 事件名（供调度层与界面层匹配）
# --------------------------------------------------------------------------
EVT_STARTED = "recorder_started"        # 开始一场录制
EVT_SEGMENT_START = "segment_started"   # 新分片开始
EVT_SEGMENT_DONE = "segment_finished"   # 分片结束
EVT_RECONNECT = "reconnecting"          # 触发重连
EVT_STOPPED = "recorder_stopped"        # 录制完全结束（含合并）
EVT_MERGE_START = "merge_started"
EVT_MERGE_DONE = "merge_finished"
EVT_ERROR = "recorder_error"
EVT_DISK_LOW = "disk_low"
EVT_FALLBACK = "quality_fallback"

# 小于该字节数的分片视作无效产物（会被清理，不进入合并清单）
_MIN_SEGMENT_BYTES = 1024
# 进程存活不足该秒数且无数据产出，判定为启动失败而非正常收流
_INSTANT_FAIL_SECONDS = 3.0


class RecorderState(str, Enum):
    """录制器状态。"""

    IDLE = "idle"
    STARTING = "starting"
    RECORDING = "recording"
    RECONNECTING = "reconnecting"
    MERGING = "merging"
    STOPPED = "stopped"
    ERROR = "error"


class _ExitReason(str, Enum):
    """单次 ffmpeg 进程退出的原因。"""

    NORMAL = "normal"        # 进程自然结束（通常为主播下播）
    STOPPED = "stopped"      # 用户/调度层要求停止
    ROTATE = "rotate"        # 达到分卷条件
    STALL = "stall"          # 卡顿 / 拉流中断
    DISK_LOW = "disk_low"    # 磁盘空间不足
    ERROR = "error"          # ffmpeg 异常退出（无法启动或参数错误）


@dataclass
class RecorderStats:
    """录制状态快照，供界面层定时刷新。"""

    state: RecorderState = RecorderState.IDLE
    nickname: str = ""
    platform: str = ""
    segment_count: int = 0
    total_bytes: int = 0
    recorded_seconds: float = 0.0
    current_file: str = ""
    output_dir: str = ""
    started_at: Optional[datetime] = None
    last_message: str = ""

    @property
    def total_mb(self) -> float:
        return self.total_bytes / 1024 ** 2


@dataclass
class _SegmentRecord:
    """一个已完成分片的记录。"""

    path: Path
    seconds: float
    size: int


class StreamRecorder:
    """单个直播间的录制器。

    参数
    ----
    room:            直播间条目（提供平台、房间号、昵称）
    stream_provider: 无参可调用对象，返回 ``StreamInfo``；**每个分片开始时都会重新调用**，
                     以规避平台播放地址过期的问题
    config:          全局配置
    on_event:        事件回调 ``(event_name: str, payload: dict) -> None``
    """

    def __init__(
        self,
        room: RoomEntry,
        stream_provider: Callable[[], StreamInfo],
        config: AppConfig,
        on_event: Optional[Callable[[str, Dict], None]] = None,
    ) -> None:
        self.room = room
        self._stream_provider = stream_provider
        self.config = config
        self._on_event = on_event

        # 线程与生命周期
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._proc_lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None

        # 录制产物
        self._session_start: datetime = datetime.now()
        self._output_dir: Path = Path(config.save_path)
        self._segments: List[_SegmentRecord] = []
        self._current_path: Optional[Path] = None
        self._segment_index = 0
        self._container = "flv"
        self._final_path: Optional[Path] = None

        # 统计与状态
        self._state = RecorderState.IDLE
        self._recorded_seconds = 0.0
        self._current_segment_start = 0.0
        self._reconnect_attempts = 0
        self._last_message = ""
        self._stop_reason = ""
        # 部分平台/老版本 ffmpeg 不接受网络重连参数，失败一次后自动降级为不带该参数的写法
        self._omit_network_options = False

    # ------------------------------------------------------------------ 属性
    @property
    def state(self) -> RecorderState:
        with self._lock:
            return self._state

    @property
    def is_active(self) -> bool:
        """是否仍处于录制流程中（含重连/合并）。"""
        return self._thread is not None and self._thread.is_alive()

    @property
    def final_path(self) -> Optional[Path]:
        with self._lock:
            return self._final_path

    def stats(self) -> RecorderStats:
        """返回当前状态快照（线程安全）。"""
        with self._lock:
            total = sum(seg.size for seg in self._segments)
            if self._current_path is not None:
                total += _file_size(self._current_path)
            recorded = self._recorded_seconds
            if self._state is RecorderState.RECORDING and self._current_segment_start:
                recorded += max(0.0, time.monotonic() - self._current_segment_start)
            return RecorderStats(
                state=self._state,
                nickname=self.room.nickname,
                platform=self.room.platform,
                segment_count=len(self._segments) + (1 if self._current_path else 0),
                total_bytes=total,
                recorded_seconds=recorded,
                current_file=self._current_path.name if self._current_path else "",
                output_dir=str(self._output_dir),
                started_at=self._session_start,
                last_message=self._last_message,
            )

    # ------------------------------------------------------------------ 控制
    def start(self) -> None:
        """启动录制（非阻塞，实际工作在后台线程）。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                logger.debug("录制器已在运行，忽略重复启动：%s", self.room.key)
                return
            self._stop_event.clear()
            self._stop_reason = ""
            self._reconnect_attempts = 0
            self._session_start = datetime.now()
            self._segments.clear()
            self._segment_index = 0
            self._recorded_seconds = 0.0
            self._final_path = None
            self._state = RecorderState.STARTING

        self._thread = threading.Thread(
            target=self._run, name=f"recorder-{self.room.key}", daemon=True
        )
        self._thread.start()

    def stop(self, reason: str = "manual") -> None:
        """请求停止录制。

        立即返回：实际的 ffmpeg 优雅退出与分片合并由后台线程完成，
        完成后会触发 ``EVT_STOPPED`` 事件，避免阻塞界面。
        """
        with self._lock:
            self._stop_reason = reason
        self._stop_event.set()
        # 立即打断正在进行的 ffmpeg 拉流，缩短收尾时间
        self._terminate_process(graceful=True)

    def join(self, timeout: Optional[float] = None) -> None:
        """等待录制线程完全结束（含合并）。"""
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    # ---------------------------------------------------------------- 主流程
    def _run(self) -> None:
        """录制主循环（在工作线程中执行）。"""
        try:
            self._prepare_output_dir()
            self._emit(EVT_STARTED, {"platform": self.room.platform})
            logger.info(
                "[开始录制] %s「%s」-> %s",
                self._platform_label(),
                self.room.nickname or self.room.room_id,
                self._output_dir,
            )
            self._record_loop()
        except Exception as exc:  # noqa: BLE001 - 兜底，防止线程静默死亡
            logger.exception("录制线程异常：%s", exc)
            with self._lock:
                self._state = RecorderState.ERROR
                self._last_message = str(exc)
            self._emit(EVT_ERROR, {"error": str(exc)})
        finally:
            self._finalize()

    def _prepare_output_dir(self) -> None:
        """创建「平台/主播昵称/日期」三级输出目录。"""
        self._output_dir = build_output_dir(
            self.config.save_path,
            self.room.platform,
            self.room.nickname or self.room.room_id,
            self._session_start,
        )

    def _record_loop(self) -> None:
        """分片循环：反复「取流 -> 录制 -> 判断退出原因」直到结束。"""
        while not self._stop_event.is_set():
            # 磁盘检查：不足则直接停止并上报
            free_gb = get_free_space_gb(self.config.save_path)
            if free_gb < self.config.min_free_disk_gb:
                logger.error(
                    "[磁盘不足] 剩余 %.2f GB，低于阈值 %.2f GB，停止录制：%s",
                    free_gb,
                    self.config.min_free_disk_gb,
                    self.room.nickname,
                )
                self._emit(
                    EVT_DISK_LOW,
                    {"free_gb": free_gb, "threshold_gb": self.config.min_free_disk_gb},
                )
                return

            # 1) 获取流地址（每个分片都重新解析，规避地址过期）
            try:
                stream = self._stream_provider()
            except Exception as exc:  # noqa: BLE001
                # 平台已明确告知「房间不可用 / 未开播」：继续重连没有意义
                # （重连多少次都不会有流），立即收尾。
                # 上层监控会把状态置为「未开播」，下一轮检测到开播会重新启动录制。
                if self._is_room_unavailable(exc):
                    logger.info(
                        "[结束] %s 平台确认房间不可用，停止重连：%s",
                        self.room.nickname or self.room.room_id,
                        exc,
                    )
                    with self._lock:
                        self._last_message = f"房间不可用：{exc}"
                    return
                self._reconnect_attempts += 1
                if self._exceeded_reconnect_limit():
                    self._fail(f"连续 {self._reconnect_attempts} 次无法获取流地址：{exc}")
                    return
                with self._lock:
                    self._state = RecorderState.RECONNECTING
                    self._last_message = f"取流失败，{self.config.reconnect_interval}s 后重试"
                logger.warning(
                    "[重连] %s 取流失败（第 %d 次）：%s",
                    self.room.nickname,
                    self._reconnect_attempts,
                    exc,
                )
                self._emit(EVT_RECONNECT, {"reason": str(exc)})
                if self._stop_event.wait(self.config.reconnect_interval):
                    return
                continue

            # 2) 记录降级事件（原画失败退超清时）
            if stream.is_fallback:
                self._emit(
                    EVT_FALLBACK,
                    {
                        "quality": stream.quality.value,
                        "quality_label": stream.quality_label,
                    },
                )

            # 3) 开一个新分片并录制
            self._container = stream.container
            reason = self._record_one_segment(stream)

            if reason in (_ExitReason.STOPPED, _ExitReason.DISK_LOW):
                return
            if reason is _ExitReason.ERROR:
                self._reconnect_attempts += 1
                if self._exceeded_reconnect_limit():
                    self._fail("ffmpeg 连续异常退出，已达最大重连次数")
                    return
                if self._stop_event.wait(self.config.reconnect_interval):
                    return
                continue
            if reason is _ExitReason.NORMAL:
                # 进程自然结束通常意味着主播下播；等待一个间隔后由调度层再确认
                logger.info(
                    "[流结束] %s 拉流自然结束，%d 秒后再次确认状态",
                    self.room.nickname,
                    self.config.reconnect_interval,
                )
                if self._stop_event.wait(self.config.reconnect_interval):
                    return
                continue
            if reason is _ExitReason.ROTATE:
                # 达到分卷条件，立即开新分片，不等待
                logger.info("[分卷] %s 触发分卷，开始新分片", self.room.nickname)
                continue

            # 4) 卡顿/中断：按重连间隔等待后重试
            self._reconnect_attempts += 1
            if self._exceeded_reconnect_limit():
                self._fail(f"连续 {self._reconnect_attempts} 次重连失败，停止录制")
                return
            with self._lock:
                self._state = RecorderState.RECONNECTING
                self._last_message = f"拉流中断，{self.config.reconnect_interval}s 后重连"
            logger.warning(
                "[重连] %s 拉流中断（第 %d 次），%d 秒后重连",
                self.room.nickname,
                self._reconnect_attempts,
                self.config.reconnect_interval,
            )
            self._emit(
                EVT_RECONNECT,
                {"attempt": self._reconnect_attempts, "wait": self.config.reconnect_interval},
            )
            if self._stop_event.wait(self.config.reconnect_interval):
                return

    # ------------------------------------------------------------ 单分片录制
    def _record_one_segment(self, stream: StreamInfo) -> _ExitReason:
        """启动 ffmpeg 录制一个分片，返回该分片的退出原因。"""
        self._segment_index += 1
        out_path = build_segment_path(
            self._output_dir,
            self.room.platform,
            self.room.nickname or self.room.room_id,
            self._session_start,
            self._segment_index,
            self._container,
        )
        with self._lock:
            self._current_path = out_path
            self._state = RecorderState.RECORDING
            self._current_segment_start = time.monotonic()
            self._last_message = "录制中"

        cmd = self._build_ffmpeg_command(stream, out_path)
        logger.info(
            "[分片 %03d] %s -> %s",
            self._segment_index,
            self.room.nickname,
            out_path.name,
        )
        self._emit(
            EVT_SEGMENT_START,
            {"index": self._segment_index, "file": out_path.name},
        )

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,   # 由后台线程持续排空，避免管道写满导致 ffmpeg 阻塞
                creationflags=_CREATE_NO_WINDOW,
            )
        except OSError as exc:
            logger.error("启动 ffmpeg 失败：%s", exc)
            self._finalize_segment(out_path, failed=True)
            return _ExitReason.ERROR

        with self._proc_lock:
            self._proc = proc

        # 排空 stderr，保留最后若干行用于错误诊断
        error_lines: List[str] = []
        drain = threading.Thread(
            target=_drain_stream,
            args=(proc.stderr, error_lines),
            name=f"ffmpeg-stderr-{self.room.key}",
            daemon=True,
        )
        drain.start()

        reason = self._monitor_process(proc, out_path)

        with self._proc_lock:
            self._proc = None
        drain.join(timeout=2.0)

        # 启动即失败（存活时间短、无数据产出、返回码非 0）判定为参数/环境错误
        if reason is _ExitReason.NORMAL:
            duration = time.monotonic() - self._current_segment_start
            if proc.returncode not in (0, None) and _file_size(out_path) < _MIN_SEGMENT_BYTES:
                summary = parse_ffmpeg_error("\n".join(error_lines))
                logger.error(
                    "[异常退出] %s ffmpeg 返回码 %s：%s",
                    self.room.nickname,
                    proc.returncode,
                    summary or "无输出",
                )
                # 首次遇到参数类错误时，去掉网络重连参数再试一次（兼容性兜底）
                if not self._omit_network_options and _looks_like_option_error(summary):
                    self._omit_network_options = True
                    logger.warning(
                        "检测到 ffmpeg 不接受网络参数，后续将改用精简参数重试"
                    )
                reason = _ExitReason.ERROR
            elif duration < _INSTANT_FAIL_SECONDS and _file_size(out_path) < _MIN_SEGMENT_BYTES:
                summary = parse_ffmpeg_error("\n".join(error_lines))
                logger.warning(
                    "[异常退出] %s 首次分片立即结束：%s",
                    self.room.nickname,
                    summary or "无输出",
                )
                reason = _ExitReason.ERROR

        self._finalize_segment(out_path, failed=reason is _ExitReason.ERROR)

        # 正常退出/分卷成功时重置连续失败计数
        if reason in (_ExitReason.ROTATE, _ExitReason.NORMAL):
            self._reconnect_attempts = 0
        return reason

    def _build_ffmpeg_command(self, stream: StreamInfo, out_path: Path) -> List[str]:
        """构造 ffmpeg 拉流录制命令（``-c copy``，全程不转码）。"""
        exe = resolve_ffmpeg(self.config.ffmpeg_path) or self.config.ffmpeg_path
        headers = dict(stream.headers or {})

        cmd: List[str] = [exe, "-hide_banner", "-loglevel", "warning", "-nostats"]

        user_agent = headers.pop("User-Agent", None)
        if user_agent:
            cmd += ["-user_agent", user_agent]

        # 其余请求头统一通过 -headers 传入，ffmpeg 要求以 \r\n 分隔
        if headers:
            header_text = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            cmd += ["-headers", header_text]

        # 网络读取超时与自动重连（HTTP/HTTPS 协议选项，必须出现在 -i 之前）
        if not self._omit_network_options:
            stall_seconds = max(5, int(self.config.stall_timeout))
            cmd += [
                "-rw_timeout", str(stall_seconds * 1_000_000),   # 微秒
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", str(max(1, int(self.config.reconnect_interval))),
            ]

        cmd += ["-i", stream.url]

        # 直接复制码流，不重编码；高码率场景适当放大复用队列
        cmd += [
            "-c", "copy",
            "-max_muxing_queue_size", "2048",
            "-f", "mpegts" if stream.stream_format.value == "hls" else "flv",
            "-y",
            str(out_path),
        ]
        return cmd

    def _monitor_process(self, proc: subprocess.Popen, out_path: Path) -> _ExitReason:
        """监控 ffmpeg 进程，按需触发停止 / 分卷 / 重连 / 磁盘保护。"""
        last_size = -1
        last_growth = time.monotonic()
        max_bytes = self.config.max_file_size_bytes
        max_seconds = self.config.max_duration_seconds
        stall_timeout = max(8, int(self.config.stall_timeout))

        while True:
            # 进程已退出
            if proc.poll() is not None:
                return _ExitReason.NORMAL

            # 收到停止信号
            if self._stop_event.is_set():
                self._terminate_process(graceful=True)
                return _ExitReason.STOPPED

            size = _file_size(out_path)
            now = time.monotonic()

            # 分卷判断：单分片大小或时长达到上限
            if max_bytes and size >= max_bytes:
                logger.info(
                    "[分卷] %s 分片达到 %.2f GB，触发分卷",
                    self.room.nickname,
                    size / 1024 ** 3,
                )
                self._terminate_process(graceful=True)
                return _ExitReason.ROTATE
            if max_seconds and (now - self._current_segment_start) >= max_seconds:
                logger.info(
                    "[分卷] %s 分片达到 %d 分钟上限，触发分卷",
                    self.room.nickname,
                    self.config.max_duration_minutes,
                )
                self._terminate_process(graceful=True)
                return _ExitReason.ROTATE

            # 卡顿判断：输出文件长时间不增长
            if size > last_size:
                last_size = size
                last_growth = now
            elif now - last_growth > stall_timeout:
                logger.warning(
                    "[卡顿] %s 输出已 %.0f 秒无增长，判定拉流异常，触发重连",
                    self.room.nickname,
                    now - last_growth,
                )
                self._terminate_process(graceful=False)
                return _ExitReason.STALL

            # 磁盘保护：录制过程中也要检查
            free_gb = get_free_space_gb(self.config.save_path)
            if free_gb < self.config.min_free_disk_gb:
                logger.error(
                    "[磁盘不足] %s 录制中检测到空间不足（%.2f GB），立即停止",
                    self.room.nickname,
                    free_gb,
                )
                self._terminate_process(graceful=True)
                self._emit(
                    EVT_DISK_LOW,
                    {"free_gb": free_gb, "threshold_gb": self.config.min_free_disk_gb},
                )
                return _ExitReason.DISK_LOW

            time.sleep(1.0)

    def _terminate_process(self, graceful: bool = True) -> None:
        """终止当前 ffmpeg 进程。

        graceful=True 时先向 stdin 写入 'q'，让 ffmpeg 写完文件尾部再退出，
        保证分片文件索引完整、可被 concat 合并。
        """
        with self._proc_lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return

        if graceful:
            try:
                if proc.stdin is not None:
                    proc.stdin.write(b"q")
                    proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(timeout=10)
                return
            except subprocess.TimeoutExpired:
                logger.debug("ffmpeg 未响应优雅退出，转为强制终止")

        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
        except OSError:
            pass

    # ------------------------------------------------------------ 分片收尾
    def _finalize_segment(self, out_path: Path, failed: bool = False) -> None:
        """记录分片结果：累加时长与体积，清理空文件。"""
        seconds = time.monotonic() - self._current_segment_start
        size = _file_size(out_path)
        with self._lock:
            self._current_path = None
            self._current_segment_start = 0.0

        if size < _MIN_SEGMENT_BYTES:
            # 无效分片（多为进程启动即失败），删除以免污染合并清单
            try:
                if out_path.exists():
                    out_path.unlink()
            except OSError:
                pass
            if failed:
                logger.warning("[分片失败] %s 未产生有效数据", out_path.name)
            return

        with self._lock:
            self._segments.append(_SegmentRecord(path=out_path, seconds=seconds, size=size))
            self._recorded_seconds += seconds
        self._emit(
            EVT_SEGMENT_DONE,
            {"index": self._segment_index, "file": out_path.name, "size": size},
        )
        logger.info(
            "[分片完成] %s（%.1f MB / 约 %.0f 秒）",
            out_path.name,
            size / 1024 ** 2,
            seconds,
        )

    # ------------------------------------------------------------ 整体收尾
    def _finalize(self) -> None:
        """录制结束后的收尾：合并分片、（可选）转封装 MP4。"""
        segments = [seg.path for seg in self._segments]
        final_path: Optional[Path] = None

        if not segments:
            logger.warning("[收尾] %s 本场未产生有效分片，跳过合并", self.room.nickname)
        else:
            with self._lock:
                self._state = RecorderState.MERGING
                self._last_message = "正在合并分片"
            self._emit(EVT_MERGE_START, {"count": len(segments)})
            merged_target = build_merged_path(
                self._output_dir,
                self.room.platform,
                self.room.nickname or self.room.room_id,
                self._session_start,
                self._container,
            )
            final_path = merge_segments(
                segments,
                merged_target,
                ffmpeg_path=self.config.ffmpeg_path,
                container=self._container,
            )
            if final_path is not None:
                self._emit(
                    EVT_MERGE_DONE,
                    {"file": str(final_path), "count": len(segments)},
                )
                # 合并产生了新文件时，清理已并入的原始分片（避免磁盘占用翻倍）
                if len(segments) > 1 and final_path != segments[0]:
                    self._cleanup_merged_segments(segments)

                # 可选：转封装为 MP4（-c copy 不重编码）
                if self.config.convert_to_mp4:
                    mp4_path = remux_to_mp4(
                        final_path, ffmpeg_path=self.config.ffmpeg_path
                    )
                    if mp4_path is not None:
                        final_path = mp4_path

        with self._lock:
            self._final_path = final_path
            self._state = (
                RecorderState.STOPPED
                if (final_path is not None or not segments)
                else RecorderState.ERROR
            )
            self._last_message = (
                f"已保存：{final_path.name}" if final_path else "录制结束（无有效文件）"
            )

        self._emit(
            EVT_STOPPED,
            {
                "reason": self._stop_reason or "end",
                "file": str(final_path) if final_path else "",
                "segments": len(segments),
            },
        )
        logger.info(
            "[录制结束] %s「%s」%s",
            self._platform_label(),
            self.room.nickname or self.room.room_id,
            f"成品：{final_path.name}" if final_path else "未生成文件",
        )

    def _cleanup_merged_segments(self, segments: List[Path]) -> None:
        """合并成功后删除原始分片，避免磁盘占用翻倍。"""
        try:
            for segment in segments:
                if segment.exists():
                    segment.unlink()
            logger.debug("已清理 %d 个原始分片", len(segments))
        except OSError as exc:
            logger.warning("清理原始分片失败（不影响成品文件）：%s", exc)

    # ---------------------------------------------------------------- 辅助
    def _exceeded_reconnect_limit(self) -> bool:
        """是否达到最大重连次数限制（0 表示不限）。"""
        limit = self.config.max_reconnect_attempts
        return bool(limit) and self._reconnect_attempts >= limit

    @staticmethod
    def _is_room_unavailable(exc: BaseException) -> bool:
        """判断异常是否表示「平台已确认房间不可用 / 未开播」。

        这类情况重连没有意义，应立即收尾而不是无限重试。
        采用惰性导入，保持「录制层不依赖平台层」的分层约定。
        """
        try:
            from ..platforms.base import RoomUnavailableError
        except ImportError:  # 极端情况下平台层不可导入时不影响录制
            return False
        return isinstance(exc, RoomUnavailableError)

    def _fail(self, message: str) -> None:
        """以错误状态结束录制。"""
        logger.error("[录制失败] %s：%s", self.room.nickname, message)
        with self._lock:
            self._state = RecorderState.ERROR
            self._last_message = message
        self._emit(EVT_ERROR, {"error": message})

    def _platform_label(self) -> str:
        return PLATFORM_LABELS.get(self.room.platform, self.room.platform)

    def _emit(self, event: str, payload: Optional[Dict] = None) -> None:
        """安全地触发事件回调。"""
        if self._on_event is None:
            return
        data = dict(payload or {})
        data.setdefault("room_key", self.room.key)
        data.setdefault("nickname", self.room.nickname)
        try:
            self._on_event(event, data)
        except Exception as exc:  # noqa: BLE001 - 回调异常不应影响录制
            logger.debug("事件回调异常：%s", exc)


def _file_size(path: Optional[Path]) -> int:
    """安全地读取文件大小，不存在返回 0。"""
    if path is None:
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _drain_stream(stream, sink: List[str], keep: int = 60) -> None:
    """持续读取子进程输出，避免管道写满导致子进程阻塞。

    只保留最后 ``keep`` 行，用于失败时提取错误摘要。
    """
    if stream is None:
        return
    try:
        for raw in iter(stream.readline, b""):
            try:
                line = raw.decode("utf-8", "ignore").rstrip()
            except Exception:  # noqa: BLE001
                continue
            sink.append(line)
            if len(sink) > keep:
                del sink[0 : len(sink) - keep]
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass


def _looks_like_option_error(message: str) -> bool:
    """判断 ffmpeg 报错是否属于「参数不被识别」这类兼容性问题。"""
    if not message:
        return False
    lowered = message.lower()
    return (
        "option not found" in lowered
        or "unrecognized option" in lowered
        or "option rw_timeout not found" in lowered
        or "failed to set value" in lowered
    )
