"""账号中心：用「登录态页面通道」读当前账号的作品 / 喜欢 / 收藏 / 关注。

为什么单独一个模块、而不是塞进 bridge_server：
    · 这四类列表（`aweme/post`、`aweme/favorite`、`aweme/listcollection`、
      `user/following/list`）全部被抖音的 ArgusSecurityPlugin 门禁，纯 aiohttp
      请求一定回 `403 Blocked by ArgusSecurityPlugin ...`；
    · 唯一稳定的通路是「登录页 + 页面 SDK 现签」，也就是 core/api_client.py
      里 `_collect_post_via_in_page_api` 用的那条路；
    · 这里把那个页面做成**常驻**的：一次开浏览器约 8 秒，四个 tab 每次刷新都
      开会很慢；常驻后登录态和签名都复用，单页翻页约 0.3 秒。

数据流：页面通道 → 归一化 → 缓存（.data/account_cache/douyin.json）→ 前端只读缓存。
刷新是**手动**的（前端点「同步」），不做自动轮询：喜欢 660 条 ≈ 33 页 ≈ 2 分钟，
自动刷会白白打接口、也更容易触发风控。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # modules/douyin
COOKIES_PATH = os.path.join(ROOT, ".cookies.json")
CACHE_DIR = os.path.join(HERE, ".data", "account_cache")
CACHE_PATH = os.path.join(CACHE_DIR, "douyin.json")

BASE = "https://www.douyin.com"
# 故意不再伪造 UA：以前写死 Chrome/139，而真实内核是 Chromium/151，
# UA 与 UA-CH（navigator.userAgentData）对不上，是最容易被风控挑出来的机器特征。
# 现在统一用系统 Edge 自己的 UA（见 _ensure_page / _login_session）。

TOOLBOX_ROOT = os.path.dirname(os.path.dirname(ROOT))    # 工具箱根（= 快手段的 TOOLBOX）
# 扫码登录专用**持久化 profile**：设备指纹（ttwid / UIFID / did / IndexedDB）要跨次保留，
# 否则服务端认不出「这台设备和这张二维码是同一个」，扫码确认会被作废。
DY_PROFILE = os.path.join(TOOLBOX_ROOT, "runtime", "douyin-login-profile")
LOGIN_LOG = os.path.join(TOOLBOX_ROOT, "logs", "douyin-login.log")

# 四个 tab 的稳定 id（前端 tab 键、缓存键、接口参数三处共用）
TABS = ("post", "like", "collect", "following")

# 翻页节流：实测 0.8 秒连发时第二页就开始回空壳（只有 status_code），3 秒稳。
PAGE_DELAY = 3.0
# 单 tab 基础页数上限（20 条/页）。作品数超过它的账号按主页作品总数动态放宽
# （见 _collect_tab），避免「主页 1794 / 只同步到 1600」这类静默截断。
MAX_PAGES = 80
PAGE_SIZE = 20

_GET_SCRIPT = """
async (url) => {
  const r = await fetch(url, {credentials: 'include',
    headers: {'accept': 'application/json, text/plain, */*'}});
  const t = await r.text();
  return {status: r.status, text: t.length > 4000000 ? t.slice(0, 4000000) : t};
}
"""

_POST_SCRIPT = """
async ([url, body]) => {
  const r = await fetch(url, {method: 'POST', credentials: 'include',
    headers: {'accept': 'application/json, text/plain, */*',
              'content-type': 'application/x-www-form-urlencoded'},
    body: body});
  const t = await r.text();
  return {status: r.status, text: t.length > 4000000 ? t.slice(0, 4000000) : t};
}
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _num(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _first_url(container: Any) -> str:
    """抖音的封面/头像都是 `{url_list: [...]}` 或 `[{url_list: [...]}]` 两种形态。"""
    if isinstance(container, dict):
        urls = container.get("url_list") or []
        return str(urls[0]) if urls else ""
    if isinstance(container, list):
        for item in container:
            got = _first_url(item)
            if got:
                return got
    return ""


def _normalize_aweme(item: Dict[str, Any]) -> Dict[str, Any]:
    """作品条目归一化。字段名对齐前端，避免前端再理解抖音的原始结构。"""
    author = item.get("author") or {}
    stats = item.get("statistics") or {}
    video = item.get("video") or {}
    images = item.get("images")
    aweme_id = str(item.get("aweme_id") or "")
    kind = "image" if isinstance(images, list) and images else "video"
    cover = _first_url(video.get("cover")) or _first_url(video.get("origin_cover"))
    if not cover and isinstance(images, list) and images:
        cover = _first_url(images[0])
    duration = _num(video.get("duration"))
    return {
        "id": aweme_id,
        "type": kind,
        "desc": str(item.get("desc") or "").strip(),
        "cover": cover,
        "create_time": _num(item.get("create_time")),
        "duration": round(duration / 1000.0, 1) if duration else 0,
        "digg": _num(stats.get("digg_count")),
        "comment": _num(stats.get("comment_count")),
        "share_url": f"{BASE}/video/{aweme_id}" if aweme_id else "",
        "author": {
            "nickname": str(author.get("nickname") or ""),
            "sec_uid": str(author.get("sec_uid") or ""),
            "avatar": _first_url(author.get("avatar_thumb")),
        },
    }


def _normalize_user(item: Dict[str, Any]) -> Dict[str, Any]:
    """关注列表条目归一化（抖音把它放在 followings[] 里，字段名与作品不同）。"""
    return {
        "sec_uid": str(item.get("sec_uid") or ""),
        "uid": str(item.get("uid") or ""),
        "nickname": str(item.get("nickname") or ""),
        "unique_id": str(item.get("unique_id") or item.get("short_id") or ""),
        "signature": str(item.get("signature") or "").strip(),
        "avatar": _first_url(item.get("avatar_thumb")),
        "follower_count": _num(item.get("follower_count")),
        "aweme_count": _num(item.get("aweme_count")),
        "home_url": (f"{BASE}/user/{item.get('sec_uid')}" if item.get("sec_uid") else ""),
    }


class AccountService:
    """抖音账号内容的读取与缓存。

    线程/协程约束：一个实例只持有一个常驻页面，所有页面操作都过 `self._lock`。
    读缓存（status/list）**不拿锁** —— 同步跑 2 分钟时前端轮询不能被它卡住。
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._lock = asyncio.Lock()
        self._sync: Dict[str, Any] = {"running": False, "tab": "", "done": 0,
                                      "total": 0, "message": "", "error": ""}
        self._login: Dict[str, Any] = {"state": "idle", "message": ""}
        self._login_task: Optional[asyncio.Task] = None

    # ---------- cookie ----------
    def _cookies(self) -> Dict[str, str]:
        try:
            with open(COOKIES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {str(k): str(v) for k, v in (data or {}).items() if k}
        except Exception:
            return {}

    def _save_cookies(self, cookies: Dict[str, str]) -> None:
        merged = self._cookies()
        merged.update({k: v for k, v in cookies.items() if k and v})
        tmp = COOKIES_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=1)
        os.replace(tmp, COOKIES_PATH)

    # 注：曾经有个 _cookie_payload()，把 .cookies.json 里的 Cookie 注入一个新的
    # Chromium context。2026-11-01 起不再这么做 —— 抖音把会话和设备绑在一起，
    # 搬过去的 Cookie 会回 `status_code 8 用户未登录`。现在读取通道和扫码登录
    # 共用同一个持久化 profile（见 _ensure_page），Cookie 只作留档。

    # ---------- 常驻页面 ----------
    async def _ensure_page(self):
        if self._page is not None and not self._page.is_closed():
            return self._page
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:  # pragma: no cover - 依赖缺失时给出人话
            raise RuntimeError(f"Playwright 不可用：{exc}") from exc

        if self._pw is None:
            self._pw = await async_playwright().start()
        # 2026-11-01 改：和扫码登录**共用同一个持久化 profile**，不再「导出 Cookie →
        # 注入另一个 Chromium」。实测把登录后的 Cookie 搬过去，抖音回
        # `status_code 8 用户未登录`（同一个 sessionid 在原浏览器里是好的）——
        # 会话是跟设备/浏览器状态绑的，搬 Cookie 等于换了一台设备。
        # 有头 + 挪到屏幕外：抖音对 headless 同样降权（快手那边已经踩过），
        # 窗口不可见但仍是"真"浏览器。
        self._browser = None
        self._context = await self._pw.chromium.launch_persistent_context(
            DY_PROFILE, channel="msedge", headless=False, locale="zh-CN",
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage", "--no-sandbox",
                  "--window-position=-32000,-32000"],
        )
        self._page = (self._context.pages[0] if self._context.pages
                      else await self._context.new_page())
        await self._page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=60000)
        # 页面 SDK（byted_acrawler）就绪前发请求会被 Argus 拦；就绪后再等 3 秒
        # 让 msToken / uifid 这一套彻底落定（实测少了这一步第二页就回空壳）。
        try:
            await self._page.wait_for_function(
                "() => !!(window.byted_acrawler && window.byted_acrawler.frontierSign)",
                timeout=20000,
            )
        except Exception:
            pass
        await self._page.wait_for_timeout(3000)
        return self._page

    async def _reset_page(self) -> None:
        """关掉常驻页，把 profile 让出来（扫码登录要独占同一个 profile）。"""
        try:
            if self._context is not None:
                await self._context.close()
        except Exception:
            pass
        self._page = None
        self._context = None

    async def close(self) -> None:
        await self._reset_page()
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._pw = None

    # ---------- 页面内请求 ----------
    async def _fetch(self, path: str, params: Optional[Dict[str, Any]] = None,
                     *, method: str = "GET", body: str = "") -> Dict[str, Any]:
        page = await self._ensure_page()
        query = urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = path + ("?" + query if query else "")
        if method.upper() == "POST":
            res = await page.evaluate(_POST_SCRIPT, [url, body])
        else:
            res = await page.evaluate(_GET_SCRIPT, url)
        status = _num((res or {}).get("status"))
        text = str((res or {}).get("text") or "")
        if status != 200:
            raise RuntimeError(f"接口 HTTP {status}：{text[:120]}")
        try:
            data = json.loads(text)
        except Exception:
            raise RuntimeError(f"接口返回非 JSON：{text[:120]}")
        return data if isinstance(data, dict) else {}

    def _base_params(self) -> Dict[str, Any]:
        return {"device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web"}

    async def profile(self) -> Dict[str, Any]:
        params = self._base_params()
        params["publish_video_strategy_type"] = "2"
        data = await self._fetch("/aweme/v1/web/user/profile/self/", params)
        user = data.get("user") or {}
        if not user:
            raise RuntimeError("读取账号资料失败：接口未返回用户信息（登录可能已失效）")
        return {
            "nickname": str(user.get("nickname") or ""),
            "sec_uid": str(user.get("sec_uid") or ""),
            "uid": str(user.get("uid") or ""),
            "unique_id": str(user.get("unique_id") or user.get("short_id") or ""),
            "signature": str(user.get("signature") or "").strip(),
            "avatar": _first_url(user.get("avatar_thumb")),
            "aweme_count": _num(user.get("aweme_count")),
            "favoriting_count": _num(user.get("favoriting_count")),
            "following_count": _num(user.get("following_count")),
            "follower_count": _num(user.get("follower_count")),
        }

    async def page_of(self, tab: str, sec_uid: str, cursor: int) -> Tuple[List[Dict[str, Any]], bool, int]:
        """取一页。返回 (归一化条目, 还有下一页?, 下一页游标)。"""
        if tab == "post":
            params = self._base_params()
            params.update({"sec_user_id": sec_uid, "max_cursor": cursor,
                           "count": PAGE_SIZE, "publish_video_strategy_type": "2"})
            data = await self._fetch("/aweme/v1/web/aweme/post/", params)
            items = [_normalize_aweme(x) for x in (data.get("aweme_list") or [])]
            return items, bool(_num(data.get("has_more"))), _num(data.get("max_cursor"))

        if tab == "like":
            params = self._base_params()
            params.update({"sec_user_id": sec_uid, "max_cursor": cursor, "count": PAGE_SIZE})
            data = await self._fetch("/aweme/v1/web/aweme/favorite/", params)
            items = [_normalize_aweme(x) for x in (data.get("aweme_list") or [])]
            return items, bool(_num(data.get("has_more"))), _num(data.get("max_cursor"))

        if tab == "collect":
            params = self._base_params()
            params["publish_video_strategy_type"] = "2"
            data = await self._fetch(
                "/aweme/v1/web/aweme/listcollection/", params, method="POST",
                body=f"count={PAGE_SIZE}&cursor={int(cursor or 0)}",
            )
            items = [_normalize_aweme(x) for x in (data.get("aweme_list") or [])]
            return items, bool(_num(data.get("has_more"))), _num(data.get("cursor"))

        if tab == "following":
            params = self._base_params()
            params.update({
                "user_id": sec_uid, "sec_user_id": sec_uid, "offset": 0,
                "count": PAGE_SIZE, "source_type": "1", "gps_access": "0",
                "address_book_access": "0", "min_change": "0",
            })
            # 关注列表是**时间游标**：上一页的 min_time 作为下一页的 max_time
            if cursor:
                params["max_time"] = cursor
            data = await self._fetch("/aweme/v1/web/user/following/list/", params)
            raw = data.get("followings") or data.get("follow_list") or []
            items = [_normalize_user(x) for x in raw]
            return items, bool(data.get("has_more")), _num(data.get("min_time"))

        raise RuntimeError(f"未知的 tab：{tab}")

    # ---------- 本地已下载标记 ----------
    def _local_ids(self, ids: List[str]) -> set:
        if not ids or not self.db_path or not os.path.exists(self.db_path):
            return set()
        found = set()
        try:
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            try:
                for i in range(0, len(ids), 400):
                    chunk = ids[i:i + 400]
                    marks = ",".join("?" * len(chunk))
                    for (aid,) in con.execute(
                        f"select aweme_id from aweme where aweme_id in ({marks})", chunk
                    ):
                        found.add(str(aid))
            finally:
                con.close()
        except Exception:
            return found
        return found

    # ---------- 缓存 ----------
    def read_cache(self) -> Dict[str, Any]:
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_cache(self, data: Dict[str, Any]) -> None:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)

    def status(self) -> Dict[str, Any]:
        cache = self.read_cache()
        profile = cache.get("profile") or {}
        tabs = cache.get("tabs") or {}
        return {
            "platform": "douyin",
            "logged_in": bool(profile.get("sec_uid")),
            "profile": profile,
            "updated_at": cache.get("updated_at") or "",
            "counts": {t: len((tabs.get(t) or {}).get("items") or []) for t in TABS},
            "sync": dict(self._sync),
            "login": dict(self._login),
        }

    def list_cached(self, tab: str, limit: int = 0) -> Dict[str, Any]:
        cache = self.read_cache()
        block = (cache.get("tabs") or {}).get(tab) or {}
        items = block.get("items") or []
        if limit and limit > 0:
            items = items[:limit]
        return {
            "tab": tab,
            "items": items,
            "total": len(block.get("items") or []),
            "updated_at": cache.get("updated_at") or "",
            "synced": bool(block),
        }

    # ---------- 同步 ----------
    async def sync(self, tabs: Optional[List[str]] = None) -> Dict[str, Any]:
        if self._sync.get("running"):
            return dict(self._sync)
        wanted = [t for t in (tabs or TABS) if t in TABS] or list(TABS)
        self._sync = {"running": True, "tab": wanted[0], "done": 0, "total": len(wanted),
                      "message": "读取账号资料", "error": "", "started_at": _now()}
        try:
            async with self._lock:
                profile = await self.profile()
                sec_uid = profile["sec_uid"]
                cache = self.read_cache()
                cache["platform"] = "douyin"
                cache["profile"] = profile
                cache.setdefault("tabs", {})
                for idx, tab in enumerate(wanted):
                    self._sync.update({"tab": tab, "done": idx,
                                       "message": f"同步 {tab}（第 1 页）"})
                    items = await self._collect_tab(
                        tab, sec_uid, profile.get("aweme_count") or 0
                    )
                    cache["tabs"][tab] = {"items": items, "count": len(items)}
                cache["updated_at"] = _now()
                self._write_cache(cache)
            self._sync.update({"running": False, "done": len(wanted),
                               "message": "同步完成", "finished_at": _now()})
        except Exception as exc:
            self._sync.update({"running": False, "error": str(exc),
                               "message": f"同步失败：{exc}", "finished_at": _now()})
        return dict(self._sync)

    async def _collect_tab(
        self, tab: str, sec_uid: str, profile_total: int = 0
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        seen = set()
        cursor = 0
        pages = 0
        empty_retry = 0
        # 页数上限要能覆盖主页全部作品：作品 tab 用主页作品总数推导页数，
        # 只上浮不下调；推不出总数时退回 MAX_PAGES 基础上限。
        max_pages = MAX_PAGES
        if tab == "post" and int(profile_total or 0) > 0:
            need_pages = -(-int(profile_total) // PAGE_SIZE) + 5
            max_pages = max(max_pages, need_pages)
        while pages < max_pages:
            page_items, has_more, next_cursor = await self.page_of(tab, sec_uid, cursor)
            if not page_items:
                # 空页多半是限速，不是「到底了」；退一步重试，别把限速当成结束。
                if empty_retry < 2:
                    empty_retry += 1
                    await asyncio.sleep(3.0 * empty_retry)
                    continue
                break
            empty_retry = 0
            pages += 1
            for it in page_items:
                key = it.get("id") or it.get("sec_uid")
                if key and key not in seen:
                    seen.add(key)
                    items.append(it)
            self._sync["message"] = f"同步 {tab}（第 {pages} 页，已 {len(items)} 条）"
            if not has_more or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
            await asyncio.sleep(PAGE_DELAY)

        # 标记哪些已经在本地库里（前端据此显示「已下载」）
        if tab in ("post", "like", "collect"):
            local = self._local_ids([i["id"] for i in items])
            for it in items:
                it["local"] = it["id"] in local
        return items

    # ---------- 扫码登录 ----------
    def login_status(self) -> Dict[str, Any]:
        return dict(self._login)

    async def start_login(self) -> Dict[str, Any]:
        if self._login_task and not self._login_task.done():
            return dict(self._login)
        self._login = {"state": "waiting", "message": "已打开登录窗口，请扫码登录", "started_at": _now()}
        self._login_task = asyncio.create_task(self._login_flow())
        return dict(self._login)

    def _login_log(self, msg: str) -> None:
        """把扫码流程的关键步骤落盘 —— 扫码失败时，这是唯一能复盘的东西。"""
        try:
            os.makedirs(os.path.dirname(LOGIN_LOG), exist_ok=True)
            with open(LOGIN_LOG, "a", encoding="utf-8") as f:
                f.write("%s %s\n" % (_now(), msg))
        except Exception:
            pass

    async def _finish_login(self, context, user: Dict[str, Any]) -> None:
        """登录成功收尾：导出 Cookie 留档 + 记状态。

        登录态的真正载体是这个持久化 profile（读取通道也用同一个），
        `.cookies.json` 只是留档/给别的工具复用，不再靠它"搬"会话。
        """
        cookies = {c["name"]: c["value"] for c in await context.cookies()
                   if "douyin.com" in str(c.get("domain") or "")}
        self._save_cookies(cookies)
        nick = str(user.get("nickname") or "")
        self._login = {"state": "ok", "message": f"已登录：{nick}", "nickname": nick,
                       "finished_at": _now()}
        self._login_log("登录成功：%s（留档 Cookie %d 项）" % (nick, len(cookies)))

    async def _open_login_modal(self, page) -> bool:
        """确保扫码弹窗在屏幕上。

        抖音精选首页通常自己弹（"登录后免费畅享高清视频"），但登录态残留、
        或者用户把弹窗关掉之后就没了 —— 那时得点一下「登录」。
        """
        try:
            txt = await asyncio.wait_for(
                page.evaluate("() => document.body.innerText || ''"), 8)
        except Exception:
            txt = ""
        if re.search(r"(扫码登录|二维码|扫一扫)", txt or ""):
            return True
        for sel in ("button:has-text('登录')", "[data-e2e='login-button']", "text=登录"):
            try:
                loc = page.locator(sel).first
                if not await loc.count():
                    continue
                await loc.click(timeout=3000)
                await page.wait_for_timeout(1500)
                return True
            except Exception:
                continue
        return False

    async def _login_flow(self) -> None:
        """扫码登录入口：先独占 profile（和常驻读取页共用同一个目录，不能并存）。"""
        try:
            async with self._lock:
                await self._reset_page()          # 把 profile 让出来给登录窗口
                await self._login_session()
        except Exception as exc:
            self._login = {"state": "failed", "message": f"登录流程异常：{exc}"}
            self._login_log("异常：%r" % exc)

    async def _login_session(self) -> None:
        """开一个**有头** Edge（持久化 profile）等用户扫码，成功后把 Cookie 写回。

        2026-11-01 改：不再 `new_context + add_cookies`。
        临时 profile 每次都是"新设备"，服务端认不出这台设备和二维码是同一个，
        确认时直接把这次登录作废 —— 网页于是又给一张新码，用户看到的就是
        「扫码完成 → 又冒出二维码，永远登不上」。快手早踩过同一个坑
        （见 KsAccountService._open 的注释），这里照同一条路改：

          · 固定 profile 目录 → ttwid / UIFID / did / IndexedDB 跨次保留；
          · 用系统 Edge（channel=msedge）且**不伪造 UA** —— 之前 UA 写 Chrome/139
            而真实内核是 Chromium/151，UA 与 UA-CH 对不上，是最扎眼的机器特征；
          · 不往新 context 里塞旧 Cookie（旧 Cookie 已失效时只会污染 passport 流程）。

        为什么不用 tools/cookie_fetcher.py：它靠 `input()` 等终端回车，
        桥接服务是后台进程、没有终端。这里改成轮询 Cookie + 页面内自检。
        """
        pw = None
        context = None
        try:
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()

            async def _profile_now() -> Dict[str, Any]:
                """页内问一次 profile/self。超时必须短：页面跳转时 evaluate 会一直挂着。"""
                try:
                    res = await asyncio.wait_for(
                        page.evaluate(
                            _GET_SCRIPT,
                            "/aweme/v1/web/user/profile/self/?device_platform=webapp&aid=6383"),
                        timeout=10)
                except Exception:
                    return {}
                try:
                    data = json.loads((res or {}).get("text") or "{}")
                except Exception:
                    return {}
                user = data.get("user") or {}
                return user if isinstance(user, dict) and user.get("sec_uid") else {}

            try:
                context = await pw.chromium.launch_persistent_context(
                    DY_PROFILE, channel="msedge", headless=False,
                    locale="zh-CN", viewport={"width": 1280, "height": 860},
                    args=["--disable-blink-features=AutomationControlled",
                          "--no-first-run", "--no-default-browser-check",
                          "--disable-features=msEdgeFirstRunExperience"],
                )
            except Exception as exc:
                self._login = {"state": "failed",
                               "message": "登录窗口打不开（多半是上一次的登录窗口还开着，"
                                          "profile 被占住）：%s" % exc}
                self._login_log("启动失败：%r" % exc)
                return

            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=60000)
            except Exception:
                # 首页资源重，domcontentloaded 偶尔 60 秒还不来；commit 到了就够用
                try:
                    await page.goto(f"{BASE}/", wait_until="commit", timeout=20000)
                except Exception as exc:
                    self._login = {"state": "failed", "message": f"打开抖音首页失败：{exc}"}
                    self._login_log("打开首页失败：%r" % exc)
                    return
            await page.wait_for_timeout(4000)
            try:
                await page.bring_to_front()      # 窗口被别的窗口盖住，用户会以为没弹出来
            except Exception:
                pass
            self._login_log("登录窗口已打开（profile=%s）" % DY_PROFILE)

            # ① 这个 profile 里可能还留着上次的登录态：读得到资料就直接收工，别让人白扫。
            user = await _profile_now()
            if user:
                await self._finish_login(context, user)
                return
            qr = await self._open_login_modal(page)
            self._login_log("扫码弹窗：%s" % ("已出现" if qr else "没检测到（已尝试点击「登录」）"))

            deadline = time.time() + 600          # 手机上找 App、扫码、确认，两三分钟很正常
            noted = False
            while time.time() < deadline:
                if page.is_closed():
                    self._login = {"state": "failed", "message": "登录窗口已关闭"}
                    self._login_log("窗口被关闭")
                    return
                cookies = {c["name"]: c["value"] for c in await context.cookies()
                           if "douyin.com" in str(c.get("domain") or "")}
                if cookies.get("sessionid"):
                    # Cookie 有了还不够：真正确认要问一次 profile/self
                    user = await _profile_now()
                    if user:
                        await self._finish_login(context, user)
                        return
                    if not noted:
                        noted = True
                        self._login = dict(self._login, message="已检测到登录态，正在确认账号…")
                        self._login_log("出现 sessionid，但 profile/self 还没返回用户，继续等")
                await asyncio.sleep(2)
            self._login = {"state": "failed", "message": "登录超时（10 分钟）"}
            self._login_log("登录超时（10 分钟）")
        except Exception as exc:
            self._login = {"state": "failed", "message": f"登录流程异常：{exc}"}
            self._login_log("异常：%r" % exc)
        finally:
            try:
                if context is not None:
                    await context.close()
            except Exception:
                pass
            try:
                if pw is not None:
                    await pw.stop()
            except Exception:
                pass


# ============================================================
# 快手账号中心
# ------------------------------------------------------------
# 和抖音不是一套东西，限制必须写在代码里，不然下一个人会以为是 bug：
#   · 快手网页版个人主页**只有「作品」**，没有「喜欢」「收藏」入口（实测 2026-10-17）；
#   · 「关注」只给**关注流**（关注的人发的作品），不给「我关注了谁」的名单；
#   · 主会话 Cookie `kuaishou.server.web_st` 是 **session cookie**，浏览器一关就没了，
#     `kuaishou.server.webday7_st` 不会自动续期 —— 所以「扫码登录」必须和「同步」
#     在**同一个浏览器实例**里一口气做完，不能指望下次打开还在。
# 因此这里的能力就是：作品（可翻页全量）+ 关注动态（前几页）。喜欢/收藏如实不支持。
# ============================================================

TOOLBOX = TOOLBOX_ROOT                                 # 视频下载工具箱根（同上的另一个叫法）
KS_PROFILE = os.path.join(TOOLBOX, "runtime", "kuaishou-login-profile")
KS_COOKIES = os.path.join(TOOLBOX, "data", "kuaishou", "cookies.json")
KS_CACHE_PATH = os.path.join(CACHE_DIR, "kuaishou.json")
KS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
KS_TABS = ("post", "follow")
# 扫码登录的落点。用**个人主页**而不是首页：首页那个「登录」是导航条上的文字，
# 点它不弹扫码框；个人主页上的「登录」才是真正的登录入口。
# 这个 URL 与 tools/kuaishou_login.py 里验证过的一致（随便一个公开主页都行）。
KS_LOGIN_URL = "https://www.kuaishou.com/profile/3xxjtkcmqqt2fqg"

KS_PL_Q = (
    "query visionProfilePhotoList($userId: String, $pcursor: String, $webPageArea: String) { "
    "visionProfilePhotoList(userId: $userId, pcursor: $pcursor, webPageArea: $webPageArea) { "
    "result llsid pcursor feeds { type author { id name } photo { ... on PhotoEntity { "
    "id caption duration coverUrl photoUrl timestamp } } } } }"
)
KS_SELF_Q = (
    "query visionProfileReduced { visionProfileReduced { result userProfile { "
    "profile { user_id user_name headurl } ownerCount { fan follow photo } } } }"
)

_KS_GQL = """
async (args) => {
  const r = await fetch('/graphql', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, credentials: 'include',
    body: JSON.stringify({operationName: args.op, variables: args.v, query: args.q})});
  return await r.text();
}
"""


def _ks_normalize(feed: Dict[str, Any]) -> Dict[str, Any]:
    photo = feed.get("photo") or {}
    author = feed.get("author") or {}
    pid = str(photo.get("id") or "")
    dur = _num(photo.get("duration"))
    ts = _num(photo.get("timestamp"))
    return {
        "id": pid,
        "type": "video",
        "desc": str(photo.get("caption") or "").strip(),
        "cover": str(photo.get("coverUrl") or ""),
        "create_time": int(ts / 1000) if ts > 10_000_000_000 else ts,
        "duration": round(dur / 1000.0, 1) if dur > 1000 else dur,
        "digg": 0,
        "comment": 0,
        "share_url": f"https://www.kuaishou.com/short-video/{pid}" if pid else "",
        "author": {
            "nickname": str(author.get("name") or ""),
            "sec_uid": str(author.get("id") or ""),
            "avatar": "",
        },
    }


def _ks_num(v: Any) -> int:
    """快手把计数写成 "8.4万" 这种字符串，前端要数字。"""
    s = str(v or "").strip()
    if not s:
        return 0
    try:
        if s.endswith("亿"):
            return int(float(s[:-1]) * 100000000)
        if s.endswith("万"):
            return int(float(s[:-1]) * 10000)
        return int(float(s))
    except ValueError:
        return 0


def _ks_profile(prof: Dict[str, Any], cnt: Dict[str, Any]) -> Dict[str, Any]:
    """把 visionProfileReduced 的原始结构归一化成前端要的形状。"""
    return {
        "nickname": str(prof.get("user_name") or ""),
        "sec_uid": str(prof.get("user_id") or ""),
        "uid": str(prof.get("user_id") or ""),
        "unique_id": str(prof.get("user_id") or ""),
        "signature": "",
        "avatar": str(prof.get("headurl") or ""),
        "aweme_count": _ks_num(cnt.get("photo")),
        "favoriting_count": 0,
        "following_count": _ks_num(cnt.get("follow")),
        "follower_count": _ks_num(cnt.get("fan")),
    }


# 主会话失效后仍留在 profile 里的「看起来还登录着」的 Cookie。
# 不清掉它们，页面会一直以为已登录 —— 连二维码都不给弹。
KS_AUTH_COOKIES = (
    "passToken", "userId",
    "kuaishou.server.web_st", "kuaishou.server.web_ph",
    "kuaishou.server.webday7_st", "kuaishou.server.webday7_ph",
    "kuaishou.live.web_st", "kuaishou.live.web_ph",
    "kwssectoken", "kwscode",
)


def _ks_profile_rest(d: Dict[str, Any]) -> Dict[str, Any]:
    """把 /rest/v/profile/get 的响应归一化成前端要的形状。

    注意字段名是驼峰/连字符混着写（userName / userHead / follows / fans），
    跟 GraphQL 那套 user_name / headurl / ownerCount 完全不是一回事，别混用。
    """
    uid = str(d.get("userId") or "")
    return {
        "nickname": str(d.get("userName") or ""),
        "sec_uid": uid,
        "uid": uid,
        "unique_id": str(d.get("userDefineId") or uid),
        "signature": str(d.get("userTex") or ""),
        "avatar": str(d.get("userHead") or ""),
        "aweme_count": _num(d.get("photo")),
        "favoriting_count": _num(d.get("like")),
        "following_count": _num(d.get("follows")),
        "follower_count": _num(d.get("fans")),
        "eid": str(d.get("eid") or ""),
    }


# 主页往下滚最多翻多少页（每页 20 条）。作品多的人一次同步几百条就够用，
# 再多就是拿风控额度换完整度了。
KS_SCROLL_PAGES = 40


class KsAccountService:
    """快手账号内容（作品 / 关注动态）。接口形状与 AccountService 一致，前端不用分支。"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._lock = asyncio.Lock()
        self._sync: Dict[str, Any] = {"running": False, "tab": "", "done": 0,
                                      "total": 0, "message": "", "error": ""}
        self._login: Dict[str, Any] = {"state": "idle", "message": ""}
        self._login_task: Optional[asyncio.Task] = None

    # ---------- cookie ----------
    # 只保留"导出留档"这一半：登录态的真正载体是持久化 profile（见 _open 的说明），
    # 不再把 Cookie 注入空 context —— 那条路会被快手判成换设备，扫码确认直接作废。
    def _save_cookies(self, cookies: List[Dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(KS_COOKIES), exist_ok=True)
        tmp = KS_COOKIES + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=1)
        os.replace(tmp, KS_COOKIES)

    # ---------- 浏览器 ----------
    async def _open(self, *, headless: bool = True, url: str = "https://www.kuaishou.com/",
                    offscreen: bool = False):
        """打开快手页面。

        ⚠️ 必须用**持久化 profile**（和 tools/kuaishou_login.py 同一条路），不能
        `new_context + add_cookies`：快手把登录态绑在设备的 did + 浏览器本地状态上，
        空 context 里塞旧 Cookie 会出现「手机扫码成功、点了确认、网页却回二维码失效」——
        服务端认为这台"设备"跟二维码不是同一个，直接把这次确认作废。
        持久化 profile 里那套 did/IndexedDB 是快手认的，才能走完扫码确认。

        ⚠️ 快手对 headless 降权：同一个 Cookie，无头窗口里接口会回 result 21/50。
        所以后台同步也用有头窗口，只是把窗口挪到屏幕外（offscreen=True），不打扰人。
        """
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = None
        args = ["--window-position=-32000,-32000"] if offscreen else []
        try:
            self._context = await self._pw.chromium.launch_persistent_context(
                KS_PROFILE, channel="msedge", headless=headless,
                user_agent=KS_UA, locale="zh-CN", args=args,
                viewport={"width": 1360, "height": 900},
            )
        except Exception as exc:
            await self._close_browser()
            raise RuntimeError(
                "快手浏览器 profile 打不开（多半是另一个快手窗口正开着，profile 被占用）。"
                f"关掉多余的快手/Edge 窗口后重试。原始错误：{exc}"
            ) from exc
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        await self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await self._page.wait_for_timeout(4000)
        return self._page

    async def _close_browser(self) -> None:
        for obj, name in ((self._context, "close"), (self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    await getattr(obj, name)()
            except Exception:
                pass
        self._page = self._context = self._browser = self._pw = None

    async def close(self) -> None:
        await self._close_browser()

    async def _gql(self, page, op: str, query: str, variables: Dict[str, Any],
                   timeout: float = 15.0) -> str:
        """在页面里发一次 GraphQL。

        ⚠️ 必须带超时：`page.evaluate` 本身**没有**默认超时，页面正在跳转 /
        重载时它会一直挂着。登录循环里一旦挂住，整个流程就永远停在上一个状态
        （表现：扫码明明成功了，界面一直卡在「请在手机上确认」，直到永远）。
        """
        return await asyncio.wait_for(
            page.evaluate(_KS_GQL, {"op": op, "q": query, "v": variables}), timeout)

    async def _page_text(self, page, timeout: float = 8.0) -> str:
        """读页面可见文字（同样必须带超时，理由见 _gql）。"""
        return await asyncio.wait_for(
            page.evaluate("() => document.body.innerText || ''"), timeout)

    async def _check_login(self, page) -> bool:
        """服务端是否认为「有登录态」。

        ⚠️ 只说明长期 Cookie（passToken / userId）还在，**不代表能读到账号内容**：
        只扫码不确认时它也回 true。真正的收口是 _read_profile 能不能拿到资料。
        """
        try:
            txt = await self._gql(page, "checkLogin", "query checkLogin { checkLogin }", {})
            return '"checkLogin":true' in txt
        except Exception:
            return False

    # ---------- 数据 ----------
    async def _self_eid(self, page) -> str:
        """当前登录用户的 eid（主页地址里的那串）。页面自己会把它存在 localStorage。"""
        try:
            return str(await asyncio.wait_for(
                page.evaluate("() => localStorage.getItem('eid') || ''"), 10) or "")
        except Exception:
            return ""

    async def _listen(self, page, url_part: str, sink: list, limit: int = 0,
                      require_feeds: bool = False):
        """挂一个响应监听器，把匹配 url_part 的 JSON 响应塞进 sink。

        为什么不能自己 fetch：快手 2026 年起给 /rest/v/* 加了 `__NS_hxfalcon` 签名，
        脚本里 `fetch('/rest/v/profile/get')` 一律回 `result 50 签名验证失败`；
        老的 GraphQL（visionProfileReduced / visionProfilePhotoList）也已经停用，
        固定回 `result 21`。**唯一稳的路子是让页面自己去请求，我们只负责听响应。**
        """
        async def on_resp(resp):
            if url_part not in resp.url:
                return
            if limit and len(sink) >= limit:
                return
            try:
                data = json.loads(await resp.text())
            except Exception:
                return
            if not isinstance(data, dict) or data.get("result") != 1:
                return
            if require_feeds and "feeds" not in data:
                return
            sink.append(data)

        page.on("response", on_resp)
        return on_resp

    @staticmethod
    def _unlisten(page, handler) -> None:
        try:
            page.remove_listener("response", handler)
        except Exception:
            pass

    async def _read_profile(self, page) -> Optional[Dict[str, Any]]:
        """读当前登录账号的资料：进自己的主页，听页面自己发出的 /rest/v/profile/get。"""
        eid = await self._self_eid(page)
        if not eid:
            return None
        sink: List[Dict[str, Any]] = []
        handler = await self._listen(page, "/rest/v/profile/get", sink, limit=1)
        try:
            url = f"https://www.kuaishou.com/profile/{eid}"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            for _ in range(40):
                if sink:
                    break
                await asyncio.sleep(0.5)
            if not sink:                      # SPA 有时不重新请求，强制刷一次
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass
                for _ in range(30):
                    if sink:
                        break
                    await asyncio.sleep(0.5)
        finally:
            self._unlisten(page, handler)
        if not sink:
            return None
        return _ks_profile_rest(sink[0])

    async def _self_profile(self, page) -> Dict[str, Any]:
        profile = await self._read_profile(page)
        if profile:
            return profile
        raise RuntimeError("读取快手账号资料失败：登录态不可用，请重新扫码登录")

    async def _read_profile_any(self, page) -> Optional[Dict[str, Any]]:
        profile = await self._read_profile(page)
        if profile:
            return profile
        try:
            pages = list(self._context.pages)
        except Exception:
            return None
        for p in pages:
            try:
                if p is page or p.is_closed():
                    continue
            except Exception:
                continue
            profile = await self._read_profile(p)
            if profile:
                return profile
        return None

    async def _drop_stale_session(self) -> None:
        """清掉「看起来还登录着」的长期 Cookie，让页面重新给出二维码。

        不清的后果：页面认为已登录，既不显示二维码，扫码入口也点不出来，
        而所有需要会话的接口都返回空 —— 用户只能反复点「扫码登录」干瞪眼。
        """
        for name in KS_AUTH_COOKIES:
            try:
                await self._context.clear_cookies(name=name)
            except Exception:
                continue

    @staticmethod
    def _absorb(sink: List[Dict[str, Any]], items: List[Dict[str, Any]], seen: set) -> int:
        """把已收到的响应归一化进 items（按 id 去重）。"""
        added = 0
        for d in sink:
            for feed in d.get("feeds") or []:
                it = _ks_normalize(feed)
                if it["id"] and it["id"] not in seen:
                    seen.add(it["id"])
                    items.append(it)
                    added += 1
        return added

    async def _scroll_collect(self, page, url_part: str, items: List[Dict[str, Any]],
                              label: str, max_pages: int) -> None:
        """往下滚，听页面自己翻页的响应。滚不动/没有新响应就停。"""
        sink: List[Dict[str, Any]] = []
        handler = await self._listen(page, url_part, sink, require_feeds=True)
        seen = {it["id"] for it in items}
        try:
            for i in range(max_pages):
                before = len(sink)
                try:
                    await asyncio.wait_for(
                        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)"), 10)
                except Exception:
                    break
                for _ in range(12):
                    if len(sink) > before:
                        break
                    await asyncio.sleep(0.5)
                if len(sink) == before:
                    break
                self._absorb(sink, items, seen)
                self._sync["message"] = f"同步 {label}（第 {len(sink)} 页，已 {len(items)} 条）"
                last = sink[-1]
                if str(last.get("pcursor") or "") in ("", "no_more"):
                    break
                await asyncio.sleep(0.4)
        finally:
            self._unlisten(page, handler)

    async def _works(self, page, user_id: str = "") -> List[Dict[str, Any]]:
        """我的作品：主页上滚到底，听页面自己发出的 /rest/v/profile/feed。

        必须先 reload 一次：进主页时第一页作品在 `_read_profile` 阶段就请求完了，
        监听器挂晚了会漏掉第一页。
        """
        items: List[Dict[str, Any]] = []
        sink: List[Dict[str, Any]] = []
        handler = await self._listen(page, "/rest/v/profile/feed", sink, require_feeds=True)
        try:
            try:
                await page.reload(wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            for _ in range(30):
                if sink:
                    break
                await asyncio.sleep(0.5)
            self._absorb(sink, items, {it["id"] for it in items})
        finally:
            self._unlisten(page, handler)
        await self._scroll_collect(page, "/rest/v/profile/feed", items, "作品",
                                   max(1, KS_SCROLL_PAGES - 1))
        return items

    async def _follow_feed(self, page, max_pages: int = 3) -> List[Dict[str, Any]]:
        """关注流：关注的人最近发的作品。快手只给流，不给名单。

        网页版的关注流在 /myFollow，接口是懒加载 + 签名保护（抓不到响应），
        但卡片是实打实渲染在 DOM 里的 —— 直接读卡片最省事，也最不容易随接口改版失效。
        """
        items: List[Dict[str, Any]] = []
        seen = set()
        try:
            await page.goto("https://www.kuaishou.com/myFollow",
                            wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)
        except Exception:
            return items
        js = """() => [...document.querySelectorAll('a[href*="/short-video/"]')].map(a => {
            const box = a.parentElement || a;
            const img = a.querySelector('img') || box.querySelector('img');
            const txt = (box.innerText || a.innerText || '').trim().replace(/\\s+/g, ' ');
            return {href: a.getAttribute('href') || '',
                    cover: img ? (img.currentSrc || img.src || '') : '',
                    text: txt.slice(0, 80)};
        })"""
        for i in range(max(1, max_pages)):
            try:
                cards = await asyncio.wait_for(page.evaluate(js), 15)
            except Exception:
                break
            for c in cards or []:
                pid = str(c.get("href") or "").rstrip("/").split("/")[-1]
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                items.append({
                    "id": pid,
                    "type": "video",
                    "desc": str(c.get("text") or ""),
                    "cover": str(c.get("cover") or ""),
                    "create_time": 0,
                    "duration": 0,
                    "digg": 0,
                    "share_url": f"https://www.kuaishou.com/short-video/{pid}",
                    "author": {"nickname": "", "sec_uid": "", "avatar": ""},
                })
            self._sync["message"] = f"同步 关注动态（第 {i + 1} 页，已 {len(items)} 条）"
            try:
                await page.mouse.move(700, 600)
                await page.mouse.wheel(0, 3000)
                await page.wait_for_timeout(1800)
            except Exception:
                break
        return items

    # ---------- 缓存 ----------
    def read_cache(self) -> Dict[str, Any]:
        try:
            with open(KS_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_cache(self, data: Dict[str, Any]) -> None:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = KS_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, KS_CACHE_PATH)

    def status(self) -> Dict[str, Any]:
        cache = self.read_cache()
        profile = cache.get("profile") or {}
        tabs = cache.get("tabs") or {}
        return {
            "platform": "kuaishou",
            "logged_in": bool(profile.get("sec_uid")),
            "profile": profile,
            "updated_at": cache.get("updated_at") or "",
            "counts": {t: len((tabs.get(t) or {}).get("items") or []) for t in KS_TABS},
            "sync": dict(self._sync),
            "login": dict(self._login),
            # 能力声明：前端据此把不支持的 tab 置灰并说明原因，而不是假装在加载
            "capabilities": {"post": True, "follow": True, "like": False, "collect": False,
                             "follow_is_feed": True},
        }

    def list_cached(self, tab: str, limit: int = 0) -> Dict[str, Any]:
        cache = self.read_cache()
        block = (cache.get("tabs") or {}).get(tab) or {}
        items = block.get("items") or []
        if limit and limit > 0:
            items = items[:limit]
        return {"tab": tab, "items": items, "total": len(block.get("items") or []),
                "updated_at": cache.get("updated_at") or "", "synced": bool(block)}

    # ---------- 同步 ----------
    async def sync(self, tabs: Optional[List[str]] = None) -> Dict[str, Any]:
        if self._sync.get("running"):
            return dict(self._sync)
        self._sync = {"running": True, "tab": "post", "done": 0, "total": 1,
                      "message": "打开快手页面", "error": "", "started_at": _now()}
        try:
            async with self._lock:
                # 有头窗口：快手对 headless 降权，同一个 Cookie 在无头窗口里会被
                # 判成 result 21/50（读不到账号）。窗口挪到屏幕外，不打扰用户。
                page = await self._open(headless=False, offscreen=True)
                try:
                    # 用「能不能读到自己的资料」判会话，而不是 checkLogin：
                    # 后者在主会话已死时照样回 true，会让人以为还有登录态。
                    profile = await self._read_profile_any(page)
                    if not profile:
                        raise RuntimeError("快手登录态不完整或已失效：如果刚扫过码，"
                                           "请在手机上点「确认登录」；否则点「扫码登录」重新扫码")
                    await self._sync_in(page, profile=profile)
                finally:
                    await self._close_browser()
            self._sync.update({"running": False, "done": 1, "message": "同步完成",
                               "finished_at": _now()})
        except Exception as exc:
            self._sync.update({"running": False, "error": str(exc),
                               "message": f"同步失败：{exc}", "finished_at": _now()})
        return dict(self._sync)

    async def _sync_in(self, page, profile: Optional[Dict[str, Any]] = None) -> None:
        """在**当前这个浏览器实例**里把数据拉完并落盘。登录态只在本次会话有效，
        所以登录流程也会直接调它。

        落盘是**分步**的：账号资料一读到就先写进缓存，两个 tab 各自写。
        以前是「全跑完才写」，任何一步（比如作品翻页被限速）失败都会让整份缓存
        为空 —— 前端于是显示「没读取到登录账号」，跟实际登录成功的事实冲突。
        """
        profile = profile or await self._self_profile(page)
        cache = self.read_cache()
        cache.update({"platform": "kuaishou", "profile": profile, "updated_at": _now()})
        tabs = cache.setdefault("tabs", {})
        self._write_cache(cache)

        errors: List[str] = []
        self._sync.update({"message": "同步 作品（第 1 页）", "tab": "post"})
        try:
            works = await self._works(page, profile["sec_uid"])
            tabs["post"] = {"items": works, "count": len(works)}
            # /rest/v/profile/get 不返回作品数，用实际拉到的条数补上，
            # 否则账号卡会一直显示「0 作品」。
            if not profile.get("aweme_count") and works:
                profile["aweme_count"] = len(works)
                cache["profile"] = profile
        except Exception as exc:
            errors.append(f"作品：{exc}")
        cache["updated_at"] = _now()
        self._write_cache(cache)

        self._sync.update({"message": "同步 关注动态", "tab": "follow"})
        try:
            follow = await self._follow_feed(page)
            tabs["follow"] = {"items": follow, "count": len(follow)}
        except Exception as exc:
            errors.append(f"关注动态：{exc}")
        cache["updated_at"] = _now()
        self._write_cache(cache)

        # 有失败就报出来（缓存里已有的部分不会丢），别假装同步成功
        if errors:
            raise RuntimeError("；".join(errors))

    # ---------- 扫码登录（登录完立刻同步） ----------
    def login_status(self) -> Dict[str, Any]:
        return dict(self._login)

    async def start_login(self) -> Dict[str, Any]:
        if self._login_task and not self._login_task.done():
            return dict(self._login)
        # 上一次流程异常中断（比如旧版本在同步里抛错）会把 running 留成 True，
        # 之后点「同步」会被永远跳过。锁是空的就说明没有同步在跑，直接复位。
        if self._sync.get("running") and not self._lock.locked():
            self._sync.update({"running": False, "message": "上一次同步被中断"})
        self._login = {"state": "waiting", "message": "已打开快手登录窗口，请扫码（扫码后会自动同步）",
                       "started_at": _now(), "timeout_seconds": 600}
        self._login_task = asyncio.create_task(self._login_flow())
        return dict(self._login)

    async def _qr_visible(self, page) -> bool:
        """页面上是否已经出现二维码/扫码提示。"""
        try:
            t = await self._page_text(page)
        except Exception:
            return False
        return bool(re.search(r"(扫码登录|二维码|扫一扫|打开快手)", t))

    async def _click_login_entry(self, page) -> bool:
        """点开登录弹窗。

        快手不止一个登录入口：导航条右侧的「登录」、左下角卡片的「立即登录」。
        登出状态下真正能弹二维码的是「立即登录」（实测 2026-10-17），而
        `text=登录` 会先命中「登录即可享受…」那段文案，点了没反应 ——
        以前就是靠它，清掉旧会话后二维码一直弹不出来。
        所以这里按候选顺序点，**点到真的出现二维码为止**。
        """
        for sel in ("text=立即登录", "button:has-text('立即登录')",
                    "button:has-text('登录')", "text=登录", "[class*='login']"):
            try:
                loc = page.locator(sel).first
                if not await loc.count():
                    continue
                await loc.click(timeout=3000)
                await page.wait_for_timeout(1500)
                if await self._qr_visible(page):
                    return True
            except Exception:
                continue
        return False

    async def _login_page_state(self, page) -> str:
        """登录弹窗现在处于哪一步：confirm（已扫码等确认）/ expired（码废了）/ waiting。

        这个区分是整个扫码流程的关键：**已扫码但还没确认时必须一动不动**。
        之前只判「失效」，扫码后那几秒页面文案一切换就被当成失效去换码，
        换码等于把这次登录作废 —— 用户看到的就是「扫码成功 → 二维码失效 → 换码」死循环。
        """
        try:
            t = await self._page_text(page)
        except Exception:
            return 'waiting'
        if re.search(r"(确认登录|扫描成功|扫码成功|请在手机上|已扫描|已扫码)", t):
            return 'confirm'
        if re.search(r"二维码.{0,8}(失效|过期)", t):
            return 'expired'
        return 'waiting'

    async def _refresh_login_qr(self, page) -> None:
        """换一张新的登录二维码。

        快手的二维码一两分钟就失效，页面上会显示「二维码已失效」。顺序：
        ① 页面自己的「点击刷新」——最省事，不会把弹窗关掉；
        ② 回到登录页重新点「登录」——等同于首次进入的流程，必然给新码。
        """
        try:
            clicked = await asyncio.wait_for(page.evaluate("""() => {
                const visible = (e) => !!(e && e.offsetParent !== null);
                const nodes = [...document.querySelectorAll('div,span,a,button,p,i')];
                for (const e of nodes) {
                    const t = (e.textContent || '').trim();
                    if (visible(e) && /^(点击刷新|刷新二维码|刷新|重新获取)$/.test(t)) {
                        e.click();
                        return 'text:' + t;
                    }
                }
                return '';
            }"""), 10)
            if clicked:
                await page.wait_for_timeout(1000)
                try:
                    await page.bring_to_front()     # 新码出来了，得让人看见
                except Exception:
                    pass
                return
        except Exception:
            pass
        try:
            await page.goto(KS_LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2500)
            await self._click_login_entry(page)
            try:
                await page.bring_to_front()
            except Exception:
                pass
        except Exception:
            pass

    async def _login_flow(self) -> None:
        try:
            async with self._lock:
                page = await self._open(headless=False, url=KS_LOGIN_URL)
                try:
                    # 扫码窗口容易被别的窗口盖住，用户以为"没弹出来"。主动前置一次。
                    try:
                        await page.bring_to_front()
                    except Exception:
                        pass

                    # ① 已有登录态就别让人白扫一次：先看能不能读到资料。
                    #    注意 checkLogin 只能说明「长期 Cookie 在」，真正的判据是
                    #    页面自己请求 /rest/v/profile/get 拿没拿到账号 —— 读不到才算失效。
                    logged = await self._check_login(page)
                    profile = await self._read_profile(page) if logged else None
                    if logged and profile is None:
                        # 有登录态却读不到资料：会话确实废了，清掉重新扫
                        await self._drop_stale_session()
                        self._login = {"state": "waiting",
                                       "message": "旧的登录态已失效，已清理，正在获取二维码…",
                                       "started_at": self._login.get("started_at") or _now(),
                                       "timeout_seconds": 600}
                        try:
                            await page.goto(KS_LOGIN_URL, wait_until="domcontentloaded",
                                            timeout=45000)
                            await page.wait_for_timeout(2500)
                        except Exception:
                            pass
                    if profile is None:
                        # 登录弹窗不会自己出现，必须点一下「登录」
                        await self._click_login_entry(page)

                    # 10 分钟：手机上打开快手 App、找到扫一扫、确认，实际往往要两三分钟；
                    # 5 分钟窗口太紧，超时后窗口一关用户还得重来。
                    deadline = time.time() + 600
                    last_qr_refresh = time.time()
                    confirm_since = 0.0
                    while time.time() < deadline:
                        if page.is_closed():
                            self._login = {"state": "failed", "message": "登录窗口已关闭"}
                            return

                        # ② 判据：checkLogin 说「有登录态」→ 立刻去读资料。
                        #    读到 = 真登录成功；读不到 = 会话不可用，如实失败。
                        #    （checkLogin 单独不可信：只扫码不确认时它也回 true，
                        #     所以必须用「读不读得到账号」来收口。）
                        if profile is None and await self._check_login(page):
                            profile = await self._read_profile(page)
                            if profile is None:
                                self._login = {
                                    "state": "failed",
                                    "message": "已经登录，但读不到账号资料（会话可能被风控拦下）。"
                                               "请关掉登录窗口后重新扫码，并在手机上点「确认登录」。",
                                }
                                return
                        if profile:
                            cookies = [c for c in await self._context.cookies()
                                       if "kuaishou" in str(c.get("domain") or "")
                                       or c.get("name") in ("passToken", "userId", "did")]
                            self._save_cookies(cookies)
                            self._logged_in = True
                            nickname = profile.get("nickname") or ""
                            # 账号资料先落盘：后面翻页就算失败，账号卡也能显示"已登录谁"，
                            # 而不是一边提示登录成功、一边说没读到账号。
                            cache = self.read_cache()
                            cache.update({"platform": "kuaishou", "profile": profile,
                                          "updated_at": _now()})
                            cache.setdefault("tabs", {})
                            self._write_cache(cache)
                            self._login = {"state": "ok",
                                           "message": f"已登录：{nickname}，正在同步…",
                                           "nickname": nickname,
                                           "started_at": self._login.get("started_at") or _now()}
                            # 登录弹窗是在"未登录的 SPA 状态"里弹出来的，Cookie 变了
                            # 页面里的应用状态还没跟上，此时直接查资料会拿到空 userProfile
                            # （表现就是"手机上已登录，这边却读不到账号"）。重载一次让
                            # SPA 带着新会话重新起来。
                            try:
                                await page.goto(KS_LOGIN_URL, wait_until="domcontentloaded",
                                                timeout=45000)
                                await page.wait_for_timeout(3000)
                            except Exception:
                                pass
                            self._sync.update({"running": True, "tab": "post", "done": 0,
                                               "total": 1, "message": "登录成功，开始同步",
                                               "error": "", "started_at": _now()})
                            # 同步失败不能把「登录成功」这件事一起否掉：两者是两件事，
                            # 而且失败时 `_sync.running` 必须复位，否则后面点同步会一直被跳过。
                            # 但也不能像以前那样只说「已登录」——失败原因要带出去，
                            # 前端才有东西可显示，用户才知道是翻页失败还是会话失效。
                            sync_error = ""
                            try:
                                await self._sync_in(page, profile=profile)
                                self._sync.update({"running": False, "done": 1,
                                                   "message": "同步完成", "finished_at": _now()})
                            except Exception as exc:
                                sync_error = str(exc)
                                self._sync.update({"running": False, "done": 0,
                                                   "error": sync_error,
                                                   "message": f"同步失败：{sync_error}",
                                                   "finished_at": _now()})
                            msg = f"已登录：{nickname}"
                            if sync_error:
                                msg += f"；但同步失败：{sync_error}"
                            self._login = {"state": "ok", "message": msg,
                                           "nickname": nickname, "sync_error": sync_error,
                                           "finished_at": _now()}
                            return

                        # 分三种情况处理，绝不能只看「有没有失效」：
                        #   confirm  —— 手机已扫码，等用户点「确认登录」。此时**不许换码**。
                        #   expired  —— 码真废了，换新码。
                        #   waiting  —— 还在等扫码；只有超过 4 分钟才兜底换一次。
                        state = await self._login_page_state(page)
                        if state == "confirm":
                            if not confirm_since:
                                confirm_since = time.time()
                                # 扫码之后最关键的一步在手机上，别让登录窗口被别的
                                # 窗口（比如工具箱本体）盖住 —— 用户看不到就会以为
                                # "已经登录了"，实际还差最后一下「确认登录」。
                                try:
                                    await page.bring_to_front()
                                except Exception:
                                    pass
                            waited = time.time() - confirm_since
                            if waited > 120:
                                # 真的确认只要几秒。卡在 confirm 两分钟以上，说明这次
                                # 扫码已经废了（用户没点确认 / 平台没接受），再等下去
                                # 二维码也早就不能用了 —— 换新码并说清楚，别让人干等。
                                await self._refresh_login_qr(page)
                                confirm_since = 0.0
                                last_qr_refresh = time.time()
                                self._login = {"state": "waiting",
                                               "message": "上次扫码没确认成功，已换新二维码，请重新扫码",
                                               "started_at": self._login.get("started_at") or _now(),
                                               "timeout_seconds": 600}
                            else:
                                msg = ("已扫码，请在手机上点「确认登录」——只扫码不点确认不会登录成功"
                                       if waited > 25 else "已扫码，请在手机上点「确认登录」")
                                if self._login.get("message") != msg:
                                    self._login = {"state": "waiting",
                                                   "message": msg,
                                                   "started_at": self._login.get("started_at") or _now(),
                                                   "timeout_seconds": 600}
                        elif state == "expired":
                            confirm_since = 0.0
                            await self._refresh_login_qr(page)
                            last_qr_refresh = time.time()
                            self._login = {"state": "waiting",
                                           "message": "二维码已失效，已自动换新码，请重新扫码",
                                           "started_at": self._login.get("started_at") or _now(),
                                           "timeout_seconds": 600}
                        else:
                            confirm_since = 0.0
                            if time.time() - last_qr_refresh > 240:
                                await self._refresh_login_qr(page)
                                last_qr_refresh = time.time()
                                self._login = {"state": "waiting",
                                               "message": "二维码已刷新，请用快手 App 扫码",
                                               "started_at": self._login.get("started_at") or _now(),
                                               "timeout_seconds": 600}

                        await asyncio.sleep(2)
                    self._login = {"state": "failed", "message": "登录超时（10 分钟）"}
                finally:
                    await self._close_browser()
        except Exception as exc:
            self._login = {"state": "failed", "message": f"登录流程异常：{exc}"}
