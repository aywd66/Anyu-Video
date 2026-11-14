# -*- coding: utf-8 -*-
"""作品列表早停（一键更新提速）的离线测试。

背景（2026-10-23 实测）：`一键更新最新作品` 慢在「扫列表」——每位作者都要把
主页作品从最新翻到最老。8230 条的作者 = 412 页 × 每页 3 秒安全间隔 ≈ 20 分钟，
而实际只有最上面几条是新的。

两条停止规则（都必须建立在「列表按发布时间倒序」这个前提上）：

  ① 连续 ``known_stop`` 条（默认 40 = 两屏）都已在本地 → 更早的必然下过；
  ② 第一屏整屏都已下载 → 列表第一屏就是最新的一批，它们全在本地就证明
     **没有新作品**，不必再翻第二屏。

``increase=false``（强制重下）时不传 known_ids，行为与旧版完全一致。

跑法：
    runtime\\douyin-venv\\Scripts\\python.exe -m pytest tests/test_post_list_early_stop.py -q
"""
import asyncio

import core.api_client as api_client_module
from core.api_client import DouyinAPIClient, _consecutive_known_tail

# 翻页间隔 3 秒是线上安全值；测试里压到 0，否则一组用例白等二十秒。
api_client_module._POST_PAGE_INTERVAL_SECONDS = 0


def _ids(start: int, count: int):
    return [str(7000000000000000000 + start + i) for i in range(count)]


class _FakePage:
    """占位：真正被 monkeypatch 掉的是 _fetch_post_page_in_page。"""


def _client_with_pages(pages, page_size=20):
    client = DouyinAPIClient({})
    calls = {"n": 0}

    async def _fake_fetch(page, sec_uid, cursor, count=20):
        idx = calls["n"]
        calls["n"] += 1
        if idx >= len(pages):
            return {"aweme_list": [], "has_more": 0, "max_cursor": 0}
        items = [{"aweme_id": aid, "create_time": 1700000000 - idx} for aid in pages[idx]]
        return {"aweme_list": items, "has_more": 1 if idx < len(pages) - 1 else 0,
                "max_cursor": (idx + 1) * 1000}

    client._fetch_post_page_in_page = _fake_fetch
    return client, calls


def test_consecutive_known_tail_counts_from_the_end():
    ids = ["a", "b", "c", "d"]
    assert _consecutive_known_tail(ids, {"c", "d"}) == 2
    assert _consecutive_known_tail(ids, {"d"}) == 1
    assert _consecutive_known_tail(ids, {"b", "c", "d"}) == 3
    # 断层：尾部已知但中间夹一条未知 → 只数尾部连续的那段
    assert _consecutive_known_tail(ids, {"a", "d"}) == 1
    assert _consecutive_known_tail([], {"a"}) == 0
    assert _consecutive_known_tail(ids, set()) == 0


def test_first_page_all_known_stops_immediately():
    """第一屏 20 条全在本地 → 一页就停，不用再翻。"""
    page1 = _ids(0, 20)
    client, calls = _client_with_pages([page1, _ids(100, 20)])

    ids, _items, pages = asyncio.run(
        client._collect_post_via_in_page_api(
            _FakePage(), "sec", 0, known_ids=set(page1), known_stop=40
        )
    )

    assert pages == 1, "第一屏整屏已知就该停在第 1 页"
    assert len(ids) == 20
    assert calls["n"] == 1, "不该发出第二页请求"


def test_new_items_on_top_keep_scanning_until_two_screens_known():
    """作者一次连发 60 条（前三屏全新）时必须继续翻，不能在第一屏就停。"""
    p1, p2, p3, p4 = _ids(0, 20), _ids(20, 20), _ids(40, 20), _ids(60, 20)
    known = set(p4)  # 只有第四屏是「早就下过」的老作品
    client, calls = _client_with_pages([p1, p2, p3, p4])

    ids, _items, pages = asyncio.run(
        client._collect_post_via_in_page_api(
            _FakePage(), "sec", 0, known_ids=known, known_stop=40
        )
    )

    # 第 4 页才有已知作品：连续已知 20 < 40 → 还要再翻一页确认（这里没有第 5 页）
    assert pages == 4
    assert len(ids) == 80
    assert calls["n"] == 4


def test_known_stop_threshold_needs_full_two_screens():
    """尾部连续已知差一条不满 40 → 继续翻；第 3 页凑够才停。

    第 1 页顶部有 1 条新作品，所以第 2 页结束时尾部连续已知只有 39 条，
    不到 40 的阈值 —— 这是「宁可多翻一屏，也不在断层处提前收手」的代价。
    """
    p1 = ["new-1"] + _ids(0, 19)      # 第 1 条是新的
    p2 = _ids(100, 20)
    p3 = _ids(200, 20)
    known = set(p1[1:]) | set(p2) | set(p3)
    client, calls = _client_with_pages([p1, p2, p3])

    ids, _items, pages = asyncio.run(
        client._collect_post_via_in_page_api(
            _FakePage(), "sec", 0, known_ids=known, known_stop=40
        )
    )

    assert pages == 3, "39 条不到阈值，应当翻到第 3 页凑满 40 条再停"
    assert ids[0] == "new-1", "新的那条必须在结果里"
    assert calls["n"] == 3


def test_no_known_ids_keeps_old_behaviour():
    """不传 known_ids（强制重下）→ 不早停，翻到 has_more=0 为止。"""
    pages = [_ids(i * 20, 20) for i in range(3)]
    client, calls = _client_with_pages(pages)

    ids, _items, got_pages = asyncio.run(
        client._collect_post_via_in_page_api(_FakePage(), "sec", 0, known_ids=None)
    )

    assert got_pages == 3
    assert len(ids) == 60
