# -*- coding: utf-8 -*-
"""
平台解析层抽象基类与数据模型。

各平台解析器需继承 ``BaseLiveParser`` 并实现三个核心方法：

    resolve_room(raw_input)          —— 把分享链接/房间号解析为规范化房间标识
    check_live(room_id)              —— 检测开播状态，返回 RoomInfo
    fetch_stream(room_id, quality)   —— 获取可拉流的原画/超清地址

把「平台差异」全部收敛在子类中，上层调度与录制逻辑完全不感知平台细节，
平台接口变更时只需修改对应子类。
"""

from __future__ import annotations

import abc
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional

from ..utils.logger import get_logger
from .utils import build_headers, random_profile, safe_json, throttle

logger = get_logger(__name__)


class PlatformError(Exception):
    """平台解析相关错误的统一异常类型。"""


class RoomUnavailableError(PlatformError):
    """房间确实不可用（不存在 / 已下播 / 内容受限）。

    与普通网络异常区分开，是为了让上层能把它判定为「未开播」而不是
    「状态未知」——后者会让监控一直保持现状、看不到真实原因。
    平台返回的业务错误码（如抖音 status_code=4001038「该内容暂时无法查看」）
    应转换为本异常。
    """


class RiskControlError(PlatformError):
    """请求被平台风控拦截（HTTP 403/429/444 或返回验证页）。

    该异常**不重试**：越试越容易被长时间封禁。上层应降低检测频率或
    等待一段时间后再试。
    """


class EmptyResponseError(RiskControlError):
    """HTTP 200 但响应体为空。

    单独成类是因为它有明确的可恢复路径：抖音在**缺少首访 Cookie**
    （ttwid 等）时会稳定返回「200 + 空体」，只要补一次首页访问即可正常。
    若与普通风控混为一谈，就会表现为「怎么都录不上」且无从下手。
    仍继承 ``RiskControlError``，保证不落入指数退避重试循环。
    """


class LiveStatus(str, Enum):
    """直播间状态。"""

    LIVE = "live"          # 正在直播
    OFFLINE = "offline"    # 未开播
    UNKNOWN = "unknown"    # 无法判定（网络异常 / 风控 / 房间不存在）


class StreamQuality(str, Enum):
    """画质档位。"""

    ORIGIN = "origin"      # 原画（1080p，最高可用码率）
    HIGH = "high"          # 超清（720p）


class StreamFormat(str, Enum):
    """流封装格式，决定录制输出容器。"""

    FLV = "flv"            # FLV：直接以 -c copy 存入 .flv
    HLS = "hls"            # HLS(m3u8/TS)：存入 .ts，避免转码


@dataclass
class RoomInfo:
    """一次检测得到的直播间信息快照。"""

    platform: str
    room_id: str
    status: LiveStatus = LiveStatus.UNKNOWN
    nickname: str = ""
    title: str = ""
    cover: str = ""
    online_count: int = 0
    raw: Dict = field(default_factory=dict)   # 原始响应，便于排查
    # 补充说明：例如「房间不可观看：该内容暂时无法查看」「触发风控」。
    # 用于让界面/日志直接显示「为什么没录上」，而不是笼统的「状态未知」。
    note: str = ""

    @property
    def is_live(self) -> bool:
        return self.status is LiveStatus.LIVE


@dataclass
class StreamInfo:
    """一条可直接交给 ffmpeg 拉取的流地址。"""

    url: str
    stream_format: StreamFormat
    quality: StreamQuality
    quality_label: str = ""                   # 例如 "origin" / "hd720" / "sd"
    headers: Dict[str, str] = field(default_factory=dict)  # ffmpeg 拉流所需的请求头
    is_fallback: bool = False                 # 是否由降级策略产生

    @property
    def container(self) -> str:
        """根据流类型选择输出容器：FLV -> flv，HLS -> ts。"""
        return "ts" if self.stream_format is StreamFormat.HLS else "flv"


class BaseLiveParser(abc.ABC):
    """平台解析器抽象基类。

    子类只需关心「怎么调接口、怎么解析 JSON」，重试、节流、随机请求头等
    通用逻辑在基类中统一实现。
    """

    #: 平台标识，如 "douyin" / "kuaishou"
    platform: str = ""
    #: 平台中文名，用于界面展示
    display_name: str = ""

    def __init__(self, config=None) -> None:
        # 延迟导入避免循环依赖：config 仅用于读取节流/UA 等参数
        self.config = config
        self._session = None  # 由 _get_session 惰性创建
        # 会话级固定浏览器指纹：同一会话内 UA / Accept-Language 保持一致，
        # 否则 Cookie 与 UA 不匹配，反而更容易被风控识别
        self._profile = random_profile()

    # ------------------------------------------------------------------ 会话
    def _get_session(self):
        """惰性创建带默认请求头的 httpx.Client（cookie 自动保持）。"""
        if self._session is None:
            import httpx  # 延迟导入：未安装 httpx 时也能给出友好报错

            self._session = httpx.Client(
                headers=build_headers(self.platform, profile=self._profile),
                follow_redirects=True,
                timeout=httpx.Timeout(connect=8.0, read=15.0, write=15.0, pool=8.0),
                # HTTP/2 更贴近浏览器行为，部分平台对 HTTP/1.1 更敏感
                http2=False,
            )
        return self._session

    def close(self) -> None:
        """释放底层连接池。"""
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                pass
            self._session = None

    # -------------------------------------------------------------- 通用请求
    def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        data=None,
        headers: Optional[dict] = None,
        referer: Optional[str] = None,
        retries: int = 3,
        expect_json: bool = True,
        throttle_range=(0.5, 1.5),
    ):
        """发起一次带节流与重试的请求。

        参数
        ----
        retries:      失败重试次数（指数退避）。风控类错误不适用（见下）
        expect_json:  True 返回解析后的 dict；False 返回原始响应对象
        throttle_range: 请求前的随机等待区间

        说明
        ----
        遇到 403/429/444 这类**风控**状态码时立即抛出 ``RiskControlError`` 且不重试：
        连续重试会显著提高被长时间封禁的概率。调用方应改为降低频率或等待。
        """
        merged_headers = build_headers(
            self.platform, referer=referer, extra=headers, profile=self._profile
        )
        session = self._get_session()
        last_error: Optional[Exception] = None

        # 风控状态码：重试只会加重封禁，因此单独识别并立即放弃
        risk_status = {403, 429, 444, 418}

        for attempt in range(1, retries + 1):
            throttle(*throttle_range)
            try:
                response = session.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=merged_headers,
                )
                if response.status_code in risk_status:
                    raise RiskControlError(
                        f"HTTP {response.status_code} 疑似触发风控（{url}）"
                    )
                if response.status_code >= 400:
                    raise PlatformError(f"HTTP {response.status_code} - {url}")
                if not response.content:
                    # 200 却是空响应：抖音在缺少首访 Cookie 时即为此表现，
                    # 也可能是平台限流。不重试，交由平台层决定是否补 Cookie。
                    raise EmptyResponseError(
                        f"平台返回空响应（疑似缺首访 Cookie 或限流）- {url}"
                    )
                if expect_json:
                    payload = safe_json(response)
                    if payload is None:
                        # 带上 content-type 与长度，便于判断是验证页还是接口变更
                        ctype = response.headers.get("content-type", "未知")
                        body = (response.text or "").strip()
                        if self._looks_like_verification(body):
                            raise RiskControlError(
                                f"平台返回验证/风控页面（content-type={ctype}）- {url}"
                            )
                        raise PlatformError(
                            f"响应不是合法 JSON（content-type={ctype}，"
                            f"长度 {len(response.content)}，片段 {body[:80]!r}）- {url}"
                        )
                    return payload
                return response
            except RiskControlError:
                # 风控不重试，直接抛出
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                # 指数退避，并叠加随机抖动
                backoff = min(8.0, 1.5 ** attempt)
                time.sleep(backoff + random.random())
                logger.debug(
                    "请求失败（第 %d/%d 次）：%s -> %s", attempt, retries, url, exc
                )

        raise PlatformError(f"请求最终失败：{url}（{last_error}）")

    @staticmethod
    def _looks_like_verification(body: str) -> bool:
        """判断返回的文本是否是风控/验证页面而不是正常业务响应。"""
        if not body:
            return False
        lowered = body[:2000].lower()
        markers = (
            "captcha", "slider", "secsdk", "verify_center",
            "验证码", "安全验证", "访问过于频繁", "请求过快",
            "拒绝访问", "blocked", "forbidden",
        )
        return any(marker in lowered for marker in markers)

    # ------------------------------------------------------------------ 接口
    @abc.abstractmethod
    def resolve_room(self, raw_input: str) -> str:
        """把用户输入（分享链接 / 房间号）解析为规范化的房间标识。

        解析失败应抛出 ``PlatformError``。
        """

    @abc.abstractmethod
    def check_live(self, room_id: str) -> RoomInfo:
        """检测指定房间的开播状态。

        网络异常时返回 ``LiveStatus.UNKNOWN`` 的 RoomInfo 而不是抛异常，
        以免一次网络抖动导致上层误判下播。
        """

    @abc.abstractmethod
    def fetch_stream(
        self, room_id: str, quality: StreamQuality = StreamQuality.ORIGIN
    ) -> StreamInfo:
        """获取指定画质的可拉流地址。

        找不到对应画质时抛出 ``PlatformError``，由上层决定是否降级。
        """

    # ------------------------------------------------------------ 便捷方法
    def fetch_stream_with_fallback(
        self, room_id: str, prefer: StreamQuality = StreamQuality.ORIGIN
    ) -> StreamInfo:
        """优先取偏好画质，失败时降级到另一档画质。

        降级产生的 StreamInfo 会带上 ``is_fallback=True``，供日志记录降级事件。
        """
        try:
            return self.fetch_stream(room_id, prefer)
        except RoomUnavailableError:
            # 房间本身不可用（未开播 / 内容受限）：换画质同样不会有结果，
            # 直接上抛，避免多打一次无意义的请求。
            raise
        except PlatformError as exc:
            fallback = (
                StreamQuality.HIGH
                if prefer is StreamQuality.ORIGIN
                else StreamQuality.ORIGIN
            )
            logger.warning(
                "%s 房间 %s 获取 %s 画质失败（%s），尝试降级到 %s",
                self.display_name,
                room_id,
                prefer.value,
                exc,
                fallback.value,
            )
            info = self.fetch_stream(room_id, fallback)
            info.is_fallback = True
            return info

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _ffmpeg_headers(headers: Dict[str, str]) -> Dict[str, str]:
        """裁剪出 ffmpeg 拉流真正需要的头，避免携带多余的 sec-ch-ua 等。"""
        keep = ("User-Agent", "Referer", "Origin", "Cookie", "Accept")
        return {k: v for k, v in headers.items() if k in keep}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False
