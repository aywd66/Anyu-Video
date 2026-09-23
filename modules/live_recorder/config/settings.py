# -*- coding: utf-8 -*-
"""
配置管理

- ``AppConfig``  全局设置（轮询间隔、保存路径、画质偏好、重连参数……）
- ``RoomEntry``  单个监控对象（平台 + 用户输入 + 解析后的房间信息）
- ``ConfigStore`` 负责两者到本地 JSON 文件的原子读写

配置文件默认位置：<项目根>/config.json，可用环境变量 LIVE_RECORDER_CONFIG 覆盖。
所有写操作采用「先写临时文件再替换」的原子方式，避免断电产生半截 JSON。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
DEFAULT_SAVE_DIR = str(Path.home() / "LiveRecorder")
CONFIG_FILE_NAME = "config.json"

# 轮询间隔可选范围（秒）
POLL_INTERVAL_MIN = 10
POLL_INTERVAL_MAX = 300
# 重连间隔可选范围（秒）
RECONNECT_INTERVAL_MIN = 5
RECONNECT_INTERVAL_MAX = 120


def default_config_path() -> Path:
    """返回配置文件路径（支持环境变量覆盖，便于多实例/便携部署）。"""
    override = os.environ.get("LIVE_RECORDER_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    return (Path(__file__).resolve().parent.parent / CONFIG_FILE_NAME).resolve()


# --------------------------------------------------------------------------
# 设置
# --------------------------------------------------------------------------
@dataclass
class AppConfig:
    """全局设置。字段命名与 JSON 中的键一一对应。"""

    # ---- 监控 ----
    poll_interval: int = 60                 # 轮询间隔（秒），10~300
    max_monitor_rooms: int = 100            # 监控对象上限（要求 >=50，默认放宽到 100）
    check_concurrency: int = 6              # 并发检测线程数，过大易触发风控

    # ---- 录制 ----
    save_path: str = DEFAULT_SAVE_DIR       # 录制文件保存根目录
    quality_preference: str = "origin"      # origin=优先原画；high=优先超清
    enable_fallback: bool = True            # 原画失败时自动降级到 720p
    max_file_size_gb: float = 4.0           # 单分片大小上限（GB），达到后自动分卷
    max_duration_minutes: int = 0           # 单场直播时长上限（分钟），0=不限
    max_concurrent_recordings: int = 8      # 同时录制数上限，保护带宽与磁盘

    # ---- 异常与重连 ----
    reconnect_interval: int = 15            # 拉流中断后重连间隔（秒）
    stall_timeout: int = 25                 # 输出文件停止增长超过该秒数判定为卡顿
    max_reconnect_attempts: int = 0         # 0=无限重连（直到下播或手动停止）

    # ---- 存储 ----
    min_free_disk_gb: float = 10.0          # 磁盘剩余低于该值暂停全部录制
    convert_to_mp4: bool = False            # 录制完成后转封装为 MP4（-c copy 不重编码）

    # ---- 环境 ----
    ffmpeg_path: str = "ffmpeg"             # ffmpeg 可执行文件路径，可填绝对路径
    playwright_enabled: bool = True         # 允许在必要时用 playwright 处理动态签名
    headless_browser: bool = True           # playwright 是否无头运行

    # ---- 平台账号（可选，但强烈建议填写）----
    # 从浏览器复制来的 Cookie 字符串（形如 "did=web_xxx; kuaishou.live.web_st=xxx"）。
    #
    # 为什么需要：快手网页端**按房间查询**的接口（livedetail / profile/public）
    # 已对未登录访客失效（固定返回占位响应），未登录时程序只能监控
    # 「推荐流中被推荐的房间」。填入登录后的 Cookie 后即可查询任意指定主播。
    # 留空不影响程序运行，只是可监控的范围会受限。
    kuaishou_cookie: str = ""
    # 抖音 Cookie（可选）。抖音目前无需登录即可解析，填了可进一步降低风控概率。
    douyin_cookie: str = ""

    # ---- 快手推荐流覆盖范围 ----
    # 推荐流是快手唯一仍可用的「批量获取在播房间 + 流地址」入口。
    # 页数越多覆盖越广，但每个轮询周期会多出相应次数的请求。
    kuaishou_reco_pages: int = 3

    # ---- 界面 ----
    auto_start_monitor: bool = False        # 启动程序后自动开始监控（恢复上次任务）
    theme: str = "light"                    # light / dark

    # ------------------------------------------------------------------
    def normalize(self) -> None:
        """把越界或非法的配置值收敛到合理范围（统一在此处兜底）。"""
        self.poll_interval = _clamp_int(
            self.poll_interval, POLL_INTERVAL_MIN, POLL_INTERVAL_MAX, 60
        )
        self.reconnect_interval = _clamp_int(
            self.reconnect_interval, RECONNECT_INTERVAL_MIN, RECONNECT_INTERVAL_MAX, 15
        )
        self.check_concurrency = _clamp_int(self.check_concurrency, 1, 20, 6)
        self.max_concurrent_recordings = _clamp_int(self.max_concurrent_recordings, 1, 50, 8)
        self.max_monitor_rooms = _clamp_int(self.max_monitor_rooms, 50, 1000, 100)
        self.stall_timeout = _clamp_int(self.stall_timeout, 10, 300, 25)
        self.max_duration_minutes = max(0, int(self.max_duration_minutes or 0))
        self.max_reconnect_attempts = max(0, int(self.max_reconnect_attempts or 0))
        self.max_file_size_gb = max(0.5, float(self.max_file_size_gb or 4.0))
        self.min_free_disk_gb = max(0.0, float(self.min_free_disk_gb or 10.0))
        if self.quality_preference not in ("origin", "high"):
            self.quality_preference = "origin"
        if self.theme not in ("light", "dark"):
            self.theme = "light"
        # Cookie 里可能混入换行/多余空白，统一清洗
        self.kuaishou_cookie = _clean_cookie(self.kuaishou_cookie)
        self.douyin_cookie = _clean_cookie(self.douyin_cookie)
        self.kuaishou_reco_pages = _clamp_int(self.kuaishou_reco_pages, 1, 10, 3)

    @property
    def max_file_size_bytes(self) -> int:
        """分片大小上限（字节）。"""
        return int(self.max_file_size_gb * 1024 ** 3)

    @property
    def max_duration_seconds(self) -> int:
        """单场直播时长上限（秒），0 表示不限。"""
        return int(self.max_duration_minutes * 60)


def _clamp_int(value: Any, low: int, high: int, fallback: int) -> int:
    """把 value 收敛到 [low, high]，无法解析时返回 fallback。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def _clean_cookie(value: Any) -> str:
    """清洗用户粘贴的 Cookie 字符串。

    从浏览器复制出来的内容常带首尾空白、换行，甚至 ``Cookie:`` 前缀，
    直接使用会导致 Cookie 头非法（HTTP 头不允许出现换行）。
    """
    if not value:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ")
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1]
    return " ".join(text.split()).strip().rstrip(";")


# --------------------------------------------------------------------------
# 直播间条目
# --------------------------------------------------------------------------
@dataclass
class RoomEntry:
    """一个监控对象的配置与（解析后的）缓存信息。"""

    platform: str = ""            # "kuaishou" / "douyin"，空表示待自动识别
    raw_input: str = ""           # 用户原始输入：分享链接或房间号
    room_id: str = ""             # 解析后的房间号 / web_rid / principalId
    nickname: str = ""            # 主播昵称（解析成功后回填）
    enabled: bool = True          # 是否纳入监控
    remark: str = ""              # 用户备注

    # 唯一键：同一平台同房间号视为同一个监控对象
    @property
    def key(self) -> str:
        if self.room_id:
            return f"{self.platform}:{self.room_id}"
        return f"{self.platform or 'unknown'}:{self.raw_input}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RoomEntry":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


# --------------------------------------------------------------------------
# 配置存储
# --------------------------------------------------------------------------
class ConfigStore:
    """配置读写器（线程安全）。

    内存中始终持有一份 ``AppConfig`` 与 ``List[RoomEntry]``，
    ``save()`` 时整体序列化写盘。
    """

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self.path = Path(path) if path else default_config_path()
        self._lock = threading.RLock()
        self.config = AppConfig()
        self.rooms: List[RoomEntry] = []

    # ------------------------------------------------------------------ 读
    def load(self) -> "ConfigStore":
        """从磁盘加载配置；文件不存在或损坏时回退为默认配置。"""
        with self._lock:
            if not self.path.exists():
                self.config = AppConfig()
                self.rooms = []
                return self
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # 配置损坏时使用默认值，避免程序无法启动
                self.config = AppConfig()
                self.rooms = []
                return self

            settings = raw.get("settings", {}) if isinstance(raw, dict) else {}
            known = {f.name for f in fields(AppConfig)}
            config = AppConfig(**{k: v for k, v in settings.items() if k in known})
            config.normalize()
            self.config = config

            rooms_raw = raw.get("rooms", []) if isinstance(raw, dict) else []
            self.rooms = [
                RoomEntry.from_dict(item)
                for item in rooms_raw
                if isinstance(item, dict)
            ]
            return self

    # ------------------------------------------------------------------ 写
    def save(self) -> None:
        """原子写盘：先写 .tmp，再 os.replace 覆盖。"""
        with self._lock:
            payload = {
                "version": 1,
                "settings": asdict(self.config),
                "rooms": [room.to_dict() for room in self.rooms],
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(".json.tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, self.path)

    # --------------------------------------------------------------- 房间
    def add_room(self, entry: RoomEntry) -> bool:
        """新增监控对象；若 key 已存在则返回 False（由调用方提示重复）。"""
        with self._lock:
            existing = {room.key for room in self.rooms}
            if entry.key in existing:
                return False
            self.rooms.append(entry)
            return True

    def remove_room(self, key: str) -> bool:
        """按 key 删除监控对象。"""
        with self._lock:
            before = len(self.rooms)
            self.rooms = [room for room in self.rooms if room.key != key]
            return len(self.rooms) != before

    def find_room(self, key: str) -> Optional[RoomEntry]:
        """按 key 查找监控对象。"""
        with self._lock:
            for room in self.rooms:
                if room.key == key:
                    return room
        return None

    def upsert_room(self, entry: RoomEntry) -> RoomEntry:
        """按 key 插入或更新（用于解析成功后回填房间号与昵称）。"""
        with self._lock:
            for index, room in enumerate(self.rooms):
                if room.key == entry.key or room.raw_input == entry.raw_input:
                    # 保留用户备注等本地字段
                    entry.remark = entry.remark or room.remark
                    self.rooms[index] = entry
                    return entry
            self.rooms.append(entry)
            return entry
