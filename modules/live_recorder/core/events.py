# -*- coding: utf-8 -*-
"""
事件总线

调度层、录制层会产生大量异步事件（开播、开始录制、降级、重连、合并、磁盘告警……），
界面层需要实时感知但又不应该与业务层直接耦合。

``EventBus`` 提供一个极简的「发布-订阅」通道：

    业务线程  --publish-->  EventBus  --回调-->  界面层（转换为 Qt 信号后投递到主线程）

所有回调都在发布者线程中同步执行，因此界面层的桥接对象**必须**只用 Qt 的
信号机制转发，不能在回调里直接操作控件。
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List

from ..utils.logger import get_logger

logger = get_logger(__name__)


class ENGINE_EVENTS:
    """调度层事件名常量。"""

    MONITOR_STARTED = "monitor_started"          # 开始监控
    MONITOR_STOPPED = "monitor_stopped"          # 停止监控
    ROOM_ADDED = "room_added"                    # 新增监控对象
    ROOM_RESOLVED = "room_resolved"              # 分享链接解析完成
    ROOM_REMOVED = "room_removed"                # 删除监控对象
    ROOM_UPDATED = "room_updated"                # 房间信息发生变化（昵称/状态）
    ROOM_ERROR = "room_error"                    # 房间发生错误
    DISK_PAUSED = "disk_paused"                  # 磁盘不足，暂停全部录制
    DISK_RESUMED = "disk_resumed"                # 磁盘空间恢复，自动继续
    RECORDING_STARTED = "recording_started"      # 某房间开始录制
    RECORDING_FINISHED = "recording_finished"    # 某房间录制结束（含成品路径）
    QUALITY_FALLBACK = "quality_fallback"        # 画质降级
    ALERT = "alert"                              # 需要提示用户的告警


class EventBus:
    """线程安全的事件总线。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: List[Callable[[str, Dict[str, Any]], None]] = []

    def subscribe(self, callback: Callable[[str, Dict[str, Any]], None]) -> None:
        """注册订阅者。重复注册会被忽略。"""
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[str, Dict[str, Any]], None]) -> None:
        """注销订阅者。"""
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def publish(self, event: str, payload: Dict[str, Any] | None = None) -> None:
        """发布事件（同步通知所有订阅者，单个订阅者异常不影响其他订阅者）。"""
        with self._lock:
            subscribers = list(self._subscribers)
        data = dict(payload or {})
        for callback in subscribers:
            try:
                callback(event, data)
            except Exception as exc:  # noqa: BLE001
                logger.debug("事件订阅者异常（event=%s）：%s", event, exc)
