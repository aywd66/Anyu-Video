# -*- coding: utf-8 -*-
"""接口分页的增量早停 + 403 风控冷却（离线测试）。

覆盖 2026-10-23 的两项提速改造：

A. 接口分页早停 —— 与浏览器路径同一套语义：
   列表按时间倒序，**连续** ``early_stop_known`` 条（默认 40 = 2 屏）都已在
   本地库 → 更早的必然下过，立即停。前几屏全是新作品（没有跳过）时必须继续
   翻，直到出现跳过并连续达阈值。``increase=false`` / ``early_stop_known=0``
   一律不早停。

B. 403 冷却 —— 命中后 30 分钟内 post/detail 接口直接跳过 HTTP（返回空壳、
   详情走页面内通道），不再白试 3 次退避。

跑法：
    runtime\\douyin-venv\\Scripts\\python.exe -m pytest tests/test_post_api_early_stop.py -q
"""
import asyncio

import core.api_client as api_client_module
from core.api_client import DouyinAPIClient
from core.user_modes.post_strategy import PostUserModeStrategy

api_client_module._POST_PAGE_INTERVAL_SECONDS = 0


class _NoopRateLimiter:
    async def acquire(self):
        return


def _aweme(aweme_id: str):
    return {"aweme_id": aweme_id, "create_time": 1700000000}


def _page(ids, has_more=True, cursor=0):
    return {
        "items": [_aweme(i) for i in ids],
        "has_more": has_more,
        "max_cursor": cursor,
        "status_code": 0,
    }


def _build_strategy(pages, known_ids, increase_post=True, early_stop_known=40):
    """pages: [(ids, has_more, next_cursor), ...] 按请求顺序返回。"""
    state = {"n": 0}

    class _API:
        async def get_user_post(self, _sec_uid, _cursor=0, _count=20):
            idx = min(state["n"], len(pages) - 1)
            state["n"] += 1
            ids, has_more, next_cursor = pages[idx]
            return _page(ids, has_more, next_cursor)

    class _Cfg:
        def __init__(self):
            self._data = {
                "number": {"post": 0},
                "increase": {"post": increase_post},
                "browser_fallback": {"early_stop_known": early_stop_known},
            }

        def get(self, key, default=None):
            return self._data.get(key, default)

    class _Downloader:
        def __init__(self):
            self.api_client = _API()
            self.rate_limiter = _NoopRateLimiter()
            self.config = _Cfg()
            self.database = None
            self.progress = []
            self.verified = False

        def _progress_update_step(self, *_args):
            self.progress.append(_args)

        async def _ensure_local_aweme_index(self):
            return None

        async def _known_aweme_ids(self, ids):
            return {str(i) for i in ids if str(i) in known_ids}

        def _filter_by_time(self, items):
            return items

    downloader = _Downloader()
    strategy = PostUserModeStrategy(downloader)
    strategy._verify_scan_total = lambda *_a, **_k: downloader.__setattr__("verified", True)
    return strategy, downloader, state


def _run(strategy):
    return asyncio.run(strategy._collect_api_items("sec", {}))


def test_api_pagination_early_stops_after_two_screens_known():
    """前两屏全是新作品（无跳过）→ 继续翻；连续 40 条已知后停在第 3 页。"""
    pages = [
        (["a%02d" % i for i in range(20)], True, 1),   # 20 新
        (["b%02d" % i for i in range(20)], True, 2),   # 20 新 → streak=0
        (["c%02d" % i for i in range(20)], True, 3),   # 20 已知 → streak=20
        (["d%02d" % i for i in range(20)], True, 4),   # 40 已知 → 停
    ]
    known = {"c%02d" % i for i in range(20)} | {"d%02d" % i for i in range(20)}
    strategy, downloader, state = _build_strategy(pages, known)
    items, restricted, early_stopped = _run(strategy)

    assert restricted is False
    assert early_stopped is True
    assert state["n"] == 4          # 第 4 页拿到 40 连续已知后立刻停
    assert len(items) == 80
    assert downloader.verified is False  # 早停不做总数对账（少扫是预期）


def test_unknown_item_resets_consecutive_counter():
    """中间夹一条新作品 → 计数归零，必须重新攒够 40 条才停。"""
    pages = [
        (["a%02d" % i for i in range(20)], True, 1),
        (["c%02d" % i for i in range(19)] + ["new1"], True, 2),  # 末尾夹新 → 归零
        (["d%02d" % i for i in range(20)], True, 3),             # 20
        (["e%02d" % i for i in range(20)], True, 4),             # 40 → 停
    ]
    known = ({"a%02d" % i for i in range(20)}
             | {"c%02d" % i for i in range(19)}
             | {"d%02d" % i for i in range(20)}
             | {"e%02d" % i for i in range(20)})
    strategy, _d, state = _build_strategy(pages, known)
    _items, _restricted, early_stopped = _run(strategy)

    assert early_stopped is True
    assert state["n"] == 4


def test_force_redownload_disables_early_stop():
    """increase=false（强制重下）→ 扫到 has_more=0，绝不早停。"""
    pages = [
        (["a%02d" % i for i in range(20)], True, 1),
        (["b%02d" % i for i in range(20)], True, 2),
        (["c%02d" % i for i in range(20)], False, 0),
    ]
    known = {item for ids, _m, _c in pages for item in ids}
    strategy, _d, state = _build_strategy(
        pages, known, increase_post=False
    )
    _items, _restricted, early_stopped = _run(strategy)

    assert early_stopped is False
    assert state["n"] == 3


def test_zero_threshold_disables_early_stop():
    pages = [
        (["a%02d" % i for i in range(20)], True, 1),
        (["b%02d" % i for i in range(20)], False, 0),
    ]
    known = {item for ids, _m, _c in pages for item in ids}
    strategy, _d, state = _build_strategy(pages, known, early_stop_known=0)
    _items, _restricted, early_stopped = _run(strategy)

    assert early_stopped is False
    assert state["n"] == 2


# ---------------- B. 403 风控冷却 ----------------

def test_risk_cooldown_covers_post_and_detail_only():
    client = DouyinAPIClient({})
    assert client._risk_cooldown_applies("/aweme/v1/web/aweme/detail/") is False

    client._enter_risk_cooldown("/aweme/v1/web/aweme/detail/", 403)

    assert client._risk_cooldown_applies("/aweme/v1/web/aweme/detail/") is True
    assert client._risk_cooldown_applies("/aweme/v1/web/aweme/post/") is True
    # 其它接口不受影响（用户信息等仍走正常 HTTP）
    assert client._risk_cooldown_applies("/aweme/v1/web/user/profile/other/") is False


def test_risk_cooldown_skips_http_without_touching_session():
    client = DouyinAPIClient({})
    client._enter_risk_cooldown("/aweme/v1/web/aweme/detail/", 403)

    async def _boom():
        raise AssertionError("冷却期内不应再尝试 HTTP")

    client._ensure_session = _boom
    result = asyncio.run(
        client._request_json("/aweme/v1/web/aweme/detail/", {"aweme_id": "1"})
    )
    assert result == {}


def test_detail_uses_page_fallback_during_cooldown():
    client = DouyinAPIClient({})
    client._enter_risk_cooldown("/aweme/v1/web/aweme/detail/", 403)

    async def _page_fallback(aweme_id):
        return {"aweme_id": aweme_id, "desc": "via-page"}

    client.get_video_detail_via_page = _page_fallback
    detail = asyncio.run(client.get_video_detail("123"))
    assert detail == {"aweme_id": "123", "desc": "via-page"}

    # 批量回补路径（suppress_error=True）直接跳过，不额外开浏览器
    assert asyncio.run(client.get_video_detail("123", suppress_error=True)) is None


def test_cooldown_disabled_via_env(monkeypatch):
    monkeypatch.setenv("DOUYIN_RISK_COOLDOWN", "0")
    client = DouyinAPIClient({})
    client._enter_risk_cooldown("/aweme/v1/web/aweme/detail/", 403)
    assert client._risk_cooldown_applies("/aweme/v1/web/aweme/detail/") is False
