# -*- coding: utf-8 -*-
"""
监控调度引擎

职责
----
1. 维护全部监控对象的运行时状态（``RoomRuntime``）；
2. 按可配置间隔轮询各直播间开播状态（并发检测 + 请求节流）；
3. 检测到开播自动创建 ``StreamRecorder`` 启动录制，检测到下播自动停止并收尾；
4. 全局磁盘保护：剩余空间不足时暂停全部录制并告警，空间恢复后自动继续；
5. 通过 ``EventBus`` 把状态变化与各类事件推送给界面层。

线程模型
--------
- 调度线程：唯一，周期性执行「检测 -> 决策 -> 派遣」；
- 检测线程池：并发执行网络检测（并发数可配置，默认 6，避免风控）；
- 录制线程：每个正在录制的房间一条（由 ``StreamRecorder`` 内部管理）。

引擎自身不依赖任何 GUI 组件，可独立运行与测试。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from ..config.settings import AppConfig, ConfigStore, RoomEntry
from ..platforms.base import LiveStatus, PlatformError, RoomInfo, StreamInfo
from ..platforms.registry import (
    PLATFORM_LABELS,
    close_all,
    detect_platform,
    get_parser,
    resolve_stream,
)
from ..recorder import StreamRecorder
from ..recorder.ffmpeg_recorder import RecorderState
from ..recorder.storage import get_free_space_gb
from ..utils.logger import get_logger
from .events import ENGINE_EVENTS, EventBus

logger = get_logger(__name__)

# 状态 -> 中文标签
STATUS_LABELS: Dict[str, str] = {
    "pending": "待解析",
    "offline": "未开播",
    "live": "已开播",
    "recording": "录制中",
    "reconnecting": "重连中",
    "merging": "合并中",
    "error": "异常",
    "disabled": "已停用",
    "paused": "已暂停",
}

# 磁盘恢复时需要的额外余量（GB），避免在阈值附近反复启停（抖动）
_DISK_HYSTERESIS_GB = 2.0


@dataclass
class RoomRuntime:
    """单个监控对象的运行时状态。"""

    entry: RoomEntry
    status: str = "pending"
    nickname: str = ""
    title: str = ""
    online_count: int = 0
    last_check: Optional[datetime] = None
    last_error: str = ""
    # 状态说明：平台层给出的「为什么没录上」，例如
    # 「房间不可观看：该内容暂时无法查看」「未开播（浏览器未捕获到直播流）」
    note: str = ""
    recorder: Optional[StreamRecorder] = None
    recording_since: Optional[datetime] = None
    last_final_path: str = ""
    ever_recorded: bool = False

    @property
    def key(self) -> str:
        return self.entry.key

    @property
    def display_nickname(self) -> str:
        """用于界面展示的昵称（未解析到之前用房间号占位）。"""
        return self.nickname or self.entry.nickname or self.entry.room_id or self.entry.raw_input

    @property
    def resolved_nickname(self) -> str:
        """已解析到的**真实**昵称（未识别时为空串，不回退到房间号）。

        与 :attr:`display_nickname` 的区别：后者为界面可读性会把房间号当
        占位名，而命名逻辑必须能区分「真的昵称」和「只是占位」——
        否则房间号会被写进文件名里的「昵称」分量。
        """
        return self.nickname or self.entry.nickname or ""

    def sync_nickname_to_entry(self) -> None:
        """把运行时昵称回写进持久化条目（供录制器冻结命名前调用）。"""
        if self.nickname and not self.entry.nickname:
            self.entry.nickname = self.nickname

    def is_recording(self) -> bool:
        return self.recorder is not None and self.recorder.is_active


class MonitorEngine:
    """监控调度引擎。

    参数
    ----
    store: 配置存储器（提供设置与监控清单的持久化）
    bus:   事件总线
    """

    def __init__(self, store: ConfigStore, bus: Optional[EventBus] = None) -> None:
        self.store = store
        self.bus = bus or EventBus()

        self._lock = threading.RLock()
        self._runtimes: Dict[str, RoomRuntime] = {}
        self._scheduler: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()

        # 磁盘保护状态
        self._disk_paused = False
        self._disk_pause_reason = ""

        # 从配置恢复监控清单
        self._rebuild_runtimes()

    # ------------------------------------------------------------------ 属性
    @property
    def config(self) -> AppConfig:
        return self.store.config

    @property
    def is_running(self) -> bool:
        return self._scheduler is not None and self._scheduler.is_alive()

    @property
    def disk_paused(self) -> bool:
        with self._lock:
            return self._disk_paused

    # ---------------------------------------------------------------- 生命周期
    def _rebuild_runtimes(self) -> None:
        """根据配置中的房间清单重建运行时对象。"""
        with self._lock:
            self._runtimes.clear()
            for entry in self.store.rooms:
                runtime = RoomRuntime(entry=entry, nickname=entry.nickname)
                if not entry.enabled:
                    runtime.status = "disabled"
                self._runtimes[runtime.key] = runtime

    def start(self) -> None:
        """开始监控（幂等）。"""
        with self._lock:
            if self.is_running:
                logger.info("监控已在运行中")
                return
            self._stop_event.clear()
            self._scheduler = threading.Thread(
                target=self._schedule_loop, name="monitor-scheduler", daemon=True
            )
            self._scheduler.start()

        # 记录监控状态，供重启后自动恢复
        self.config.auto_start_monitor = True
        self.store.save()
        logger.info(
            "监控已启动：%d 个对象，轮询间隔 %d 秒",
            len(self.store.rooms),
            self.config.poll_interval,
        )
        self.bus.publish(ENGINE_EVENTS.MONITOR_STARTED, {"rooms": len(self.store.rooms)})

    def stop(self, wait_seconds: float = 6.0) -> None:
        """停止监控，并停止所有正在进行的录制（触发合并收尾）。"""
        with self._lock:
            if not self.is_running:
                return
            self._stop_event.set()
            self._wake_event.set()
            running = [
                runtime for runtime in self._runtimes.values() if runtime.is_recording()
            ]

        scheduler = self._scheduler
        if scheduler is not None:
            scheduler.join(timeout=wait_seconds)

        # 停掉所有录制任务
        for runtime in running:
            recorder = runtime.recorder
            if recorder is not None:
                logger.info("停止录制：%s", runtime.display_nickname)
                recorder.stop(reason="monitor_stopped")
                runtime.status = "offline"

        with self._lock:
            self._scheduler = None

        self.config.auto_start_monitor = False
        self.store.save()
        logger.info("监控已停止")
        self.bus.publish(ENGINE_EVENTS.MONITOR_STOPPED, {})

    def shutdown(self) -> None:
        """程序退出前的清理：停止监控并释放网络连接。"""
        try:
            self.stop(wait_seconds=3.0)
        finally:
            # 等待录制线程收尾（合并可能耗时，最多等 60 秒）
            with self._lock:
                recorders = [
                    runtime.recorder
                    for runtime in self._runtimes.values()
                    if runtime.recorder is not None
                ]
            for recorder in recorders:
                recorder.join(timeout=60)
            close_all()

    # ------------------------------------------------------------------ 房间
    def add_room(self, entry: RoomEntry) -> bool:
        """新增监控对象。

        若未指定平台，或 room_id 尚未解析，会在下一次轮询时自动完成解析，
        因此本方法不阻塞界面。
        """
        with self._lock:
            limit = self.config.max_monitor_rooms
            if len(self._runtimes) >= limit:
                logger.warning("监控对象已达上限 %d，无法继续添加", limit)
                return False
            # 已存在的按 room_id 或原始输入去重
            for runtime in self._runtimes.values():
                existed = runtime.entry
                if entry.room_id and existed.room_id == entry.room_id and existed.platform == entry.platform:
                    logger.info("监控对象已存在：%s", entry.raw_input)
                    return False
                if existed.raw_input == entry.raw_input and entry.raw_input:
                    logger.info("监控对象已存在：%s", entry.raw_input)
                    return False

            runtime = RoomRuntime(entry=entry, nickname=entry.nickname)
            if not entry.enabled:
                runtime.status = "disabled"
            self._runtimes[runtime.key] = runtime
            self.store.rooms.append(entry)
            self.store.save()

        logger.info("已添加监控对象：%s %s", entry.platform or "待识别", entry.raw_input)
        self.bus.publish(ENGINE_EVENTS.ROOM_ADDED, {"key": runtime.key})
        self._wake_event.set()   # 立即触发一次检测，无需等下一个轮询周期
        return True

    def remove_room(self, key: str) -> bool:
        """删除监控对象（若正在录制会先停止并收尾）。"""
        with self._lock:
            runtime = self._runtimes.pop(key, None)
            if runtime is None:
                return False
            self.store.remove_room(key)
            self.store.save()

        if runtime.recorder is not None and runtime.recorder.is_active:
            logger.info("删除监控对象，先停止正在进行的录制：%s", runtime.display_nickname)
            runtime.recorder.stop(reason="room_removed")
        logger.info("已删除监控对象：%s", runtime.display_nickname)
        self.bus.publish(ENGINE_EVENTS.ROOM_REMOVED, {"key": key})
        return True

    def set_room_enabled(self, key: str, enabled: bool) -> bool:
        """启用/停用某个监控对象。"""
        with self._lock:
            runtime = self._runtimes.get(key)
            if runtime is None:
                return False
            runtime.entry.enabled = enabled
            if not enabled:
                runtime.status = "disabled"
                if runtime.recorder is not None and runtime.recorder.is_active:
                    runtime.recorder.stop(reason="disabled")
            else:
                runtime.status = "pending"
            self.store.save()
        self._wake_event.set()
        return True

    def room_runtime(self, key: str) -> Optional[RoomRuntime]:
        """获取指定房间的运行时对象。"""
        with self._lock:
            return self._runtimes.get(key)

    def snapshot(self) -> List[Dict]:
        """返回全部监控对象的状态快照（供界面表格刷新）。"""
        with self._lock:
            runtimes = list(self._runtimes.values())
            disk_paused = self._disk_paused

        rows: List[Dict] = []
        for runtime in runtimes:
            stats = runtime.recorder.stats() if runtime.recorder is not None else None

            # 状态优先级：录制器实时状态 > 调度层判定的房间状态
            status = runtime.status
            if stats is not None:
                if stats.state is RecorderState.RECORDING:
                    status = "recording"
                elif stats.state is RecorderState.RECONNECTING:
                    status = "reconnecting"
                elif stats.state is RecorderState.MERGING:
                    status = "merging"
                elif stats.state is RecorderState.ERROR:
                    status = "error"
            if disk_paused and status in ("recording", "reconnecting"):
                status = "paused"

            rows.append(
                {
                    "key": runtime.key,
                    "platform": runtime.entry.platform,
                    "platform_label": PLATFORM_LABELS.get(
                        runtime.entry.platform, runtime.entry.platform or "待识别"
                    ),
                    "room_id": runtime.entry.room_id,
                    "raw_input": runtime.entry.raw_input,
                    "nickname": runtime.display_nickname,
                    "enabled": runtime.entry.enabled,
                    "status": status,
                    "status_label": STATUS_LABELS.get(status, status),
                    "title": runtime.title,
                    "online_count": runtime.online_count,
                    "recorded_seconds": stats.recorded_seconds if stats else 0.0,
                    "total_bytes": stats.total_bytes if stats else 0,
                    "segment_count": stats.segment_count if stats else 0,
                    "output_dir": stats.output_dir if stats else self.config.save_path,
                    "current_file": stats.current_file if stats else "",
                    "last_check": runtime.last_check,
                    "last_error": runtime.last_error,
                    "note": runtime.note,
                    "final_path": runtime.last_final_path,
                }
            )
        return rows

    # ---------------------------------------------------------------- 调度循环
    def _schedule_loop(self) -> None:
        """调度线程主循环：检测 -> 决策 -> 派遣 -> 等待。"""
        # 首次进入先做一次磁盘检查
        self._check_disk_guard()

        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 - 调度线程必须存活
                logger.exception("调度循环异常：%s", exc)

            # 等待下一个轮询周期；_wake_event 可被「新增房间」等操作提前唤醒
            interval = max(5, int(self.config.poll_interval))
            self._wake_event.wait(timeout=interval)
            self._wake_event.clear()

    def _tick(self) -> None:
        """执行一轮「检测 + 决策」。"""
        # 磁盘保护优先：不足时暂停整轮检测的录制动作
        self._check_disk_guard()

        with self._lock:
            targets = [
                runtime
                for runtime in self._runtimes.values()
                if runtime.entry.enabled
            ]

        if not targets:
            return

        concurrency = max(1, min(self.config.check_concurrency, len(targets)))

        # 注意：首次检测会把分享链接解析为 room_id，届时 runtime.key 会变化，
        # 因此这里用 runtime 对象本身配对结果，而不是用 key，避免结果错配。
        #
        # 2026-11-13 错峰提交：原先所有房间一次性 pool.submit，并发 6 路
        # 同一瞬间打到快手 —— 实测全员触发「请求过快」风控（本机 Cookie
        # 正常也拦）。改为按序号隔开 spread 秒再检测，把一轮的请求摊平：
        # spread = 轮询间隔 × 60% ÷ 房间数（夹在 2~30 秒），保证一轮在
        # 下一个轮询周期之前完成。
        import time as _time
        spread = max(2.0, min(30.0, (self.config.poll_interval * 0.6) / max(1, len(targets))))

        def _staggered(rt, delay):
            if delay > 0:
                _time.sleep(delay)
            return self._check_one(rt)

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            pairs = [
                (runtime, pool.submit(_staggered, runtime, idx * spread))
                for idx, runtime in enumerate(targets)
            ]
            for runtime, future in pairs:
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("检测任务异常（%s）：%s", runtime.key, exc)
                    result = None
                # 统一在调度线程中应用结果，避免并发修改运行时状态
                self._apply_check_result(runtime, result)

    def _check_one(self, runtime: RoomRuntime) -> Optional[RoomInfo]:
        """检测单个直播间（在工作线程池中执行）。

        首次检测会先完成「平台识别 + 房间号解析」，并把结果回写配置。
        """
        entry = runtime.entry

        # 1) 平台识别
        if not entry.platform:
            platform = detect_platform(entry.raw_input)
            if not platform:
                runtime.status = "error"
                runtime.last_error = "无法识别平台，请在添加时手动指定"
                runtime.last_check = datetime.now()
                return None
            entry.platform = platform
            logger.info("自动识别平台：%s -> %s", entry.raw_input, PLATFORM_LABELS.get(platform, platform))

        parser = get_parser(entry.platform, self.config)

        # 2) 房间号解析（分享短链需要跟随重定向）
        if not entry.room_id:
            try:
                room_id = parser.resolve_room(entry.raw_input)
            except PlatformError as exc:
                runtime.status = "error"
                runtime.last_error = f"解析失败：{exc}"
                runtime.last_check = datetime.now()
                logger.warning("解析房间号失败（%s）：%s", entry.raw_input, exc)
                return None

            with self._lock:
                # ⚠️ old_key 必须在 entry.room_id 赋值**之前**取：key 属性实时由
                # entry 派生（room_id 有值时 key=platform:room_id），赋值后再取
                # 拿到的是新 key，pop 变成空操作，旧槽位（platform:raw_input）
                # 永远留在字典里 —— 同一房间挂两个 key：快照重复行、且按显示
                # key 停用/删除会 404「监控清单里没有这一项」（2026-11-13 实测：
                # 快手分享短链添加的房间解析后无法从网页停用）。
                old_key = runtime.key
                entry.room_id = room_id
                self._runtimes.pop(old_key, None)
                self._runtimes[runtime.key] = runtime
            self.store.save()
            logger.info(
                "解析成功：%s -> %s（房间 %s）",
                entry.raw_input,
                PLATFORM_LABELS.get(entry.platform, entry.platform),
                room_id,
            )
            self.bus.publish(
                ENGINE_EVENTS.ROOM_RESOLVED,
                {"key": runtime.key, "platform": entry.platform, "room_id": room_id},
            )

        # 3) 检测开播状态
        try:
            info = parser.check_live(entry.room_id)
        except Exception as exc:  # noqa: BLE001 - 单房间异常不影响整轮
            runtime.last_check = datetime.now()
            runtime.last_error = str(exc)
            logger.debug("检测异常（%s）：%s", runtime.display_nickname, exc)
            return None

        runtime.last_check = datetime.now()
        runtime.last_error = "" if info.status is not LiveStatus.UNKNOWN else runtime.last_error
        return info

    def _apply_check_result(self, runtime: RoomRuntime, info: Optional[RoomInfo]) -> None:
        """根据检测结果决定是否启停录制。"""
        if info is None:
            return

        # 昵称与标题回填
        if info.nickname and info.nickname != runtime.nickname:
            runtime.nickname = info.nickname
            runtime.entry.nickname = info.nickname
            self.store.save()
            self.bus.publish(
                ENGINE_EVENTS.ROOM_UPDATED,
                {"key": runtime.key, "nickname": info.nickname},
            )
        runtime.title = info.title or runtime.title
        runtime.online_count = info.online_count
        # 记录平台层给出的原因说明（未开播的具体缘由、风控、接口失效等），
        # 便于界面与日志直接回答「为什么没录上」。
        runtime.note = info.note or ""

        # 无法判定（网络异常/风控）：保持现状，避免误判为下播而中断录制
        if info.status is LiveStatus.UNKNOWN:
            if not runtime.is_recording():
                runtime.status = "error"
                runtime.last_error = info.note or "状态未知（网络异常或触发风控）"
            logger.debug("状态未知，保持现状：%s", runtime.display_nickname)
            return

        if info.status is LiveStatus.LIVE:
            if runtime.is_recording():
                runtime.status = "recording"
                return
            if self._disk_paused:
                runtime.status = "paused"
                return
            self._start_recording(runtime)
            return

        # 下播
        if runtime.is_recording():
            logger.info(
                "[下播] %s 已结束直播，停止录制并开始合并",
                runtime.display_nickname,
            )
            recorder = runtime.recorder
            if recorder is not None:
                recorder.stop(reason="live_ended")
            runtime.status = "offline"
            return
        runtime.status = "offline"

    # ---------------------------------------------------------------- 录制启停
    def _start_recording(self, runtime: RoomRuntime) -> None:
        """为指定房间创建并启动录制器。"""
        # 并发录制数限制，保护带宽与磁盘
        with self._lock:
            active = sum(
                1 for item in self._runtimes.values() if item.is_recording()
            )
        if active >= self.config.max_concurrent_recordings:
            runtime.status = "live"
            runtime.last_error = (
                f"已达同时录制上限（{self.config.max_concurrent_recordings}），排队等待"
            )
            logger.warning(
                "[排队] %s 已达同时录制上限，等待下一轮",
                runtime.display_nickname,
            )
            return

        entry = runtime.entry
        # 启动录制前，确保「运行时解析到的昵称」已落到 entry ——
        # 录制器会在 start() 时冻结命名快照，此刻的 entry.nickname
        # 就是本场文件名的唯一依据。
        runtime.sync_nickname_to_entry()

        def stream_provider() -> StreamInfo:
            """每次取流都重新解析地址，规避平台播放地址过期。"""
            parser = get_parser(entry.platform, self.config)
            return resolve_stream(parser, entry.room_id, self.config)

        recorder = StreamRecorder(
            room=entry,
            stream_provider=stream_provider,
            config=self.config,
            on_event=lambda event, payload: self._on_recorder_event(runtime, event, payload),
        )
        runtime.recorder = recorder
        runtime.recording_since = datetime.now()
        runtime.ever_recorded = True
        runtime.status = "recording"
        recorder.start()
        self.bus.publish(
            ENGINE_EVENTS.RECORDING_STARTED,
            {"key": runtime.key, "nickname": runtime.display_nickname},
        )

    def _on_recorder_event(self, runtime: RoomRuntime, event: str, payload: Dict) -> None:
        """把录制器事件转译为引擎级事件并推送给界面层。"""
        from ..recorder import ffmpeg_recorder as rec

        if event == rec.EVT_FALLBACK:
            self.bus.publish(
                ENGINE_EVENTS.QUALITY_FALLBACK,
                {
                    "key": runtime.key,
                    "nickname": runtime.display_nickname,
                    "quality": payload.get("quality", ""),
                    "quality_label": payload.get("quality_label", ""),
                },
            )
        elif event == rec.EVT_DISK_LOW:
            # 录制过程中发现磁盘不足：立即触发全局暂停
            logger.error("[磁盘告警] %s 录制中断：磁盘空间不足", runtime.display_nickname)
            self._pause_all("磁盘剩余空间不足")
        elif event == rec.EVT_ERROR:
            runtime.last_error = payload.get("error", "")
            runtime.status = "error"
            self.bus.publish(ENGINE_EVENTS.ROOM_ERROR, dict(payload))
        elif event == rec.EVT_STOPPED:
            final_file = payload.get("file", "")
            runtime.last_final_path = final_file
            runtime.recorder = None
            runtime.recording_since = None
            if runtime.entry.enabled:
                runtime.status = "offline"
            self.bus.publish(ENGINE_EVENTS.RECORDING_FINISHED, dict(payload))

    # ---------------------------------------------------------------- 磁盘保护
    def _check_disk_guard(self) -> None:
        """检查磁盘空间，必要时暂停/恢复全部录制。"""
        config = self.config
        free_gb = get_free_space_gb(config.save_path)

        if not self._disk_paused:
            if free_gb < config.min_free_disk_gb:
                self._pause_all(
                    f"磁盘剩余 {free_gb:.2f} GB，低于阈值 {config.min_free_disk_gb:.2f} GB"
                )
            return

        # 已暂停：空间恢复到「阈值 + 余量」以上时自动继续
        if free_gb >= config.min_free_disk_gb + _DISK_HYSTERESIS_GB:
            with self._lock:
                self._disk_paused = False
                self._disk_pause_reason = ""
            logger.info("磁盘空间已恢复（剩余 %.2f GB），自动继续监控", free_gb)
            self.bus.publish(ENGINE_EVENTS.DISK_RESUMED, {"free_gb": free_gb})

    def _pause_all(self, reason: str) -> None:
        """暂停全部录制任务。"""
        with self._lock:
            already = self._disk_paused
            self._disk_paused = True
            self._disk_pause_reason = reason
            runtimes = [r for r in self._runtimes.values() if r.is_recording()]

        if already:
            return

        logger.error("⚠ 已暂停全部录制任务：%s", reason)
        self.bus.publish(
            ENGINE_EVENTS.DISK_PAUSED,
            {"reason": reason, "stopped": len(runtimes)},
        )
        self.bus.publish(
            ENGINE_EVENTS.ALERT,
            {"level": "error", "title": "磁盘空间不足", "message": f"{reason}\n已暂停全部录制任务，请清理磁盘后自动恢复。"},
        )

        for runtime in runtimes:
            recorder = runtime.recorder
            if recorder is not None:
                recorder.stop(reason="disk_low")
            runtime.status = "paused"

    # ---------------------------------------------------------------- 手动控制
    def stop_room_recording(self, key: str) -> bool:
        """手动停止某个房间的录制（不影响监控）。"""
        with self._lock:
            runtime = self._runtimes.get(key)
        if runtime is None or runtime.recorder is None:
            return False
        runtime.recorder.stop(reason="manual")
        return True

    def force_check_now(self) -> None:
        """立即触发一次检测（用户点「立即检测」时使用）。"""
        self._wake_event.set()

    def counts(self) -> Dict[str, int]:
        """返回概览统计，用于状态栏显示。"""
        with self._lock:
            runtimes = list(self._runtimes.values())
            paused = self._disk_paused
            free_gb = get_free_space_gb(self.config.save_path)

        recording = sum(1 for r in runtimes if r.is_recording())
        live = sum(1 for r in runtimes if r.status in ("live", "recording", "reconnecting"))
        return {
            "total": len(runtimes),
            "enabled": sum(1 for r in runtimes if r.entry.enabled),
            "live": live,
            "recording": recording,
            "free_gb": free_gb,
            "paused": int(paused),
        }
