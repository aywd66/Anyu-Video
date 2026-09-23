# -*- coding: utf-8 -*-
"""
快手（Kuaishou）直播解析器

现状与应对策略（重要，基于实测）
--------------------------------
快手网页端的「按房间查询」接口已对**未登录访客**全部关闭：

- ``live_api/liveroom/livedetail``：对任意 ``principalId``（含乱码）都返回同一份
  约 700 字节的占位响应（``result=2``、``author.name`` 为空、
  ``playUrls`` 为 ``{"h264": {}, "hevc": {}}``）。
  **已用「确认在播」的房间验证过：在播房间同样返回该占位响应**，
  因此它不是「未开播」，而是接口已失效。
- ``live_api/profile/public``：同样返回空的 ``playUrls``。
- ``live_api/liveroom/websocketinfo``、``live_api/search/*``：返回空或错误。
- ``live_api/category/*``：只返回**分类列表**（游戏名），不含房间。

浏览器兜底同样不可靠：
  用真实浏览器打开 ``live.kuaishou.com/u/<id>`` 会得到「请求过快，请稍后重试」。
  实测对照：**无头 Chromium、有头 Chromium、有头真实 Chrome 三者都会被拦截**，
  说明这不是浏览器指纹问题，而是访客级别的访问限制。
  更危险的是——被拦截时页面可能自动播放**推荐位**内容（实测请求房间 A 时
  页面跳转到了房间 B 并播放了 B 的流），若不校验就会「录到别人的直播」。

因此本模块的取流顺序为：

1. **推荐流索引（首选）**：``live_api/liveroom/reco`` 仍可正常返回
   **在播房间的完整流地址**（``liveStream.playUrls.h264.adaptationSet``）。
   该索引对所有房间共用、带 TTL，命中时完全不必启动浏览器。
   页数可在设置中调整（每页约 10 个房间）。
2. **旧详情接口**：仅在未标记降级时尝试一次，用于识别接口是否恢复。
3. **浏览器兜底**：先在同一会话预热首页，再打开直播间；
   同时做**风控识别**与**落地校验**，任一不通过即丢弃抓取结果。

如果你需要监控**任意指定主播**（而不是推荐流里恰好出现的房间），
请在「设置 → 账号」填入快手登录 Cookie（可用 login.bat 扫码登录后自动写入）。
带上登录凭证后，平台的访客限制不再适用。

开播判定
--------
**绝不能**用 ``bool(playUrls)`` 判断是否开播：未开播时接口同样会返回
``{"h264": {}, "hevc": {}}``，其布尔值为 True，会导致「永远判定为已开播」并
引发无限重连。正确顺序是：显式开播标志（``author.living`` / ``liveStream.living``
/ ``isLiving``）→ 是否存在**真实可拉流的地址**。

接口若再次变更，仅需修改本文件中的 ``*_API`` 常量与字段解析逻辑。
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from ..utils.logger import get_logger
from .base import (
    BaseLiveParser,
    LiveStatus,
    PlatformError,
    RiskControlError,
    RoomInfo,
    StreamFormat,
    StreamInfo,
    StreamQuality,
)
from .utils import dig, extract_url

logger = get_logger(__name__)


class KuaishouParser(BaseLiveParser):
    """快手直播解析器。"""

    platform = "kuaishou"
    display_name = "快手"

    HOME_URL = "https://live.kuaishou.com/"
    DETAIL_API = "https://live.kuaishou.com/live_api/liveroom/livedetail"

    #: 推荐流接口。**当前唯一仍然可用的「批量获取在播房间 + 真实流地址」入口**。
    #: 未登录即可调用，返回的每条记录都带完整 ``liveStream.playUrls``
    #: （含 ``adaptationSet.representation[].url``）。
    RECO_API = "https://live.kuaishou.com/live_api/liveroom/reco"

    #: 推荐流索引的缓存有效期（秒）。索引对**所有**房间共用，
    #: 因此监控 N 个快手房间时，每 TTL 秒只多付出「页数」次请求。
    RECO_CACHE_TTL = 45
    #: 推荐流拉取页数的**默认值**（实际取值来自配置 ``kuaishou_reco_pages``）。
    #: 每页约 10 个在播房间；页数越多覆盖越广，但请求次数也相应增加。
    RECO_PAGES = 3

    #: 浏览器兜底抓取结果的缓存有效期（秒）。check_live 与 fetch_stream 背靠背调用，
    #: 缓存可避免同一轮为同一房间启动两次浏览器。
    BROWSER_CACHE_TTL = 150

    #: 被风控时的缓存有效期（秒）。风控具有时效性，缓存太久会让房间在恢复后
    #: 仍被判为「未知」；太短又会在风控期内反复启动浏览器、加重封禁。
    BLOCKED_CACHE_TTL = 40

    # 播放地址 CDN 特征（用于浏览器抓取时的可信度排序）
    _CDN_HINTS = ("yximgs.com", "kwimgs.com", "gifshow.com", "kuaishou")

    def __init__(self, config=None) -> None:
        super().__init__(config)
        # room_id -> (抓取时间, StreamInfo 或 None, 是否被风控)
        self._browser_cache: Dict[str, Tuple[float, Optional[StreamInfo], bool]] = {}
        # HTTP 详情接口是否已确认失效（失效后不再重复尝试，直接走浏览器）
        self._api_degraded = False
        # 浏览器不可用时的原因（用于给出可操作提示）
        self._browser_unavailable_reason = ""
        # principalId -> 房间信息（来自推荐流），所有房间共用
        self._reco_index: Dict[str, dict] = {}
        self._reco_index_at: float = 0.0
        # 保护推荐流索引的刷新：并发检测时避免多个线程同时拉取
        self._reco_lock = threading.Lock()
        # 最近一次浏览器兜底是否被风控拦截（区别于「未开播」）
        self._last_probe_blocked = False
        # 已注入到 HTTP 会话的 Cookie（避免重复注入）
        self._applied_cookie = ""

    # ------------------------------------------------------------- 账号 Cookie
    def _user_cookie(self) -> str:
        """读取用户配置的快手 Cookie（可能在运行中被修改）。"""
        return str(getattr(self.config, "kuaishou_cookie", "") or "").strip()

    def _apply_user_cookie(self) -> None:
        """把用户配置的 Cookie 注入 HTTP 会话（幂等，支持运行中修改）。

        快手未登录时，按房间查询的接口固定返回占位响应；带上登录 Cookie
        后才可能返回真实数据。因此这里直接设置会话级 Cookie 头。
        """
        cookie = self._user_cookie()
        if not cookie or cookie == self._applied_cookie:
            return
        session = self._get_session()
        session.headers["Cookie"] = cookie
        self._applied_cookie = cookie
        logger.info("已应用快手账号 Cookie（长度 %d），可查询任意指定主播", len(cookie))

    def _cookie_dict(self) -> Dict[str, str]:
        """把配置的 Cookie 字符串解析为字典（供浏览器上下文注入）。"""
        result: Dict[str, str] = {}
        for part in self._user_cookie().split(";"):
            if "=" not in part:
                continue
            name, _, value = part.partition("=")
            name, value = name.strip(), value.strip()
            if name:
                result[name] = value
        return result

    @property
    def _reco_pages(self) -> int:
        """推荐流拉取页数（可在设置中调整）。"""
        try:
            return max(1, min(10, int(getattr(self.config, "kuaishou_reco_pages", 3) or 3)))
        except (TypeError, ValueError):
            return 3

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _normalize_id(text: str) -> Optional[str]:
        """从字符串中提取 principalId（字母数字下划线组合，长度 >= 5）。"""
        match = re.search(r"([A-Za-z0-9_-]{5,})", text or "")
        return match.group(1) if match else None

    @staticmethod
    def _looks_like_play_url(url: str) -> bool:
        """判断一个字符串是否像可拉流的地址。"""
        if not isinstance(url, str) or not url.startswith("http"):
            return False
        lowered = url.lower()
        # 排除封面图、头像等静态资源
        if any(ext in lowered for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
            return False
        return ".flv" in lowered or ".m3u8" in lowered or ".ts" in lowered

    #: 快手的画质枚举 -> 中文档位。用于把 ``qualityType`` 显示成可读文案。
    _QUALITY_TYPE_LABELS = {
        "BLUE_RAY": "原画",
        "SUPER": "超清",
        "HIGH": "高清",
        "STANDARD": "标清",
        "LOW": "流畅",
    }

    # ------------------------------------------------------------- 房间解析
    def resolve_room(self, raw_input: str) -> str:
        """解析分享链接或房间号，返回 principalId。

        支持：
        - 纯用户 ID            ``3xf8dh7tqmd9bps``
        - 网页直播间地址        ``https://live.kuaishou.com/u/3xf8dh7tqmd9bps``
        - 分享短链             ``https://v.kuaishou.com/abcdEF``
        - 含口令的整段分享文本

        重要：``efid`` 参数**不是**主播 ID
        -------------------------------
        分享短链重定向后的地址形如::

            https://live.kuaishou.com/u/NingXinHui716?cc=share_copylink
                &...&efid=3xfapsw9azs7yiq&userId=1903992145&shareObjectId=tE_6iEdQEtU

        其中 ``efid`` 是**分享者**的 ID（同一个人分享不同主播时会保持一致），
        ``userId`` 是数字 uid，``shareObjectId`` 是直播流 ID。
        只有**路径** ``/u/<principalId>`` 才是主播的公开 ID，也是接口所需
        ``principalId``。早期版本误用了 ``efid``，导致「解析出的房间号永远是
        分享者自己的」，进而取流必败。
        """
        text = (raw_input or "").strip()
        if not text:
            raise PlatformError("输入为空，无法解析快手房间号")

        url = extract_url(text)
        if not url:
            room_id = self._normalize_id(text)
            if room_id:
                return room_id
            raise PlatformError(f"无法从输入中识别快手用户 ID：{raw_input}")

        parsed = urlparse(url)
        host = parsed.netloc.lower()

        # 短链跟随重定向
        if "v.kuaishou.com" in host or "kuaishou.com/s/" in url:
            url = self._follow_redirect(url)
            parsed = urlparse(url)

        # 1) 最高优先级：路径中的 /u/<id> 与 /profile/<id>
        match = re.search(r"/(?:u|profile)/([A-Za-z0-9_-]{5,})", parsed.path)
        if match:
            logger.debug("快手从路径取到 principalId=%s", match.group(1))
            return match.group(1)

        # 2) 其次：查询参数里的 principalId（明确命名，可信）
        query = parse_qs(parsed.query)
        value = (query.get("principalId") or [""])[0]
        if re.fullmatch(r"[A-Za-z0-9_-]{5,}", value or ""):
            logger.debug("快手从查询参数取到 principalId=%s", value)
            return value

        # 3) 再次：/fw/live/<id> 路径。该路径在移动分享页里承载 principalId，
        #    但也可能出现在流地址中，因此优先级低于上面两种。
        match = re.search(r"/fw/live/([A-Za-z0-9_-]{5,})", parsed.path)
        if match:
            logger.debug("快手从 /fw/live/ 路径取到 principalId=%s", match.group(1))
            return match.group(1)

        # 兜底：整串里找合法 ID。
        # 注意这里刻意不使用 efid —— 它是分享者 ID，会造成「录的是分享者的直播间」。
        room_id = self._normalize_id(parsed.path or url)
        if room_id:
            return room_id
        raise PlatformError(f"无法从链接中解析快手用户 ID：{url}")

    def _follow_redirect(self, url: str) -> str:
        """跟随短链重定向，返回最终地址。"""
        try:
            response = self.request(
                "GET", url, expect_json=False, retries=2, referer=self.HOME_URL
            )
            return str(response.url)
        except PlatformError as exc:
            logger.debug("快手短链重定向失败：%s", exc)
            return url

    def _ensure_cookies(self) -> None:
        """访问首页拿 did 等 Cookie，降低风控概率。"""
        session = self._get_session()
        if len(session.cookies.jar) > 0:
            return
        try:
            self.request(
                "GET", self.HOME_URL, expect_json=False, retries=2, referer=None
            )
        except PlatformError as exc:
            logger.debug("快手首页 Cookie 获取失败：%s", exc)

    # --------------------------------------------------------- 推荐流索引
    def _load_reco_index(self, *, force: bool = False) -> Dict[str, dict]:
        """拉取推荐流，建立 ``principalId -> 房间信息`` 索引。

        为什么需要它
        ------------
        快手的按房间查询接口（``livedetail`` / ``profile/public``）现已全部失效，
        浏览器兜底又会遭遇风控。而推荐流接口仍能返回**在播房间的完整流地址**，
        因此把它作为首选数据源：
        - 命中时无需启动浏览器（快、不触发风控）；
        - 未命中时再退回浏览器兜底。

        索引对所有房间共用并有 TTL，监控多个房间时开销可忽略。

        返回的每个值形如::

            {"stream": StreamInfo, "nickname": str, "title": str,
             "live_stream_id": str, "raw": dict}
        """
        now = time.monotonic()
        if (
            not force
            and self._reco_index
            and now - self._reco_index_at < self.RECO_CACHE_TTL
        ):
            return self._reco_index

        with self._reco_lock:
            # 双重检查：并发检测时可能已有其它线程完成了刷新
            now = time.monotonic()
            if (
                not force
                and self._reco_index
                and now - self._reco_index_at < self.RECO_CACHE_TTL
            ):
                return self._reco_index

            index = self._fetch_reco_pages()
            if index:
                self._reco_index = index
                self._reco_index_at = now
                logger.info("快手推荐流索引已更新：%d 个在播房间", len(index))
            return self._reco_index

    def _fetch_reco_pages(self) -> Dict[str, dict]:
        """实际拉取推荐流各页并构建索引（调用方负责加锁）。"""
        self._apply_user_cookie()
        index: Dict[str, dict] = {}
        for page in range(1, self._reco_pages + 1):
            try:
                payload = self.request(
                    "POST",
                    self.RECO_API,
                    data=json.dumps({"source": 2, "page": page, "pageSize": 20}),
                    headers={"Content-Type": "application/json"},
                    referer=self.HOME_URL,
                    retries=1,
                )
            except RiskControlError as exc:
                logger.warning("快手推荐流被风控拦截：%s", exc)
                break
            except PlatformError as exc:
                logger.debug("快手推荐流拉取失败（第 %d 页）：%s", page, exc)
                break

            if not isinstance(payload, dict):
                break
            items = (payload.get("data") or {}).get("list") or []
            if not isinstance(items, list):
                break

            for item in items:
                if not isinstance(item, dict):
                    continue
                entry = self._reco_entry_to_info(item)
                if entry:
                    index[entry["principal_id"]] = entry
            logger.debug("快手推荐流第 %d 页解析出 %d 条", page, len(items))

        return index

    def _reco_entry_to_info(self, item: dict) -> Optional[dict]:
        """把推荐流的一条记录转换为索引条目。"""
        author = item.get("author") or {}
        live_stream = item.get("liveStream") or {}
        principal_id = author.get("id") or author.get("principalId")
        if not principal_id:
            return None
        if item.get("isLiving") is False:
            return None

        candidates = self._collect_stream_candidates(live_stream.get("playUrls") or {})
        if not candidates:
            # 没有可用流地址的记录不纳入索引（避免「显示在播却录不了」）
            return None

        candidates.sort(key=lambda it: (it[1], it[2]), reverse=True)
        best = candidates[0]
        stream_format = (
            StreamFormat.HLS if ".m3u8" in best[0].lower() else StreamFormat.FLV
        )
        stream = StreamInfo(
            url=best[0],
            stream_format=stream_format,
            quality=StreamQuality.ORIGIN,
            quality_label=best[3] or (f"{best[1]}P" if best[1] else "origin"),
            headers=self._stream_headers(str(principal_id)),
        )
        return {
            "principal_id": str(principal_id),
            "candidates": candidates,
            "stream": stream,
            "nickname": str(author.get("name") or ""),
            "title": str(
                live_stream.get("caption") or live_stream.get("title") or ""
            ),
            "live_stream_id": str(live_stream.get("id") or ""),
            "raw": item,
        }

    # ------------------------------------------------------------- 开播检测
    def check_live(self, room_id: str) -> RoomInfo:
        """检测开播状态。

        顺序：
        1. **推荐流索引**（纯 HTTP，快且不触发浏览器风控）；
        2. 旧详情接口（多已失效，仅在未标记降级时尝试一次）；
        3. 浏览器兜底（并区分「未开播」与「被风控拦截」）。
        """
        room = RoomInfo(platform=self.platform, room_id=room_id)

        # 1) 推荐流命中
        index = self._load_reco_index()
        entry = index.get(room_id)
        if entry is not None:
            room.status = LiveStatus.LIVE
            room.nickname = entry.get("nickname") or ""
            room.title = entry.get("title") or ""
            room.note = "直播中"
            return room

        # 2) 旧详情接口
        if not self._api_degraded:
            payload = None
            try:
                payload = self._fetch_room_payload(room_id)
            except RiskControlError as exc:
                logger.warning("快手房间 %s 触发风控：%s", room_id, exc)
                room.status = LiveStatus.UNKNOWN
                room.note = f"触发风控：{exc}"
                return room
            except PlatformError as exc:
                logger.warning("快手房间 %s 状态检测失败：%s", room_id, exc)
                room.status = LiveStatus.UNKNOWN
                room.note = str(exc)
                return room

            # _fetch_room_payload 内部若识别为占位响应，会置位 _api_degraded
            if not self._api_degraded and payload is not None:
                room.raw = payload
                self._fill_room_info(room, payload)
                return room

        # 3) 浏览器兜底
        return self._check_via_browser(room)

    def _check_via_browser(self, room: RoomInfo) -> RoomInfo:
        """用真实浏览器判定开播状态。

        关键：**必须区分「被风控」与「未开播」**。
        被风控的页面同样抓不到流，但它还会播放推荐位内容——
        若不识别，就会出现「明明在直播却显示未开播」甚至「录到别人的直播」。
        """
        info = self.probe_stream_via_browser(room.room_id)
        if info is not None:
            room.status = LiveStatus.LIVE
            room.note = "直播中（浏览器确认）"
            return room

        if self._last_probe_blocked:
            room.status = LiveStatus.UNKNOWN
            room.note = (
                "被快手风控拦截（页面提示「请求过快」或出现验证码）。"
                "请把轮询间隔调大到 120 秒以上，稍后重试"
            )
            return room

        if self._browser_unavailable_reason:
            room.status = LiveStatus.UNKNOWN
            room.note = self._browser_unavailable_reason
            return room

        room.status = LiveStatus.OFFLINE
        room.note = "未开播（浏览器未捕获到直播流）"
        return room

    def _fetch_room_payload(self, room_id: str) -> dict:
        """请求房间详情接口。

        识别到占位响应（接口已失效）时置位 ``_api_degraded`` 并只告警一次。
        """
        self._apply_user_cookie()
        self._ensure_cookies()
        referer = f"https://live.kuaishou.com/u/{room_id}"
        params = {"principalId": room_id, "source": "1", "subBiz": "living"}

        payload = self.request(
            "GET", self.DETAIL_API, params=params, referer=referer
        )
        if not isinstance(payload, dict):
            raise PlatformError("快手 livedetail 返回非 JSON 对象")

        data = payload.get("data")
        if not isinstance(data, dict) or not data:
            raise PlatformError("快手 livedetail 未返回 data 节点")

        # 失效识别：占位响应的特征是「既没有作者信息，也没有任何真实流地址」。
        # 实测该响应与 principalId 完全无关（乱码 id 也返回同一份），
        # 因此不能据此判定未开播，只能判定「接口不可用」。
        author = data.get("author") or {}
        has_author = bool(author.get("name") or author.get("id"))
        if not has_author and not self._collect_stream_candidates(data):
            self._mark_api_degraded(room_id)

        return payload

    def _mark_api_degraded(self, room_id: str) -> None:
        """标记 HTTP 详情接口已失效（只告警一次，避免刷屏）。"""
        if self._api_degraded:
            return
        self._api_degraded = True
        logger.warning(
            "[接口变更] 快手 livedetail 对任意房间号都返回同一份占位响应"
            "（无作者信息、无流地址），判定该接口已失效。"
            "后续将改用浏览器兜底获取状态与流地址，"
            "这会明显增加单次检测耗时，建议适当调大轮询间隔（如 120 秒）。"
        )

    def _fill_room_info(self, room: RoomInfo, payload: dict) -> None:
        """从接口响应填充 RoomInfo。

        开播判定顺序：显式标志位 → 是否存在真实可拉流地址。
        **不可**使用 ``bool(playUrls)``：未开播时该字段是
        ``{"h264": {}, "hevc": {}}``，布尔值为 True，会造成误判。
        """
        data = payload.get("data") or {}
        live_stream = data.get("liveStream") or {}
        author = data.get("author") or {}

        # 昵称
        room.nickname = str(
            author.get("name")
            or dig(live_stream, "user", "name")
            or dig(data, "liveStream", "author", "name")
            or room.nickname
            or ""
        )
        room.title = str(
            data.get("caption")
            or live_stream.get("caption")
            or dig(data, "liveStream", "title")
            or ""
        )
        room.cover = str(
            live_stream.get("coverUrl")
            or live_stream.get("poster")
            or dig(data, "liveStream", "cover")
            or ""
        )

        real_urls = self._collect_stream_candidates(data)

        # 1) 优先使用显式开播标志位
        is_living: Optional[bool] = None
        for flag in (
            author.get("living"),
            live_stream.get("living"),
            data.get("isLiving"),
        ):
            if isinstance(flag, bool):
                is_living = flag
                break

        # 2) 没有标志位时，以「是否存在真实流地址」为准
        if is_living is None:
            is_living = bool(real_urls)
        elif is_living is False and real_urls:
            # 标志位与流地址冲突时，以「确实有流」为准（避免漏录）
            is_living = True

        room.status = LiveStatus.LIVE if is_living else LiveStatus.OFFLINE
        if not is_living:
            room.note = "未开播"

        count = live_stream.get("userCount") or data.get("userCount") or 0
        try:
            room.online_count = int(count)
        except (TypeError, ValueError):
            room.online_count = 0

    # --------------------------------------------------------------- 流地址
    def fetch_stream(
        self, room_id: str, quality: StreamQuality = StreamQuality.ORIGIN
    ) -> StreamInfo:
        """获取指定画质的拉流地址。

        顺序与 ``check_live`` 一致：推荐流索引 → 旧详情接口 → 浏览器兜底。
        """
        # 1) 推荐流索引命中（纯 HTTP，最快）
        entry = self._load_reco_index().get(room_id)
        if entry is not None:
            logger.debug("快手房间 %s 从推荐流索引命中流地址", room_id)
            return self._pick_stream(entry["candidates"], room_id, quality)

        # 2) 旧详情接口（多已失效）
        candidates: List[Tuple[str, int, int, str]] = []
        if not self._api_degraded:
            try:
                payload = self._fetch_room_payload(room_id)
                candidates = self._collect_stream_candidates(payload)
            except RiskControlError:
                raise
            except PlatformError as exc:
                logger.debug("快手 livedetail 取流失败：%s", exc)

        if not candidates:
            # 3) 浏览器兜底（结果会缓存，供紧随其后的 check_live 复用）
            info = self.probe_stream_via_browser(room_id)
            if info is not None:
                if quality is StreamQuality.HIGH:
                    info.quality = StreamQuality.HIGH
                return info
            if self._last_probe_blocked:
                raise RiskControlError(
                    f"快手房间 {room_id} 取流被风控拦截（页面提示「请求过快」"
                    f"或出现验证码），请调大轮询间隔后重试"
                )
            reason = self._browser_unavailable_reason or "浏览器未捕获到直播流（可能未开播）"
            raise PlatformError(f"快手房间 {room_id} 无法获取可用流地址：{reason}")

        # 按（高度, 码率）降序排列，取最接近目标档位的地址
        return self._pick_stream(candidates, room_id, quality)

    def _pick_stream(
        self,
        candidates: List[Tuple[str, int, int, str]],
        room_id: str,
        quality: StreamQuality,
    ) -> StreamInfo:
        """从候选地址中按画质偏好挑出一条，构造 StreamInfo。"""
        candidates.sort(key=lambda item: (item[1], item[2]), reverse=True)

        if quality is StreamQuality.ORIGIN:
            url, height, _bitrate, label = candidates[0]
        else:
            # 超清：优先接近 720p 的最高档
            target = 720
            url, height, _bitrate, label = min(
                candidates, key=lambda item: abs(item[1] - target) if item[1] else 10 ** 6
            )

        stream_format = StreamFormat.HLS if ".m3u8" in url.lower() else StreamFormat.FLV
        return StreamInfo(
            url=url,
            stream_format=stream_format,
            quality=quality,
            quality_label=(label or f"{height}P") if (label or height) else quality.value,
            headers=self._stream_headers(room_id),
        )

    def _collect_stream_candidates(
        self, payload: dict
    ) -> List[Tuple[str, int, int, str]]:
        """递归遍历响应，收集所有候选播放地址。

        返回 ``[(url, height, bitrate, label), ...]``。
        之所以递归收集，是因为快手不同接口版本的流地址嵌套层级不一致，
        递归方式对 schema 微调更健壮。
        """
        results: List[Tuple[str, int, int, str]] = []
        seen: set = set()

        def walk(node, meta: Dict) -> None:
            if isinstance(node, dict):
                # 收集本层的画质描述信息，向下传递
                local_meta = dict(meta)
                for key in ("name", "quality", "label", "definition"):
                    if isinstance(node.get(key), str):
                        local_meta["label"] = node[key]
                # 快手的 representation 用 qualityType 表达档位（英文枚举）
                quality_type = node.get("qualityType")
                if isinstance(quality_type, str) and quality_type:
                    local_meta["label"] = self._QUALITY_TYPE_LABELS.get(
                        quality_type.upper(), quality_type
                    )
                for key in ("height", "heightLevel", "resolution"):
                    if isinstance(node.get(key), (int, float)):
                        local_meta["height"] = int(node[key])
                for key in ("bitrate", "avgBitrate", "bandwidth"):
                    if isinstance(node.get(key), (int, float)):
                        local_meta["bitrate"] = int(node[key])

                # 本层的 url 字段
                for key in ("url", "playUrl", "src", "address"):
                    value = node.get(key)
                    if self._looks_like_play_url(value) and value not in seen:
                        seen.add(value)
                        results.append(
                            (
                                value,
                                int(local_meta.get("height") or 0),
                                int(local_meta.get("bitrate") or 0),
                                str(local_meta.get("label") or ""),
                            )
                        )
                # 继续深入
                for value in node.values():
                    walk(value, local_meta)
            elif isinstance(node, (list, tuple)):
                for item in node:
                    walk(item, meta)
            elif isinstance(node, str):
                # 直接就是 URL 的情况
                if self._looks_like_play_url(node) and node not in seen:
                    seen.add(node)
                    results.append(
                        (
                            node,
                            int(meta.get("height") or 0),
                            int(meta.get("bitrate") or 0),
                            str(meta.get("label") or ""),
                        )
                    )

        walk(payload, {})
        return results

    def _stream_headers(self, room_id: str) -> Dict[str, str]:
        """生成 ffmpeg 拉流所需请求头（含 Cookie）。"""
        session = self._get_session()
        cookie_str = "; ".join(f"{c.name}={c.value}" for c in session.cookies.jar)
        headers = {
            "User-Agent": self._profile["User-Agent"],
            "Referer": f"https://live.kuaishou.com/u/{room_id}",
        }
        if cookie_str:
            headers["Cookie"] = cookie_str
        return headers

    # ------------------------------------------------------- Playwright 兜底
    def probe_stream_via_browser(
        self, room_id: str, wait_seconds: float = 15.0
    ) -> Optional[StreamInfo]:
        """用真实浏览器打开直播间页面，直接抓取当前房间的播放地址。

        HTTP 详情接口已失效，而推荐流只能覆盖部分房间，因此浏览器是最后的兜底手段。

        三项关键防护（缺一不可）：
        1. **预热**：先在同一会话打开站点首页，建立 Cookie 与站点状态后再进直播间，
           实测可显著降低被判定为异常访问的概率；
        2. **风控识别**：页面出现「请求过快」/验证码时，明确标记为风控
           （``_last_probe_blocked``），避免被上层误判为「未开播」。此时页面上
           可能仍在播放推荐位内容，若不拦截就会录到别人的直播；
        3. **落地校验**：最终的页面地址必须仍属于目标房间，否则丢弃抓取结果。

        返回 ``None`` 表示未抓到（未开播、被风控或浏览器不可用）。
        """
        cached = self._browser_cache.get(room_id)
        if cached is not None:
            ttl = self.BLOCKED_CACHE_TTL if cached[2] else self.BROWSER_CACHE_TTL
            if time.monotonic() - cached[0] < ttl:
                self._last_probe_blocked = cached[2]
                return cached[1]

        from .playwright_helper import BrowserSession, PlaywrightUnavailable

        page_url = f"https://live.kuaishou.com/u/{room_id}"
        info: Optional[StreamInfo] = None
        blocked = False
        landed = ""
        page_text = ""
        try:
            with BrowserSession(
                headless=True,
                cookies=self._cookie_dict(),
                cookie_domain=".kuaishou.com",
            ) as session:
                urls = session.capture_streams(
                    page_url,
                    wait_seconds=wait_seconds,
                    # 只认真正的流地址，避免把封面等静态资源当流
                    predicate=lambda u: ".flv" in u.lower() or ".m3u8" in u.lower(),
                    host_hints=self._CDN_HINTS,
                    # 先访问首页建立会话状态（实测能明显降低风控概率）
                    warmup_url=self.HOME_URL,
                )
                cookies = session.cookies_dict()
                blocked = session.page_blocked()
                landed = session.final_url()
                page_text = session.page_text()
        except PlaywrightUnavailable as exc:
            self._browser_unavailable_reason = (
                f"浏览器兜底不可用（{exc}）。请执行："
                f".venv\\Scripts\\playwright install chromium"
            )
            logger.warning("快手浏览器兜底不可用：%s", exc)
            self._browser_cache[room_id] = (time.monotonic(), None, False)
            return None
        except Exception as exc:  # noqa: BLE001 - 兜底失败不应中断监控
            self._browser_unavailable_reason = f"浏览器兜底异常：{exc}"
            logger.warning("快手浏览器兜底异常：%s", exc)
            self._browser_cache[room_id] = (time.monotonic(), None, False)
            return None

        # 浏览器可用，清掉不可用原因
        self._browser_unavailable_reason = ""
        self._last_probe_blocked = blocked

        if blocked:
            logger.warning(
                "快手房间 %s 页面被风控拦截，本轮不采信抓取结果（页面片段：%r）",
                room_id,
                (page_text or "")[:100],
            )
            urls = []

        # 落地校验：页面若已跳转到别处（或被跳到验证页），抓到的流不可信
        if urls and landed and room_id not in landed:
            logger.warning(
                "快手房间 %s 页面落地地址为 %s，与目标房间不符，丢弃抓取结果",
                room_id,
                landed[:120],
            )
            urls = []

        if urls:
            # capture_streams 已按可信度排序：media 类型 > CDN 域名 > 出现顺序
            target = urls[0]
            is_hls = ".m3u8" in target.lower()
            headers = {
                "User-Agent": self._profile["User-Agent"],
                "Referer": page_url,
            }
            if cookies:
                headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
            info = StreamInfo(
                url=target,
                stream_format=StreamFormat.HLS if is_hls else StreamFormat.FLV,
                quality=StreamQuality.ORIGIN,
                quality_label="browser",
                headers=headers,
            )
            logger.info(
                "快手房间 %s 浏览器兜底成功（%s，共捕获 %d 个候选）",
                room_id,
                "HLS" if is_hls else "FLV",
                len(urls),
            )

        self._browser_cache[room_id] = (time.monotonic(), info, blocked)
        return info

    def capture_stream_via_browser(
        self, room_id: str, wait_seconds: float = 12.0
    ) -> Optional[StreamInfo]:
        """``registry.resolve_stream`` 三级降级的统一入口别名。"""
        return self.probe_stream_via_browser(room_id, wait_seconds=wait_seconds)
