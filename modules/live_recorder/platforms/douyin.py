# -*- coding: utf-8 -*-
"""
抖音（Douyin）直播解析器

关键点
------
1. 网页端直播间地址形如 ``https://live.douyin.com/<web_rid>``，
   分享短链形如 ``https://v.douyin.com/xxxx/``，需先跟随重定向。
2. **``web_rid`` 与 ``room_id`` 是两套完全不同的标识，不可混用**：
   - ``web_rid`` 约 10~13 位，是网页端直播间地址与 enter 接口使用的标识；
   - ``room_id`` 为 19 位长整数，分享链接解析后只能拿到它。
   把 ``room_id`` 当作 ``web_rid`` 传给 enter 接口，会固定得到
   ``status_code=4001038``（「该内容暂时无法查看」），表现为「怎么都录不上」。
   本模块通过解析分享页 ``/douyin/webcast/reflow/<room_id>`` 内嵌的
   ``"webRid":"..."`` 字段完成换算（详见 ``_web_rid_from_room_id``）。
3. 开播状态与流地址来自 ``/webcast/room/web/enter/`` 接口。
   **该接口必须携带首访 Cookie（``ttwid``），否则会稳定返回「HTTP 200 +
   空响应体」**——这是本模块最容易踩的坑：
   - 空体不是风控，而是缺 Cookie，补一次 ``live.douyin.com`` 首页访问即可；
   - 且 ``ttwid`` 必须属于 ``.douyin.com`` 主域。分享页 ``webcast.amemv.com``
     会写入一个**同名但不同域**的 ``ttwid``，它不会随请求发往 live.douyin.com，
     所以「jar 里存在 ttwid」不能作为已就绪的判据（见 ``_ensure_cookies``）。
   另外该接口需要动态签名（a_bogus），实测：只要 ``web_rid`` 与 Cookie 正确，
   **不带 a_bogus 也能取到数据**；若被风控则再走 Playwright 兜底
   （真实浏览器打开页面直接抓播放地址）。

接口若发生变更，仅需修改本文件中的 ``WEB_ENTER_API`` / ``REFLOW_API`` 与字段解析路径。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional
from urllib.parse import urlparse

from ..utils.logger import get_logger
from .base import (
    BaseLiveParser,
    EmptyResponseError,
    LiveStatus,
    PlatformError,
    RiskControlError,
    RoomInfo,
    RoomUnavailableError,
    StreamFormat,
    StreamInfo,
    StreamQuality,
)
from .utils import dig, extract_url

logger = get_logger(__name__)


class DouyinParser(BaseLiveParser):
    """抖音直播解析器。"""

    platform = "douyin"
    display_name = "抖音"

    def __init__(self, config=None) -> None:
        super().__init__(config)
        # room_id -> web_rid 换算结果缓存。
        # 一是避免同一个长房间号反复请求分享页；二是早期版本把 19 位 room_id
        # 直接存进了配置，这里做一次规范化即可让历史配置继续可用。
        self._rid_cache: Dict[str, str] = {}
        # 已注入会话的 Cookie（避免重复注入）
        self._applied_cookie = ""

    HOME_URL = "https://live.douyin.com/"
    WEB_ENTER_API = "https://live.douyin.com/webcast/room/web/enter/"
    # 分享页：内嵌 room 数据与 webRid 字段，用于把 room_id 换算为 web_rid
    REFLOW_API = "https://webcast.amemv.com/douyin/webcast/reflow/{room_id}"

    # 网页端通用参数（固定值，来源为官方 web 端）
    ENTER_BASE_PARAMS = {
        "aid": "6383",
        "app_name": "douyin_web",
        "live_id": "1",
        "device_platform": "web",
        "language": "zh-CN",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Chrome",
        "browser_version": "126.0.0.0",
        "os_name": "Windows",
        "os_version": "10",
        "cookie_enabled": "true",
    }

    # 画质优先顺序（原画 -> 超清）
    ORIGIN_KEYS = ["FULL_HD1", "ORIGIN", "origin", "HD1", "hd_1"]
    HIGH_KEYS = ["HD1", "hd_1", "SD1", "sd_1", "FULL_HD1"]

    # 抖音业务错误码：房间不存在 / 已结束 / 内容受限
    UNAVAILABLE_CODES = {
        4001038,   # 「该内容暂时无法查看」——分享链接过期、房间已结束或内容受限
    }
    # 超过该位数即认为是 room_id 而非 web_rid
    ROOM_ID_MIN_LEN = 17

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _digits(text: str) -> Optional[str]:
        """抽取字符串中的连续数字。"""
        match = re.search(r"(\d{5,})", text or "")
        return match.group(1) if match else None

    def _coerce_rid(self, value: str) -> str:
        """把用户输入的数字标识规范化为 enter 接口所需的 web_rid。

        19 位的房间号（room_id）会被自动换算为 web_rid；换算失败时原样返回，
        由后续接口给出明确报错，避免「静默失败」。
        """
        digits = re.sub(r"\D", "", value or "")
        if not digits:
            raise PlatformError(f"无法识别抖音房间号：{value!r}")
        if len(digits) >= self.ROOM_ID_MIN_LEN:
            return self._normalize_rid(digits)
        return digits

    def _normalize_rid(self, value: str) -> str:
        """把可能混入的 19 位 room_id 统一换算为 web_rid（带缓存）。

        为什么需要它：``check_live`` / ``fetch_stream`` 的入参来自监控清单，
        而清单中的数据可能是早期版本保存的 19 位 room_id。若直接当作
        ``web_rid`` 传给 enter 接口，会固定得到 ``status_code=4001038``
        （「该内容暂时无法查看」），表现为「这个房间永远录不上」。
        """
        key = str(value or "").strip()
        if not key.isdigit() or len(key) < self.ROOM_ID_MIN_LEN:
            return key
        if key in self._rid_cache:
            return self._rid_cache[key]
        resolved = self._web_rid_from_room_id(key)
        self._rid_cache[key] = resolved
        return resolved

    def _web_rid_from_room_id(self, room_id: str) -> str:
        """把 19 位 room_id 换算成网页端 web_rid。

        换算依据：分享页 ``webcast.amemv.com/douyin/webcast/reflow/<room_id>``
        的 HTML 中内嵌了 ``"webRid":"<web_rid>"``，无需签名即可纯 HTTP 获取。

        参数
        ----
        room_id: 分享链接解析出的长房间号

        返回
        ----
        web_rid；换算失败时返回原 room_id（由调用方接口给出明确错误）。
        """
        try:
            response = self.request(
                "GET",
                self.REFLOW_API.format(room_id=room_id),
                expect_json=False,
                retries=2,
                referer="https://www.douyin.com/",
            )
            html = response.text or ""
        except PlatformError as exc:
            logger.warning(
                "抖音 room_id %s 换算 web_rid 失败（%s），将按原值尝试", room_id, exc
            )
            return room_id

        # 页面里该字段可能是明文也可能被 JSON 转义（\"webRid\":\"123\"），
        # 因此用「关键字 + 少量非数字字符 + 数字」的宽松匹配。
        for pattern in (
            r"webRid\D{0,6}(\d{6,})",
            r'"web_rid"\D{0,6}(\d{6,})',
            r"webRid%22%3A%22(\d{6,})",
        ):
            match = re.search(pattern, html)
            if match:
                web_rid = match.group(1)
                logger.info(
                    "抖音 room_id %s 已换算为 web_rid %s", room_id, web_rid
                )
                return web_rid

        logger.warning(
            "抖音分享页未找到 webRid 字段（room_id=%s），将按原值尝试", room_id
        )
        return room_id

    # ------------------------------------------------------------- 房间解析
    def resolve_room(self, raw_input: str) -> str:
        """解析分享链接或房间号，返回 enter 接口所需的 web_rid。

        支持：
        - 纯房间号            ``123456789``
        - 网页直播间地址       ``https://live.douyin.com/123456789``
        - 分享短链            ``https://v.douyin.com/abcdEF/``
        - 含口令的整段分享文本
        """
        text = (raw_input or "").strip()
        if not text:
            raise PlatformError("输入为空，无法解析抖音房间号")

        # 纯数字：短的是 web_rid，19 位的是 room_id（内部会换算）
        if text.isdigit():
            return self._coerce_rid(text)

        url = extract_url(text)
        if not url:
            rid = self._digits(text)
            if rid:
                return self._coerce_rid(rid)
            raise PlatformError(f"无法从输入中识别抖音房间号：{raw_input}")

        parsed = urlparse(url)
        host = parsed.netloc.lower()

        # 短链 / 分享链需要跟随重定向
        if any(hint in host for hint in ("v.douyin.com", "iesdouyin.com", "amemv.com")):
            url = self._follow_redirect(url)
            parsed = urlparse(url)
            host = parsed.netloc.lower()

        # 分享页：webcast.amemv.com/douyin/webcast/reflow/<room_id>
        match = re.search(r"/reflow/(\d+)", parsed.path)
        if match:
            return self._coerce_rid(match.group(1))

        # 标准网页直播间：路径第一段即 web_rid
        path_segments = [seg for seg in parsed.path.split("/") if seg]
        if path_segments:
            rid = self._digits(path_segments[0])
            if rid:
                return self._coerce_rid(rid)

        # 兜底：从整个 URL 中抓数字
        rid = self._digits(url)
        if rid:
            return self._coerce_rid(rid)
        raise PlatformError(f"无法从链接中解析抖音房间号：{url}")

    def _follow_redirect(self, url: str) -> str:
        """跟随短链重定向，返回最终地址。"""
        try:
            response = self.request(
                "GET", url, expect_json=False, retries=2, referer=self.HOME_URL
            )
            return str(response.url)
        except PlatformError as exc:
            logger.debug("抖音短链重定向失败：%s", exc)
            return url

    # Cookie 主域后缀，用于校验 ttwid 是否真的会随请求发往 live.douyin.com
    COOKIE_DOMAIN = "douyin.com"

    @staticmethod
    def _has_domain_cookie(session, name: str, domain_suffix: str) -> bool:
        """判断会话中是否存在**属于指定域**的某个 Cookie。

        必须校验域：httpx 的 CookieJar 是全局共享的，``webcast.amemv.com``
        写入的同名 ttwid 会被 ``in session.cookies.jar`` 命中，
        但它不会发送到 ``live.douyin.com``，据此判断会得到错误结论。
        """
        suffix = domain_suffix.lstrip(".")
        for cookie in session.cookies.jar:
            if cookie.name != name:
                continue
            domain = (cookie.domain or "").lstrip(".")
            if domain and domain.endswith(suffix):
                return True
        return False

    def _apply_user_cookie(self) -> None:
        """把用户配置的抖音 Cookie 注入 HTTP 会话（幂等）。

        抖音在未登录时也能解析，此项是可选增强：带上真实账号的 Cookie
        可以让请求特征更接近普通用户，降低被风控的概率。
        """
        cookie = str(getattr(self.config, "douyin_cookie", "") or "").strip()
        if not cookie or cookie == self._applied_cookie:
            return
        session = self._get_session()
        session.headers["Cookie"] = cookie
        self._applied_cookie = cookie
        logger.info("已应用抖音账号 Cookie（长度 %d）", len(cookie))

    def _ensure_cookies(self, *, force: bool = False) -> None:
        """访问一次抖音主域首页，获取 ttwid / UIFID 等必要 Cookie。

        参数
        ----
        force: True 时强制重新访问首页（用于「空响应」后的自救重试）
        """
        self._apply_user_cookie()
        session = self._get_session()
        if not force and self._has_domain_cookie(
            session, "ttwid", self.COOKIE_DOMAIN
        ):
            return
        try:
            self.request(
                "GET", self.HOME_URL, expect_json=False, retries=2, referer=None
            )
            logger.debug(
                "抖音首访 Cookie 已获取：%s",
                [c.name for c in session.cookies.jar],
            )
        except PlatformError as exc:
            logger.debug("抖音首页 Cookie 获取失败：%s", exc)

    # ------------------------------------------------------------- 开播检测
    def check_live(self, room_id: str) -> RoomInfo:
        """调用 enter 接口检测开播状态。

        状态判定规则
        ------------
        - 业务错误码命中 ``UNAVAILABLE_CODES`` → ``OFFLINE``
          （房间不存在 / 已结束 / 内容受限，属于「没得录」而不是「网络问题」）
        - 风控 / 网络异常 → ``UNKNOWN``（保持现状，避免误判为下播而中断录制）
        """
        room_id = self._normalize_rid(room_id)
        room = RoomInfo(platform=self.platform, room_id=room_id)
        try:
            payload = self._fetch_room_payload(room_id, allow_unavailable=True)
        except RiskControlError as exc:
            logger.warning("抖音房间 %s 触发风控：%s", room_id, exc)
            room.status = LiveStatus.UNKNOWN
            room.note = f"触发风控：{exc}"
            return room
        except PlatformError as exc:
            logger.warning("抖音房间 %s 状态检测失败：%s", room_id, exc)
            room.status = LiveStatus.UNKNOWN
            room.note = str(exc)
            return room

        room.raw = payload or {}
        self._fill_room_info(room, payload)
        return room

    def _fetch_room_payload(
        self, room_id: str, *, allow_unavailable: bool = False
    ) -> dict:
        """请求 enter 接口，返回 room 节点所在的对象。

        参数
        ----
        allow_unavailable:
            True 时把「房间不可观看」这类业务错误转成
            ``{"__unavailable__": True, ...}`` 返回，供状态检测判定为未开播；
            False（取流场景）时抛出 ``RoomUnavailableError``。
        """
        params = dict(self.ENTER_BASE_PARAMS)
        params.update({"web_rid": room_id, "enter_from": "web_live"})

        payload = None
        # 最多两次：第一次用现有 Cookie，若返回空体则强制补抓首访 Cookie 再试一次
        # （抖音在缺少 ttwid 时正是「200 + 空体」，属于可自愈情况）
        for attempt in (1, 2):
            self._ensure_cookies(force=attempt == 2)
            try:
                payload = self.request(
                    "GET",
                    self.WEB_ENTER_API,
                    params=params,
                    referer=f"https://live.douyin.com/{room_id}",
                )
                break
            except EmptyResponseError as exc:
                if attempt == 1:
                    logger.info(
                        "抖音 enter 返回空响应（房间 %s），补抓首访 Cookie 后重试一次",
                        room_id,
                    )
                    continue
                raise
        if not isinstance(payload, dict):
            raise PlatformError("抖音 enter 接口返回格式异常")

        status_code = payload.get("status_code", 0)
        data_node = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        prompts = str((data_node or {}).get("prompts") or "")

        if status_code not in (0, None):
            if status_code in self.UNAVAILABLE_CODES:
                detail = f"status_code={status_code}" + (f"，{prompts}" if prompts else "")
                if allow_unavailable:
                    logger.debug(
                        "抖音房间 %s 不可观看（%s），判定为未开播", room_id, detail
                    )
                    return {
                        "__unavailable__": True,
                        "status_code": status_code,
                        "prompts": prompts,
                    }
                raise RoomUnavailableError(f"抖音房间 {room_id} 不可观看（{detail}）")
            raise PlatformError(
                f"抖音 enter 接口返回 status_code={status_code}"
                + (f"（{prompts}）" if prompts else "")
            )

        # 兼容两种结构：
        # 老版：data.room；新版：data.data[0]
        room_node = dig(payload, "data", "room")
        if not isinstance(room_node, dict) or not room_node:
            candidate = dig(payload, "data", "data", 0)
            if isinstance(candidate, dict) and candidate:
                room_node = candidate
        if isinstance(room_node, dict) and room_node:
            return payload

        # 没有 room 节点：多为房间不存在或未开播（接口仍返回 status_code=0）
        user_node = dig(payload, "data", "user")
        if isinstance(user_node, dict):
            return {"__offline__": True, "user": user_node}
        return {"__offline__": True}

    def _fill_room_info(self, room: RoomInfo, payload: dict) -> None:
        """从接口响应中填充 RoomInfo。"""
        # 房间不可观看（分享链接过期 / 已结束 / 内容受限）
        if payload.get("__unavailable__"):
            prompts = str(payload.get("prompts") or "该内容暂时无法查看")
            room.status = LiveStatus.OFFLINE
            room.note = f"房间不可观看：{prompts}"
            return

        if payload.get("__offline__"):
            user = payload.get("user") or {}
            room.nickname = str(user.get("nickname") or "")
            room.status = LiveStatus.OFFLINE
            room.note = "房间不存在或未开播"
            return

        room_node = dig(payload, "data", "room")
        if not isinstance(room_node, dict):
            room_node = dig(payload, "data", "data", 0) or {}
        user_node = dig(payload, "data", "user") or {}

        # 昵称：优先 room.owner.nickname，其次 user.nickname
        room.nickname = str(
            dig(room_node, "owner", "nickname")
            or dig(user_node, "nickname")
            or room.nickname
            or ""
        )
        room.title = str(dig(room_node, "title") or "")
        room.cover = str(
            dig(room_node, "cover", "url_list", 0)
            or dig(room_node, "cover", "url_list", 1)
            or ""
        )
        # status: 2=直播中，4=已下播
        status_code = dig(room_node, "status")
        if status_code == 2:
            room.status = LiveStatus.LIVE
        elif status_code in (3, 4):
            room.status = LiveStatus.OFFLINE
        else:
            # 部分接口用 live_status / room_status
            live_flag = dig(room_node, "live_status")
            if live_flag in (1, True):
                room.status = LiveStatus.LIVE
            else:
                room.status = LiveStatus.OFFLINE if room_node else LiveStatus.UNKNOWN

        count_text = dig(room_node, "stats", "user_count_str") or dig(
            room_node, "user_count"
        )
        try:
            room.online_count = int(str(count_text).replace("+", "").replace(",", ""))
        except (TypeError, ValueError):
            room.online_count = 0

    # --------------------------------------------------------------- 流地址
    def fetch_stream(
        self, room_id: str, quality: StreamQuality = StreamQuality.ORIGIN
    ) -> StreamInfo:
        """获取指定画质的拉流地址。"""
        room_id = self._normalize_rid(room_id)
        payload = self._fetch_room_payload(room_id)
        if payload.get("__unavailable__"):
            raise RoomUnavailableError(
                f"抖音房间 {room_id} 不可观看（{payload.get('prompts') or '内容受限'}）"
            )
        if payload.get("__offline__"):
            raise RoomUnavailableError(f"抖音房间 {room_id} 未开播，无可用流地址")

        room_node = dig(payload, "data", "room")
        if not isinstance(room_node, dict) or not room_node:
            room_node = dig(payload, "data", "data", 0) or {}

        # 关键：先判开播状态，再找流地址。
        # 已下播时接口仍会返回 room 节点（只是流地址为空），若此时才报
        # 「未找到流地址」——上层会当成「接口异常」而启动浏览器兜底，
        # 而抖音的下播页会自动播放「推荐直播」，最终录到**别的房间**。
        # 因此这里必须直接判定为「房间不可用」，阻断降级与兜底。
        live_flag = dig(room_node, "status")
        if live_flag not in (2, None) and live_flag not in ("2",):
            raise RoomUnavailableError(
                f"抖音房间 {room_id} 未开播（status={live_flag}），无可用流地址"
            )

        stream_url = room_node.get("stream_url") or {}
        if not isinstance(stream_url, dict):
            raise PlatformError("抖音响应中缺少 stream_url 节点")

        # 1) 优先 FLV（原画通常只在 FLV 中提供）
        flv_map = stream_url.get("flv_pull_url") or {}
        if isinstance(flv_map, dict) and flv_map:
            chosen = self._pick_by_quality(
                flv_map, quality, self.ORIGIN_KEYS, self.HIGH_KEYS
            )
            if chosen:
                key, url = chosen
                return StreamInfo(
                    url=url,
                    stream_format=StreamFormat.FLV,
                    quality=StreamQuality.ORIGIN
                    if key in self.ORIGIN_KEYS[:2]
                    else quality,
                    quality_label=key,
                    headers=self._stream_headers(room_id),
                )

        # 2) 退回 HLS
        hls_map = stream_url.get("hls_pull_url_map") or {}
        if isinstance(hls_map, dict) and hls_map:
            chosen = self._pick_by_quality(
                hls_map, quality, self.ORIGIN_KEYS, self.HIGH_KEYS
            )
            if chosen:
                key, url = chosen
                return StreamInfo(
                    url=url,
                    stream_format=StreamFormat.HLS,
                    quality=StreamQuality.ORIGIN
                    if key in self.ORIGIN_KEYS[:2]
                    else quality,
                    quality_label=key,
                    headers=self._stream_headers(room_id),
                )

        # 3) 单地址兜底
        single = (
            stream_url.get("hls_pull_url")
            or stream_url.get("flv_pull_url")
            or stream_url.get("default_pull_url")
        )
        if isinstance(single, str) and single.startswith("http"):
            fmt = StreamFormat.HLS if ".m3u8" in single else StreamFormat.FLV
            return StreamInfo(
                url=single,
                stream_format=fmt,
                quality=quality,
                quality_label="default",
                headers=self._stream_headers(room_id),
            )

        raise PlatformError(
            f"抖音房间 {room_id} 响应中未找到可用流地址（可能未开播或接口已变更）"
        )

    @staticmethod
    def _pick_by_quality(
        mapping: Dict[str, str],
        quality: StreamQuality,
        origin_keys: List[str],
        high_keys: List[str],
    ) -> Optional[tuple]:
        """按画质优先顺序在映射表中挑选可用地址。

        返回 ``(key, url)``；无匹配返回 None。
        """
        preferred = origin_keys if quality is StreamQuality.ORIGIN else high_keys
        # 先按预设顺序精确匹配
        for key in preferred:
            if key in mapping and isinstance(mapping[key], str):
                return key, mapping[key]
        # 再做大小写不敏感匹配
        lowered = {str(k).lower(): (k, v) for k, v in mapping.items()}
        for key in preferred:
            if key.lower() in lowered:
                origin_key, url = lowered[key.lower()]
                if isinstance(url, str):
                    return origin_key, url
        # 最后任取一个可用地址（保证能录到，宁可降级也不空手）
        for key, url in mapping.items():
            if isinstance(url, str) and url.startswith("http"):
                return key, url
        return None

    def _stream_headers(self, room_id: str) -> Dict[str, str]:
        """生成 ffmpeg 拉流所需请求头（含 Cookie）。"""
        session = self._get_session()
        cookie_str = "; ".join(f"{c.name}={c.value}" for c in session.cookies.jar)
        headers = {
            "User-Agent": self._profile["User-Agent"],
            "Referer": f"https://live.douyin.com/{room_id}",
        }
        if cookie_str:
            headers["Cookie"] = cookie_str
        return headers

    # ------------------------------------------------------- Playwright 兜底
    def capture_stream_via_browser(
        self, room_id: str, wait_seconds: float = 10.0
    ) -> Optional[StreamInfo]:
        """用真实浏览器打开直播间页面，直接抓取播放地址。

        仅在纯 HTTP 方案失败时调用，作为动态签名参数的兜底方案。
        """
        from .playwright_helper import BrowserSession, PlaywrightUnavailable

        room_id = self._normalize_rid(room_id)
        page_url = f"https://live.douyin.com/{room_id}"
        try:
            with BrowserSession(headless=True) as session:
                urls = session.capture_streams(
                    page_url,
                    wait_seconds=wait_seconds,
                    predicate=lambda u: ".flv" in u.lower() or ".m3u8" in u.lower(),
                    # 抖音拉流 CDN 域名特征，用于把「正在播放的那一路」排到最前
                    host_hints=("douyinliving.com", "douyincdn.com", "douyinvod.com"),
                )
                cookies = session.cookies_dict()
                final_url = session.final_url()
        except PlaywrightUnavailable as exc:
            logger.warning("Playwright 兜底不可用：%s", exc)
            return None

        # 身份校验：原房间已下播时，抖音页面会自动播放「推荐直播」，
        # 此时抓到的流属于**别的房间**。录错房间比录不上更糟，必须丢弃。
        landed = re.search(r"live\.douyin\.com/(\d{6,})", final_url or "")
        if landed and landed.group(1) != str(room_id):
            logger.warning(
                "Playwright 兜底落到其它直播间（%s != %s），丢弃抓取结果",
                landed.group(1),
                room_id,
            )
            return None

        if not urls:
            return None

        # urls 已按可信度排序（media 资源类型优先），直接取第一个
        target = urls[0]
        flv = ".flv" in target.lower()

        headers = {
            "User-Agent": self._profile["User-Agent"],
            "Referer": page_url,
        }
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

        return StreamInfo(
            url=target,
            stream_format=StreamFormat.FLV if flv else StreamFormat.HLS,
            quality=StreamQuality.ORIGIN,
            quality_label="browser",
            headers=headers,
        )
