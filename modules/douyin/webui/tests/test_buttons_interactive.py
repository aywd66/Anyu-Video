# -*- coding: utf-8 -*-
"""端到端：全站按钮交互审计 + 重复视频弹窗「关闭」回归。

2026-11-07 用户报障：「查找重复视频」弹窗里的「关闭」点了没反应。
根因：C.open 打开时对当时存在的 [data-modal-close] 逐元素绑定，而这个
弹窗的 footer「关闭」是扫描结束后才 innerHTML 插入的 —— 新按钮没有监听。
修复：C.open 改挂 mask 级事件委托（动态插入的关闭按钮全部生效）。

本测试两件事：
  1. 专项回归：打开「查找重复视频」，点 footer 的「关闭」，弹窗必须消失；
  2. **全站审计**：遍历主要页面的所有 <button>，用 CDP
     DOMDebugger.getEventListeners 检查「自身没有任何事件监听」的死按钮
     （豁免：disabled、.modal-mask 内的按钮走委托、summary 原生交互）。
     以后任何人加按钮忘了绑事件，跑这个测试就会被抓住。

跑法：
    runtime\\douyin-venv\\Scripts\\python.exe modules\\douyin\\webui\\tests\\test_buttons_interactive.py
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

PORT = 8803
FAILS = []


def check(label, cond, extra=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + label + ((" -> " + extra) if extra and not cond else ""))
    if not cond:
        FAILS.append(label)


def _safe(fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return {"_stub_error": str(exc)}


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


async def _h_index(r):
    with open(os.path.join(WEBUI_ROOT, "index.html"), encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html",
                            headers={"Cache-Control": "no-store"})


async def _h_dedup_scan(r):
    return B.j({"ok": True})


async def _h_dedup_status(r):
    # 扫描"已完成"：让弹窗立即渲染结果 + footer 的动态「关闭」按钮
    return B.j({"running": False,
                "result": {"group_count": 0, "dup_count": 0,
                           "scanned": 1234, "reclaim_bytes": 0, "groups": []}})


async def _h_fallback(req):
    """兜底桩：任何未显式桩化的 /api/bridge/* 请求，按名字给空安全值。
    页面渲染出真实骨架即可 —— 审计的是按钮有没有绑事件，不是数据内容。"""
    tail = req.match_info.get("tail", "")
    if any(k in tail for k in ("tasks", "log")):
        return B.j({"tasks": [], "items": []})
    if any(k in tail for k in ("authors", "works", "items", "folders",
                               "accounts", "providers", "groups", "history")):
        return B.j([])
    return B.j({})


app = web.Application()
app.router.add_get("/api/bridge/launcher", lambda r: B.j(_safe(B.q_launcher)))
app.router.add_get("/api/bridge/live", lambda r: B.j(_safe(B.q_live)))
app.router.add_get("/api/bridge/live/daemon/ensure",
                   lambda r: B.j({"ok": True, "pid": 1234, "version": "test"}))
app.router.add_get("/api/bridge/live/daemon/status", lambda r: B.j({
    "ok": True, "app": "live-recorder-daemon", "version": "test", "pid": 1234,
    "uptime": 60.0, "running": True, "disk_paused": False,
    "counts": {"total": 1, "enabled": 1, "recording": 0, "live": 0, "free_gb": 100},
    "rooms": [{"key": "room-1", "platform": "kuaishou", "room_id": "abc",
               "raw_input": "https://live.kuaishou.com/u/x", "nickname": "甲",
               "title": "", "status": "idle", "status_label": "待命",
               "enabled": True, "recorded_seconds": 0, "total_bytes": 0,
               "segment_count": 0, "last_error": ""}],
    "settings": dict(_SETTINGS),
    "login": {"kuaishou": True, "douyin": False},
    "login_session": {"state": "idle"},
}))
app.router.add_get("/api/bridge/live/daemon/logs", lambda r: B.j({"seq": 0, "items": []}))
app.router.add_post("/api/bridge/dedup/scan", _h_dedup_scan)
app.router.add_get("/api/bridge/dedup/status", _h_dedup_status)
app.router.add_get("/api/bridge/config", B.h_config)
app.router.add_get("/api/bridge/webui-settings", B.h_webui_settings)
app.router.add_get("/api/bridge/health", lambda r: B.j({"mode": "bridge", "ok": True}))
app.router.add_get("/api/bridge/tasks", lambda r: B.j({"tasks": []}))
app.router.add_get("/api/bridge/history", lambda r: B.j({"items": []}))
app.router.add_get("/api/bridge/stats", lambda r: B.j({}))
app.router.add_get("/api/bridge/storage",
                   lambda r: B.j({"path": "downloads", "exists": True}))
app.router.add_get("/api/bridge/env", lambda r: B.j({}))
# 兜底桩必须**最后**注册（aiohttp 按注册顺序匹配）
app.router.add_route("*", "/api/bridge/{tail:.*}", _h_fallback)
app.router.add_get("/", _h_index)
app.router.add_get("/index.html", _h_index)
app.router.add_static("/assets/", os.path.join(WEBUI_ROOT, "assets"))

threading.Thread(
    target=lambda: web.run_app(app, host="127.0.0.1", port=PORT, print=None,
                               access_log=None, shutdown_timeout=0),
    daemon=True).start()
time.sleep(2.5)

PAGES = ["launch/live", "launch/toolbox", "archive", "settings", "tasks",
         "queue", "favorites", "creators", "workbench"]


def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        br = p.chromium.launch(headless=True)
        pg = br.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto("http://127.0.0.1:%d/index.html#/launch/live" % PORT,
                wait_until="networkidle")
        pg.wait_for_timeout(1500)

        print("=== A0. 侧栏结构：平台导航/账号卡已移除，菜单 8 项 ===")
        check("侧栏无平台切换按钮", pg.locator(".platform-btn").count() == 0,
              str(pg.locator(".platform-btn").count()))
        check("侧栏无账号卡（哔哩哔哩·游客可用）", pg.locator("#account-card").count() == 0)
        check("账号卡文案不再出现", "游客可用" not in pg.content())
        for label in ["启动台", "多链排队", "直播流录制", "任务中心",
                      "任务记录", "关注管理", "收藏与喜欢", "设置"]:
            check("侧栏菜单含「%s」" % label,
                  pg.locator('.nav__item:has-text("%s")' % label).count() >= 1)
        check("侧栏无「抖音下载」项", pg.locator('.nav__item:has-text("抖音下载")').count() == 0)

        print("=== A. 专项回归：查找重复视频的「关闭」=== ")
        pg.evaluate("window.location.hash = '#/archive'")
        pg.wait_for_timeout(1500)
        check("archive 页有「查找重复视频」入口", pg.locator("#ar-dedup").count() == 1,
              str(pg.locator("#ar-dedup").count()))
        pg.locator("#ar-dedup").click()
        pg.wait_for_timeout(1200)
        check("弹窗已打开（扫描结果就绪）", pg.locator(".modal-mask").count() == 1)
        # footer 的「关闭」是结果渲染时才插入的 —— 修复前它点了没反应
        pg.locator(".modal__foot button:has-text('关闭')").click()
        pg.wait_for_timeout(500)
        check("点击动态插入的「关闭」后弹窗消失", pg.locator(".modal-mask").count() == 0,
              str(pg.locator(".modal-mask").count()))
        pg.locator("#ar-dedup").click()
        pg.wait_for_timeout(1200)
        pg.locator(".modal__head [data-modal-close]").click()
        pg.wait_for_timeout(400)
        check("头部 × 图标也能关", pg.locator(".modal-mask").count() == 0,
              str(pg.locator(".modal-mask").count()))

        print()
        print("=== B. 全站按钮交互审计 ===")
        audit = []
        for page_id in PAGES:
            pg.evaluate("window.location.hash = '#/%s'" % page_id)
            pg.wait_for_timeout(1400)
            n = pg.evaluate("() => document.querySelectorAll('button').length")
            dead = []
            delegated = 0
            for i in range(n):
                info = pg.evaluate(
                    """(i) => { const b = document.querySelectorAll('button')[i];
                        if (!b) return null;
                        return { text: (b.textContent || '').trim().slice(0, 40),
                                 id: b.id, cls: b.className.slice(0, 60),
                                 disabled: b.disabled }; }""", i)
                if not info or info["disabled"]:
                    continue
                self_l = _cdp_listeners(pg, "document.querySelectorAll('button')[%d]" % i)
                if self_l is None:
                    continue
                clicks = [l for l in self_l.get("listeners", [])
                          if l.get("type") in ("click", "mousedown", "touchstart",
                                               "keydown", "keypress")]
                if clicks:
                    continue
                # 自身无监听 → 沿祖先链找委托（容器级 addEventListener 是
                # 本项目的常见写法：tabs/chips/任务行按钮都这么绑）。
                hit = _cdp_listeners(
                    pg,
                    """(function(){
                        let el = document.querySelectorAll('button')[%d].parentElement;
                        for (let d = 0; el && d < 10; d++, el = el.parentElement) {
                            if (el.classList && el.classList.contains('modal-mask')) return {delegatedMask: true};
                        }
                        return null;
                    })()""" % i)
                if hit and hit.get("delegatedMask"):
                    delegated += 1
                    continue
                found = False
                probe = i
                for depth in range(10):
                    probe_l = _cdp_listeners(
                        pg,
                        "(function(){ let el = document.querySelectorAll('button')[%d];"
                        " for (let d = 0; el && d <= %d; d++) el = el.parentElement;"
                        " return el; })()" % (i, depth))
                    if probe_l is None:
                        break
                    pl = [l for l in probe_l.get("listeners", [])
                          if l.get("type") in ("click", "mousedown", "touchstart")]
                    if pl:
                        found = True
                        break
                if found:
                    delegated += 1
                    continue
                dead.append(info)
            audit.append((page_id, n, dead))
            if dead:
                for d in dead:
                    print("  [DEAD] %s : %r id=%r cls=%r" % (page_id, d["text"], d["id"], d["cls"]))
            else:
                print("  [OK]   %s：%d 个按钮全部可交互" % (page_id, n))

        br.close()

        print()
        total_dead = sum(len(d) for _, _, d in audit)
        check("全站没有死按钮", total_dead == 0, "死按钮 %d 个" % total_dead)
        check("无页面级 JS 错误", not errs, str(errs[:3]))


def _cdp_listeners(pg, expr):
    """通过 CDP DOMDebugger.getEventListeners 查 expression 节点的监听器。"""
    cdp = getattr(pg, "_audit_cdp", None)
    if cdp is None:
        cdp = pg.context.new_cdp_session(pg)
        pg._audit_cdp = cdp  # noqa: SLF001 - 测试内部缓存
    res = cdp.send("Runtime.evaluate", {"expression": expr, "returnByValue": False})
    oid = (res.get("result") or {}).get("objectId")
    if not oid:
        return None
    return cdp.send("DOMDebugger.getEventListeners", {"objectId": oid})


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
