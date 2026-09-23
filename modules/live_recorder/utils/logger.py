# -*- coding: utf-8 -*-
"""
日志系统

设计要点
--------
1. 全局单例 ``LOG_BUFFER`` 保存最近 N 条日志，界面层可直接读取或订阅，
   避免界面层与业务层互相 import，保持分层清晰。
2. 业务代码统一使用 ``get_logger(__name__)`` 获取标准 logging 日志器，
   所有日志会自动进入缓冲区，同时按需输出到控制台与文件。
3. 缓冲区线程安全，业务层在工作线程写日志不会与界面线程冲突。
"""

from __future__ import annotations

import logging
import sys
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Deque, List, Optional

# 缓冲区默认容量：保留最近 5000 条，防止长时间运行内存膨胀
DEFAULT_BUFFER_SIZE = 5000

# 日志格式：[时间] [级别] [模块] 内容
LOG_FORMAT = "[%(asctime)s] [%(levelname)-8s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass
class LogRecord:
    """一条结构化的日志记录，供界面层渲染。"""

    time: datetime
    level: str
    module: str
    message: str

    def format_line(self) -> str:
        """格式化为单行文本，用于日志窗口显示与文件导出。"""
        return (
            f"[{self.time.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"[{self.level:<8}] [{self.module}] {self.message}"
        )


class LogBuffer:
    """线程安全的环形日志缓冲区，支持多订阅者回调。

    界面层通过 ``subscribe`` 注册回调即可实时接收日志；
    订阅者抛出的异常会被吞掉，避免影响业务层正常写日志。
    """

    def __init__(self, maxlen: int = DEFAULT_BUFFER_SIZE) -> None:
        self._records: Deque[LogRecord] = deque(maxlen=maxlen)
        self._lock = threading.RLock()
        self._subscribers: List[Callable[[LogRecord], None]] = []

    # ------------------------------------------------------------------ 写
    def append(self, record: LogRecord) -> None:
        """追加一条日志并通知所有订阅者。"""
        with self._lock:
            self._records.append(record)
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(record)
            except Exception:  # noqa: BLE001 - 订阅者异常不应影响日志写入
                pass

    # ------------------------------------------------------------------ 读
    def subscribe(self, callback: Callable[[LogRecord], None]) -> None:
        """注册一个日志订阅者。"""
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[LogRecord], None]) -> None:
        """注销日志订阅者。"""
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def snapshot(self) -> List[LogRecord]:
        """返回当前缓冲区快照（浅拷贝）。"""
        with self._lock:
            return list(self._records)

    def clear(self) -> None:
        """清空缓冲区（不影响已落盘的日志文件）。"""
        with self._lock:
            self._records.clear()

    # ---------------------------------------------------------------- 导出
    def export(self, file_path: str | Path) -> int:
        """把缓冲区内容导出为文本文件，返回写入的日志条数。"""
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        records = self.snapshot()
        with path.open("w", encoding="utf-8") as fp:
            for record in records:
                fp.write(record.format_line() + "\n")
        return len(records)


# 全局日志缓冲区单例
LOG_BUFFER = LogBuffer()


class _BufferHandler(logging.Handler):
    """把标准 logging 记录转发到全局缓冲区。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOG_BUFFER.append(
                LogRecord(
                    time=datetime.fromtimestamp(record.created),
                    level=record.levelname,
                    # 只保留末级模块名，界面更简洁：live_recorder.platforms.douyin -> douyin
                    module=record.name.rsplit(".", 1)[-1],
                    message=record.getMessage(),
                )
            )
        except Exception:  # noqa: BLE001 - 日志系统自身不能抛出
            pass


# 标记是否已配置，避免重复添加 Handler
_CONFIGURED = False
_CONFIG_LOCK = threading.Lock()


def configure_logging(
    level: int = logging.INFO,
    log_dir: Optional[str | Path] = None,
    log_file_name: str = "live_recorder.log",
) -> Optional[Path]:
    """统一配置根日志器。

    参数
    ----
    level:        控制台日志级别
    log_dir:      若提供，则额外写入该目录下的日志文件（自动轮转不实现，简单追加）
    log_file_name: 日志文件名

    返回
    ----
    实际写入的日志文件路径；未启用文件日志时返回 None。
    """
    global _CONFIGURED
    with _CONFIG_LOCK:
        root = logging.getLogger()
        if _CONFIGURED:
            return None
        root.setLevel(logging.DEBUG)

        # 控制台输出。
        # 注意：用 pythonw.exe（无控制台）启动时 sys.stdout / sys.stderr 都是 None，
        # StreamHandler 会退化成 stream=None —— 每条日志都先格式化再被丢弃，
        # 并且每次 emit 都在 logging 内部触发一次被吞掉的 AttributeError。
        # 「视频下载工具箱」的菜单按钮正是用 pythonw 拉起本程序的，
        # 因此这里显式跳过控制台 handler；文件日志与界面日志窗格不受影响。
        if sys.stdout is not None:
            console = logging.StreamHandler(stream=sys.stdout)
            console.setLevel(level)
            console.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
            root.addHandler(console)

        # 缓冲区（界面日志窗口）
        buffer_handler = _BufferHandler()
        buffer_handler.setLevel(logging.DEBUG)
        root.addHandler(buffer_handler)

        file_path: Optional[Path] = None
        if log_dir is not None:
            directory = Path(log_dir)
            directory.mkdir(parents=True, exist_ok=True)
            file_path = directory / log_file_name
            file_handler = logging.FileHandler(file_path, encoding="utf-8")
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
            root.addHandler(file_handler)

        _CONFIGURED = True
        return file_path


def get_logger(name: str) -> logging.Logger:
    """获取业务日志器（统一入口，便于以后加过滤/埋点）。"""
    return logging.getLogger(name)
