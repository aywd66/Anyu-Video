# -*- coding: utf-8 -*-
"""
存储管理：目录组织、文件名生成、磁盘空间检查。

命名契约（唯一权威，改动请同步 README / ARCHITECTURE / 界面「使用说明」）
======================================================================

目录结构
--------

::

    <保存根目录>/
        └── <平台中文名>/                例：抖音
            └── <主播昵称>/              例：阿姨芥末
                ├── 抖音-阿姨芥末-2026-08-28_141530-001.flv
                ├── 抖音-阿姨芥末-2026-08-28_141530-002.flv
                └── 抖音-阿姨芥末-2026-08-28_141530-merged.flv

**只有两级目录**（平台 / 昵称），日期不做目录层 —— 早先是三级
（``<平台>/<昵称>/<2026年08月28日>/``），后按用户要求把日期并入文件名，
避免同一个主播下堆出大量日期文件夹（一场直播就多一个目录）。

未识别到昵称时，昵称目录退化为 ``未知主播-<房间号>``（见下「回退命名」）。

文件名格式
----------

::

    分片：{平台}-{主播昵称}-{YYYY-MM-DD}_{HHMMSS}-{分段序号}.{容器}
    成品：{平台}-{主播昵称}-{YYYY-MM-DD}_{HHMMSS}-merged.{容器}

各分量取值规则
--------------

===========  ==============================================================
分量          规则
===========  ==============================================================
平台          平台中文名（``PLATFORM_LABELS``），如 ``抖音`` / ``快手``；未知平台取 ``未知平台``
主播昵称       由平台解析得到并清洗非法字符；**识别失败时的回退见下**
日期          本场直播**起始日期** ``YYYY-MM-DD``（带横线，便于阅读）
时间          本场直播**起始时刻** ``HHMMSS``；日期与时间用 ``_`` 相连
分段序号       3 位数字，从 ``001`` 开始，每场直播独立计数
容器          FLV 流为 ``flv``，HLS 流为 ``ts``
===========  ==============================================================

日期时间为什么进文件名
----------------------

用户明确要求「命名直接加上时间，不要给时间单独创建文件夹」。
所以日期时间整体作为**一个分量** ``YYYY-MM-DD_HHMMSS`` 写进文件名，
目录只保留 平台 / 昵称 两级。

**注意这个分量内部含横线**，所以解析文件名时不能简单 ``split("-")`` ——
昵称本身也可能含横线。判定顺序是：从左侧剥掉「平台」，
从**右侧**剥掉「序号/merged」和「时间戳」，中间剩下的才是昵称。

时间用 ``YYYY-MM-DD`` 而非紧凑的 ``YYYYMMDD``，是为了让人类一眼读出来；
代价是分量内有横线，但文件名仍按日期时间排序正确。

回退命名
--------

昵称识别失败（空串 / 纯空白 / 只有非法字符）时：

- 昵称分量固定填 :data:`UNKNOWN_NICKNAME`（``未知主播``）；
- 目录名追加房间号消歧，即 ``未知主播-<房间号>``，避免多个未识别房间
  全部堆进同一个 ``未知主播`` 目录。

同时程序会在写入前把「解析到的昵称」回填到 ``config.json``，所以回退命名
通常只出现在**首场**录制；一旦昵称落盘，后续场次都会用真实昵称。

同名不冲突
----------

时间戳精度为秒，理论上两场直播可能碰撞（停止后秒级重启、或多实例并发）。
因此所有生成路径都会经过 :func:`reserve_path`：

1. 目录级：``未知主播`` 追加房间号消歧（见上）；
2. 文件级：目标已存在时依次尝试 ``-2`` / ``-3`` / …… （最多 :data:`MAX_DEDUP_TRIES` 次）。

注意：**分片序号本身就保证了同一场直播内不冲突**（``-001`` / ``-002`` …），
文件级去重只处理「跨场次同秒」这种极端情况。
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
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

# 昵称识别失败时的占位名（同时用于目录与文件名）
UNKNOWN_NICKNAME = "未知主播"
# 未知平台占位
UNKNOWN_PLATFORM = "未知平台"

# 昵称/房间号分量长度上限（防止路径过长）
_DEFAULT_NAME_MAX_LENGTH = 60
_PLATFORM_MAX_LENGTH = 20
# 房间号分量上限：平台 id 一般不超过 32 位，留足余量
_ROOM_ID_MAX_LENGTH = 48
# 文件级去重最大尝试次数（-2 ... -10）
MAX_DEDUP_TRIES = 10

# 文件名各分量之间的分隔符（与抖音下载器一致的连字符风格）
SEP = "-"
# 文件名里的日期格式：带横线，便于人类阅读
FILE_DATE_FORMAT = "%Y-%m-%d"
# 文件名里的时刻格式
FILE_TIME_FORMAT = "%H%M%S"
# 日期与时间之间的连接符。
# 刻意**不用** SEP（``-``）：日期内部已经用了横线（2026-09-24），
# 若再用横线连接时刻，整个时间分量会变成 ``2026-09-24-133057``，
# 和分隔符完全无法区分。用下划线把「日期+时刻」捆成一个整体分量。
DATETIME_JOIN = "_"


def sanitize_name(name: str, fallback: str = UNKNOWN_NICKNAME, max_length: int = 60) -> str:
    """把昵称清洗为合法且安全的目录/文件名片段。

    - 去掉 Windows 非法字符
    - 折叠连续空白为单个下划线
    - 去掉首尾空白、点、下划线**与连字符**
      （连字符是文件名分隔符，昵称首尾若带连字符会与分隔符混淆，
      产生 ``抖音--阿姨--141530-001.flv`` 这种歧义写法）
    - 限制长度，避免路径过长
    - 清洗后为空则返回 ``fallback``
    """
    cleaned = _ILLEGAL_CHARS.sub("_", (name or "").strip())
    # 连续空白折叠成单个下划线，避免文件名里出现大段空格
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = cleaned.strip(" ._-")
    if not cleaned:
        cleaned = fallback
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" ._-")
    return cleaned or fallback


def is_meaningful_name(name: Optional[str]) -> bool:
    """判断昵称是否「有意义」（非空且清洗后不是占位名）。

    用于区分「真的解析到了昵称」和「拿房间号临时顶替」两种情况。
    """
    if not name:
        return False
    cleaned = sanitize_name(name, fallback="")
    return bool(cleaned) and cleaned != UNKNOWN_NICKNAME


# --------------------------------------------------------------------------
# 会话命名（一场直播的命名快照）
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SessionNaming:
    """一场直播的命名快照，**创建后不再变化**。

    录制器必须在 :meth:`StreamRecorder.start` 时冻结本对象，之后所有分片
    与合并都复用它。否则中途回填的昵称会让同一场直播的分片散落到不同目录。
    """

    platform: str
    nickname: str          # 已清洗，识别失败时为 UNKNOWN_NICKNAME
    room_id: str           # 已清洗，可为空串
    session_start: datetime

    # ---------------------------------------------------------------- 派生值
    @property
    def platform_label(self) -> str:
        """平台中文名（文件/目录中实际使用的写法）。"""
        return sanitize_name(
            PLATFORM_LABELS.get(self.platform, self.platform or UNKNOWN_PLATFORM),
            UNKNOWN_PLATFORM,
            _PLATFORM_MAX_LENGTH,
        )

    @property
    def dir_nickname(self) -> str:
        """目录中使用的昵称片段。

        未识别到昵称时追加房间号消歧，避免多个未知房间混进同一目录。
        """
        if self.nickname == UNKNOWN_NICKNAME and self.room_id:
            return sanitize_name(f"{UNKNOWN_NICKNAME}-{self.room_id}")
        return self.nickname

    @property
    def display_name(self) -> str:
        """界面/日志展示用名称（不回退到房间号时会显示占位名）。"""
        return self.nickname

    @property
    def date_token(self) -> str:
        """日期分量 ``2026-08-28``（带横线，便于阅读）。"""
        return self.session_start.strftime(FILE_DATE_FORMAT)

    @property
    def time_token(self) -> str:
        """时刻分量 ``141530``。"""
        return self.session_start.strftime(FILE_TIME_FORMAT)

    @property
    def datetime_token(self) -> str:
        """文件名里的「日期+时刻」整体分量 ``2026-08-28_141530``。

        用下划线把两段连起来，当作**一个**分量看待：
        文件名结构变成 ``平台-昵称-<日期_时刻>-序号``，
        解析时从右侧剥掉序号、再把最后一段按 ``_`` 拆回日期与时刻。
        """
        return f"{self.date_token}{DATETIME_JOIN}{self.time_token}"

    # ---------------------------------------------------------------- 文件名
    def _stem(self, suffix: str) -> str:
        """拼出不带容器的文件名主干。

        房间号不再参与文件名（保留在 :attr:`room_id` 供目录消歧与调用方使用）。
        """
        return SEP.join(
            [self.platform_label, self.nickname, self.datetime_token, suffix]
        )

    def segment_name(self, index: int, container: str = "flv") -> str:
        """分片文件名，``index`` 从 1 开始。"""
        return f"{self._stem(f'{index:03d}')}.{container}"

    def merged_name(self, container: str = "flv") -> str:
        """合并后的成品文件名。"""
        return f"{self._stem(MERGED_SUFFIX)}.{container}"

    def describe(self) -> str:
        """一句话描述本场命名，便于日志与「使用说明」展示。"""
        room = self.room_id or "无房间号"
        return (
            f"{self.platform_label} / {self.nickname} / {room} / "
            f"{self.datetime_token}"
        )


def make_session_naming(
    platform: str,
    nickname: Optional[str],
    room_id: Optional[str],
    session_start: Optional[datetime] = None,
) -> SessionNaming:
    """构造一场直播的命名快照。

    - ``nickname`` 为空/无意义 → 回退为 :data:`UNKNOWN_NICKNAME`，
      并在目录名中追加房间号消歧；
    - ``nickname`` 若恰好等于房间号（历史调用方把房间号当昵称传入），
      同样视为未识别，避免「昵称」分量变成一串数字；
    - ``room_id`` 清洗后可为空串，此时文件名省略该分量。
    """
    session_start = session_start or datetime.now()
    raw_nickname = (nickname or "").strip()
    raw_room_id = (room_id or "").strip()

    # 历史调用方习惯把 room_id 塞进 nickname（``room.nickname or room.room_id``），
    # 这里做一次纠偏：昵称等于房间号时按「未识别」处理。
    if raw_nickname and raw_room_id and raw_nickname == raw_room_id:
        raw_nickname = ""

    if is_meaningful_name(raw_nickname):
        clean_nickname = sanitize_name(raw_nickname)
    else:
        clean_nickname = UNKNOWN_NICKNAME

    clean_room_id = sanitize_name(raw_room_id, fallback="", max_length=_ROOM_ID_MAX_LENGTH) \
        if raw_room_id else ""

    return SessionNaming(
        platform=platform or "",
        nickname=clean_nickname,
        room_id=clean_room_id,
        session_start=session_start,
    )


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
    room_id: Optional[str] = None,
) -> Path:
    """按「平台 / 主播昵称」两级目录组织，返回该目录路径。

    日期**不再**单独建目录层（用户要求：命名直接带时间，不要给时间单独建文件夹），
    而是写进文件名。``when`` 仍接收，用于构造命名快照。

    建议优先使用 :func:`build_output_dir_from_naming`，它直接消费
    :class:`SessionNaming`，与文件名保持同一份命名快照。
    """
    naming = make_session_naming(platform, nickname, room_id, when)
    return build_output_dir_from_naming(root, naming)


def build_output_dir_from_naming(root: str | Path, naming: SessionNaming) -> Path:
    """由命名快照生成输出目录，确保目录名与文件名同源。

    两级结构 ``<root>/<平台>/<昵称>/``。
    日期时间信息在**文件名**里（``<平台>-<昵称>-<YYYY-MM-DD_HHMMSS>-<序号>``），
    不再作为目录层 —— 否则每录一场就多一个日期文件夹。
    """
    directory = Path(root) / naming.platform_label / naming.dir_nickname
    return ensure_dir(directory)


def reserve_path(path: str | Path) -> Path:
    """返回一个当前不存在的路径。

    目标已被占用时依次尝试 ``-2`` / ``-3`` / ……，最多 :data:`MAX_DEDUP_TRIES` 次；
    全部占用则追加微秒级时间戳兜底，保证**任何情况下都不会覆盖已有录像**。
    """
    target = Path(path)
    if not target.exists():
        return target

    stem, suffix = target.stem, target.suffix
    for index in range(2, MAX_DEDUP_TRIES + 2):
        candidate = target.with_name(f"{stem}{SEP}{index}{suffix}")
        if not candidate.exists():
            return candidate

    # 极端情况：同名文件过多，用微秒时间戳兜底（仍不覆盖）
    stamp = datetime.now().strftime("%f")
    fallback = target.with_name(f"{stem}{SEP}{stamp}{suffix}")
    logger.warning("同名文件过多，改用时间戳兜底命名：%s", fallback.name)
    return fallback


def build_segment_path(
    directory: str | Path,
    platform: str,
    nickname: str,
    session_start: datetime,
    segment_index: int,
    container: str = "flv",
    room_id: Optional[str] = None,
    naming: Optional[SessionNaming] = None,
) -> Path:
    """生成一个分片文件的完整路径。

    参数
    ----
    directory:     输出目录（通常来自 :func:`build_output_dir_from_naming`）
    session_start: 本场直播的起始时间（写进文件名，同一场直播所有分片共用）
    segment_index: 分段序号，从 1 开始，固定 3 位
    container:     容器格式，flv / ts
    room_id:       房间标识（仅用于目录消歧，不写进文件名）
    naming:        命名快照；**推荐传入**，与目录使用同一份快照
    """
    if naming is None:
        naming = make_session_naming(platform, nickname, room_id, session_start)
    return reserve_path(Path(directory) / naming.segment_name(segment_index, container))


def build_merged_path(
    directory: str | Path,
    platform: str,
    nickname: str,
    session_start: datetime,
    container: str = "flv",
    room_id: Optional[str] = None,
    naming: Optional[SessionNaming] = None,
) -> Path:
    """生成本场直播合并后的目标文件路径。"""
    if naming is None:
        naming = make_session_naming(platform, nickname, room_id, session_start)
    return reserve_path(Path(directory) / naming.merged_name(container))


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
