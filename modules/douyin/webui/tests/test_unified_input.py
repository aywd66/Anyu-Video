# -*- coding: utf-8 -*-
"""端到端：统一链接入口（识别 → 路由）。

2026-11-07 用户需求：链接输入收敛为两个 —— 启动台的「链接下载」+
直播录制页的「添加监控房间」，输入后自动识别类型并路由，无需手动选择。

锁定：
  A. 启动台渲染统一入口；四个单工具页（douyin/kuaishou/direct/ty）不再有
     输入框，改为跳转提示卡；
  B. 混合粘贴 5 类链接 → 点「自动识别并下载」→ 各通道 API 收到正确 URL：
     抖音(submit) / 快手(download) / ty(download) / 直链(download) /
     直播间(加入监控) —— 下载框识别出直播间也自动转监控；
  C. 兜底：未知域名 → 直链通道；纯文本无 URL → 不提交；
  D. 直播框贴视频链接 → 不提交（明确提示，不静默转下载）；
  E. 分发后「最近任务」区在总览可见。
"""
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WEBUI_ROOT = os.path.dirname(HERE)
sys.path.insert(0, WEBUI_ROOT)

import bridge_server as B  # noqa: E402

from aiohttp import web  # noqa: E402

PORT = 8804
FAILS = []


def check(label, cond, extra=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + label + ((" -> " + extra) if extra and not cond else ""))
    if not cond:
        FAILS.append(label)


_SETTINGS = {
    "save_path": "E:\\lp\\zb\\直播录制", "poll_interval": 60,
    "quality_preference": "origin", "enable_fallback": True,
    "max_file_size_gb": 2.0, "max_concurrent_recordings": 3,
    "convert_to_mp4": True, "keep_original_after_mp4": True,
    "min_free_disk_gb": 5.0, "check_concurrency": 4, "max_monitor_rooms": 200,
    "max_duration_minutes": 0, "reconnect_interval": 15, "stall_timeout": 60,
    "max_reconnect_attempts": 0, "ffmpeg_path": "ffmpeg",
    "playwright_enabled": True, "headless_browser": True,
}

SAVED = []   # [(tail, body)] —— 兜底路由捕获的提交


def _safe(fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return {"_stub_error": str(exc)}


async def _h_index(r):
    with open(os.path.join(WEBUI_ROOT, "index.html"), encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html",
                            headers={"Cache-Control": "no-store"})


async def _h_fallback(req):
    tail = req.match_info.get("tail", "")
    body = {}
    if req.method == "POST":
        try:
            body = await req.json()
        except Exception:
            body = {}
    SAVED.append((tail, body))
    if "tasks" in tail or "log" in tail:
        return B.j({"tasks": [], "items": []})
    if any(k in tail for k in ("authors", "works", "items", "folders",
                               "accounts", "providers", "groups", "history")):
        return B.j([])
    # 提交类端点给最小合法响应
    if tail.startswith("download") or tail.startswith("ty/") \
            or tail.startswith("kuaishou/") or tail.startswith("direct/"):
        return B.j({"ok": True, "count": 1, "task": {"count": 1}})
    if tail.startswith("live/add-room"):
        return B.j({"ok": True, "message": "已加入监控清单"})
    return B.j({})


app = web.Application()
app.router.add_get("/api/bridge/launcher", lambda r: B.j(_safe(B.q_launcher)))
app.router.add_get("/api/bridge/live", lambda r: B.j(_safe(B.q_live)))
app.router.add_get("/api/bridge/live/daemon/ensure",
                   lambda r: B.j({"ok": True, "pid": 1, "version": "test"}))
app.router.add_get("/api/bridge/live/daemon/status", lambda r: B.j({
    "ok": True, "app": "live-recorder-daemon", "version": "test", "pid": 1,
    "uptime": 1.0, "running": True, "disk_paused": False,
    "counts": {"total": 1, "enabled": 1, "recording": 0, "live": 0, "free_gb": 100},
    "rooms": [], "settings": dict(_SETTINGS),
    "login": {"kuaishou": True, "douyin": False},
    "login_session": {"state": "idle"},
}))
app.router.add_get("/api/bridge/live/daemon/logs", lambda r: B.j({"seq": 0, "items": []}))
app.router.add_get("/api/bridge/live/daemon/settings", lambda r: B.j({"ok": True}))
app.router.add_post("/api/bridge/live/daemon/settings",
                    lambda r: B.j({"ok": True, "applied": {}, "rejected": {}}))
app.router.add_get("/api/bridge/kuaishou/status", lambda r: B.j({"need_login": False}))
app.router.add_get("/api/bridge/health", lambda r: B.j({"mode": "bridge", "ok": True}))
app.router.add_get("/api/bridge/tasks", lambda r: B.j({"tasks": []}))
app.router.add_get("/api/bridge/history", lambda r: B.j({"items": []}))
app.router.add_get("/api/bridge/stats", lambda r: B.j({}))
app.router.add_get("/api/bridge/storage", lambda r: B.j({"path": "downloads", "exists": True}))
app.router.add_get("/api/bridge/config", B.h_config)
app.router.add_get("/api/bridge/webui-settings", B.h_webui_settings)
app.router.add_get("/api/bridge/env", lambda r: B.j({}))
app.router.add_route("*", "/api/bridge/{tail:.*}", _h_fallback)
app.router.add_get("/", _h_index)
app.router.add_get("/index.html", _h_index)
app.router.add_static("/assets/", os.path.join(WEBUI_ROOT, "assets"))

threading.Thread(
    target=lambda: web.run_app(app, host="127.0.0.1", port=PORT, print=None,
                               access_log=None, shutdown_timeout=0),
    daemon=True).start()
time.sleep(2.5)


def calls(prefix):
    """按 tail 前缀取捕获的提交。"""
    out = []
    for tail, body in SAVED:
        if tail.startswith(prefix):
            out.append(body)
    return out


def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        br = p.chromium.launch(headless=True)
        pg = br.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto("http://127.0.0.1:%d/index.html#/launch/toolbox" % PORT,
                wait_until="networkidle")
        pg.wait_for_timeout(1600)

        print("=== A. 启动台渲染统一入口；单工具页不再有输入框 ===")
        check("总览有「链接下载」输入区", pg.locator("#utl-input").count() == 1)
        check("总览有「自动识别并下载」按钮", pg.locator("#utl-go").count() == 1)
        for tool in ["douyin", "kuaishou", "direct", "ty"]:
            pg.evaluate("window.location.hash = '#/launch/%s'" % tool)
            pg.wait_for_timeout(900)
            has_input = pg.locator("#lt-url").count() > 0
            has_card = "链接输入已统一" in pg.content()
            check("单工具页 %s：保留链接输入（无拦截卡）" % tool,
                  has_input and not has_card, "input=%s card=%s" % (has_input, has_card))
        for page_id, marker in [("planc-home", "去启动台粘贴链接"),
                                ("bilibili", "哔哩哔哩链接输入已统一")]:
            pg.evaluate("window.location.hash = '#/%s'" % page_id)
            pg.wait_for_timeout(1100)
            no_input = pg.locator("#ph-paste, #wb-url").count() == 0
            has_card = marker in pg.content()
            check("页面 %s：无输入框 + 引导到启动台" % page_id,
                  no_input and has_card, "input=%s card=%s" % (no_input, has_card))
        pg.evaluate("window.location.hash = '#/launch/toolbox'")
        pg.wait_for_timeout(1200)

        print()
        print("=== B. 混合粘贴 6 类链接 → 自动分发 ===")
        SAVED.clear()
        pg.fill("#utl-input",
                "https://www.douyin.com/video/111\n"
                "https://v.kuaishou.com/abc\n"
                "https://www.bilibili.com/video/BV1xx411c7mD\n"
                "https://www.youtube.com/watch?v=xyz\n"
                "https://example.com/files/movie.mp4\n"
                "https://live.kuaishou.com/u/someone")
        pg.locator("#utl-go").click()
        pg.wait_for_timeout(1800)
        douyin_calls = calls("download")   # submitDownload → download/…
        check("抖音 1 条已提交", len(calls("download")) == 1
              and "douyin.com/video/111" in json.dumps(calls("download")),
              json.dumps(calls("download"))[:200])
        ks = [b for t, b in SAVED if t.startswith("kuaishou/") and b]
        check("快手 1 条已提交", len(ks) == 1 and "v.kuaishou.com/abc" in json.dumps(ks),
              json.dumps(ks)[:200])
        ty = [b for t, b in SAVED if t.startswith("ty/") and b]
        check("TikTok/YouTube 1 条已提交", len(ty) == 1
              and "youtube.com/watch?v=xyz" in json.dumps(ty), json.dumps(ty)[:200])
        dd = [b for t, b in SAVED if t.startswith("direct/") and b]
        check("直链 1 条已提交", len(dd) == 1
              and "example.com/files/movie.mp4" in json.dumps(dd), json.dumps(dd)[:200])
        bili = [b for t, b in SAVED if t.startswith("bili/") and b]
        check("哔哩哔哩 1 条已提交", len(bili) == 1
              and "bilibili.com/video/BV1xx411c7mD" in json.dumps(bili), json.dumps(bili)[:200])
        lr = [b for t, b in SAVED if t.startswith("live/add-room") and b]
        check("直播间 1 条自动转监控", len(lr) == 1
              and "live.kuaishou.com/u/someone" in json.dumps(lr), json.dumps(lr)[:200])
        check("分发汇总展示在页面上", "已分发" in pg.content() or "分发完成" in pg.content())

        print()
        print("=== C. 兜底：未知域名按直链；纯文本不提交 ===")
        SAVED.clear()
        pg.fill("#utl-input", "https://some-unknown-site.org/data.bin")
        pg.locator("#utl-go").click()
        pg.wait_for_timeout(1200)
        dd2 = [b for t, b in SAVED if t.startswith("direct/") and b]
        check("未知域名走直链通道", len(dd2) == 1, str(len(dd2)))
        n_before = len(SAVED)
        pg.fill("#utl-input", "这是一段没有任何链接的纯文本")
        pg.locator("#utl-go").click()
        pg.wait_for_timeout(900)
        check("纯文本不产生提交", len(SAVED) == n_before, str(len(SAVED)))

        print()
        print("=== D. 直播框贴视频链接 → 不提交（明确提示）===")
        pg.evaluate("window.location.hash = '#/launch/live'")
        pg.wait_for_timeout(1500)
        SAVED.clear()
        check("直播页有「添加监控房间」输入", pg.locator("#lt-room").count() == 1)
        pg.fill("#lt-room", "https://www.douyin.com/video/999")
        pg.locator("#lt-addroom").click()
        pg.wait_for_timeout(900)
        lr2 = [b for t, b in SAVED if t.startswith("live/add-room") and b]
        check("视频链接不会被加进监控", len(lr2) == 0, str(len(lr2)))
        SAVED.clear()
        pg.fill("#lt-room", "https://live.douyin.com/888")
        pg.locator("#lt-addroom").click()
        pg.wait_for_timeout(900)
        lr3 = [b for t, b in SAVED if t.startswith("live/add-room") and b]
        check("抖音直播间正常提交", len(lr3) == 1, str(len(lr3)))

        print()
        print('=== F. 单工具页链接输入提交回归（2026-11-08 恢复的输入框）===')
        SAVED.clear()
        pg.evaluate("window.location.hash = '#/launch/douyin'")
        pg.wait_for_timeout(1100)
        check("抖音页有输入框和下载按钮",
              pg.locator("#lt-url").count() == 1 and pg.locator("#lt-go").count() == 1)
        pg.fill("#lt-url", "https://www.douyin.com/video/777")
        pg.locator("#lt-go").click()
        pg.wait_for_timeout(1400)
        dy2 = [b for t2, b in SAVED if t2.startswith("download") and b]
        check("单工具页提交走抖音通道", len(dy2) == 1
              and "douyin.com/video/777" in json.dumps(dy2), json.dumps(dy2)[:200])

        print()
        print("=== E. 总览有「最近任务」区（分发结果可见）===")
        pg.evaluate("window.location.hash = '#/launch/toolbox'")
        pg.wait_for_timeout(1200)
        check("总览渲染最近任务区", pg.locator("#lt-recent").count() == 1)
        check("无页面级 JS 错误", not errs, str(errs[:3]))

        br.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        print()
        if FAILS:
            print("失败 %d 项：" % len(FAILS))
            for f in FAILS:
                print("  -", f)
            sys.exit(1)
        print("全部通过")
