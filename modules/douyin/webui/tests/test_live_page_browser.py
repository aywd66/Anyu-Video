# -*- coding: utf-8 -*-
"""端到端：真实浏览器里验证「直播流录制只有一个页面」。

2026-11-07 合并（桌面窗口退役 → 单页面）要锁住的约束：
  1. #/launch/live 渲染完整控制台：内核 / 录制设置 / 监控清单 / 登录态 /
     Stripchat / 录像库 / 运行日志 —— 各区块齐全；
  2. 页面上**没有**「打开桌面窗口」入口（合并后它是唯一的界面）；
  3. 桌面窗口的独有功能搬进来了：录制设置表单（白名单 18 项）、
     房间行「复制链接」、日志区「导出日志」；
  4. 改设置 → 点保存 → 真的 POST 到 /api/bridge/live/daemon/settings；
  5. 旧路由 #/live 重定向到 #/launch/live（旧书签不留死路）。

跑法（自带服务，不依赖外部已启动的桥接；不碰 8518）：
    runtime\\douyin-venv\\Scripts\\python.exe modules\\douyin\\webui\\tests\\test_live_page_browser.py
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

PORT = 8801
FAILS = []


def check(label, cond, extra=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + label + ((" -> " + extra) if extra and not cond else ""))
    if not cond:
        FAILS.append(label)


def _safe(fn):
    try:
        return fn()
    except Exception as exc:            # noqa: BLE001 - 桩失败要让测试看见原因
        return {"_stub_error": str(exc)}


# daemon 状态桩：两个房间（一录一停）+ 全部 18 项设置
_SETTINGS = {
    "save_path": "E:\\lp\\lp\\douzy\\downloads\\live_recorder",
    "poll_interval": 60, "quality_preference": "origin", "enable_fallback": True,
    "max_file_size_gb": 2.0, "max_concurrent_recordings": 3,
    "convert_to_mp4": True, "keep_original_after_mp4": True, "min_free_disk_gb": 5.0,
    "check_concurrency": 4, "max_monitor_rooms": 200, "max_duration_minutes": 0,
    "reconnect_interval": 15, "stall_timeout": 60, "max_reconnect_attempts": 0,
    "ffmpeg_path": "ffmpeg", "playwright_enabled": True, "headless_browser": True,
}

_ROOMS = [
    {"key": "room-1", "platform": "kuaishou", "room_id": "3xf8dh7tqmd9bps",
     "raw_input": "https://live.kuaishou.com/u/test-streamer",
     "nickname": "测试主播甲", "title": "测试直播标题", "status": "recording",
     "status_label": "录制中", "enabled": True, "recorded_seconds": 3600,
     "total_bytes": 734003200, "segment_count": 3, "last_error": ""},
    {"key": "room-2", "platform": "douyin", "room_id": "123456789",
     "raw_input": "https://live.douyin.com/123456789",
     "nickname": "测试主播乙", "title": "", "status": "idle",
     "status_label": "待命", "enabled": False, "recorded_seconds": 0,
     "total_bytes": 0, "segment_count": 0, "last_error": ""},
]


def _daemon_status():
    return {
        "ok": True, "app": "live-recorder-daemon", "version": "test",
        "pid": 1234, "uptime": 60.0, "running": True, "disk_paused": False,
        "counts": {"total": 2, "enabled": 1, "recording": 1, "live": 1, "free_gb": 88.8},
        "rooms": _ROOMS, "settings": dict(_SETTINGS),
        "login": {"kuaishou": True, "douyin": False},
        # success 会话：页面初始就渲染「登录成功」提示 + 「关闭」按钮 ——
        # 用于锁定「关闭」点击可用的回归（用户报过点它没反应）。
        "login_session": {"state": "success", "platform": "kuaishou",
                          "cookie_chars": 256},
    }


SAVED_BODIES = []


async def _h_settings_save(req):
    body = await req.json()
    SAVED_BODIES.append(body)
    return B.j({"ok": True, "applied": body, "rejected": {}})


async def _index(r):
    with open(os.path.join(WEBUI_ROOT, "index.html"), encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html",
                            headers={"Cache-Control": "no-store"})


app = web.Application()
app.router.add_get("/api/bridge/launcher", lambda r: B.j(_safe(B.q_launcher)))
app.router.add_get("/api/bridge/live", lambda r: B.j(_safe(B.q_live)))
app.router.add_get("/api/bridge/live/daemon/ensure",
                   lambda r: B.j({"ok": True, "pid": 1234, "version": "test", "started": True}))
app.router.add_get("/api/bridge/live/daemon/status", lambda r: B.j(_daemon_status()))
app.router.add_get("/api/bridge/live/daemon/logs",
                   lambda r: B.j({"seq": 3, "items": [
                       {"seq": 1, "level": "info", "time": "12:00:01", "message": "轮询房间：测试主播甲"},
                       {"seq": 2, "level": "ok", "time": "12:00:02", "message": "开播 → 开始录制"},
                       {"seq": 3, "level": "info", "time": "12:00:03", "message": "分片 #1 完成"},
                   ]}))
app.router.add_post("/api/bridge/live/daemon/settings", _h_settings_save)
app.router.add_get("/api/bridge/health", lambda r: B.j({"mode": "bridge", "ok": True}))
app.router.add_get("/api/bridge/tasks", lambda r: B.j({"tasks": []}))
app.router.add_get("/api/bridge/history", lambda r: B.j({"items": []}))
app.router.add_get("/api/bridge/stats", lambda r: B.j({}))
app.router.add_get("/api/bridge/storage", lambda r: B.j({"path": "downloads", "exists": True}))
app.router.add_get("/api/bridge/config", B.h_config)
app.router.add_get("/api/bridge/webui-settings", B.h_webui_settings)
app.router.add_get("/api/bridge/env", lambda r: B.j({}))
app.router.add_get("/", _index)
app.router.add_get("/index.html", _index)
app.router.add_static("/assets/", os.path.join(WEBUI_ROOT, "assets"))

threading.Thread(
    target=lambda: web.run_app(app, host="127.0.0.1", port=PORT, print=None,
                               access_log=None, shutdown_timeout=0),
    daemon=True).start()
time.sleep(2.5)


def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        br = p.chromium.launch(headless=True)
        ctx = br.new_context(accept_downloads=True)
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto("http://127.0.0.1:%d/index.html#/launch/live" % PORT,
                wait_until="networkidle")
        pg.wait_for_timeout(1800)

        print("=== A. 单页面渲染：区块齐全，无桌面窗口入口 ===")
        content = pg.content()
        for name in ["录制内核", "录制设置", "监控清单", "登录态",
                     "Stripchat 房间录制", "录像库", "运行日志"]:
            check("区块存在：%s" % name, name in content)
        check("没有「打开桌面窗口」入口", "打开桌面窗口" not in content)
        check("没有旧「桌面窗口（高级）」文案", "桌面窗口（高级）" not in content)
        check("渲染没有 JS 错误", not errs, str(errs[:2]))

        print()
        print("=== B. 桌面窗口独有功能已并入 ===")
        n = pg.locator("[data-lrset]").count()
        check("设置表单控件 ≥ 18 项", n >= 18, "只有 %d 个" % n)
        for key in ["save_path", "poll_interval", "quality_preference", "enable_fallback",
                    "max_file_size_gb", "max_concurrent_recordings", "convert_to_mp4",
                    "keep_original_after_mp4", "min_free_disk_gb", "check_concurrency",
                    "max_monitor_rooms", "max_duration_minutes", "reconnect_interval",
                    "stall_timeout", "max_reconnect_attempts", "ffmpeg_path",
                    "playwright_enabled", "headless_browser"]:
            check("设置项存在：%s" % key, pg.locator('[data-lrset="%s"]' % key).count() == 1)
        check("房间行有「复制链接」", pg.locator("[data-room-copy]").count() >= 2)
        copy_val = pg.locator('[data-room-copy="room-1"]').get_attribute("data-copy")
        check("复制的是原始输入", copy_val == "https://live.kuaishou.com/u/test-streamer", str(copy_val))
        check("日志区有「导出日志」", pg.locator("#lt-log-export").count() == 1)

        print()
        print("=== C. 保存设置：真 POST 到 daemon/settings ===")
        pg.fill('[data-lrset="poll_interval"]', "120")
        pg.locator("#lt-settings-save").click()
        pg.wait_for_timeout(1200)
        check("settings 已 POST", len(SAVED_BODIES) == 1, str(len(SAVED_BODIES)))
        if SAVED_BODIES:
            check("poll_interval 提交 120", SAVED_BODIES[0].get("poll_interval") == 120,
                  str(SAVED_BODIES[0].get("poll_interval")))
            check("布尔项随表单提交", "convert_to_mp4" in SAVED_BODIES[0])

        print()
        print("=== D. 导出日志：下载触发且含累计行 ===")
        with pg.expect_download(timeout=5000) as dl:
            pg.locator("#lt-log-export").click()
        f = dl.value
        path = f.path()
        data = open(path, encoding="utf-8-sig").read()
        check("导出文件名带时间戳", f.suggested_filename.startswith("live-log-"),
              f.suggested_filename)
        check("导出包含日志行", "开播 → 开始录制" in data, data[:120])

        print()
        print("=== E. 旧路由 #/live 重定向到唯一页面 ===")
        pg.evaluate("window.location.hash = '#/live'")
        pg.wait_for_timeout(1500)
        check("hash 落到 #/launch/live",
              pg.evaluate("() => window.location.hash") == "#/launch/live",
              pg.evaluate("() => window.location.hash"))
        check("重定向后页面仍是完整控制台", "录制设置" in pg.content())

        print()
        print("=== F. 登录成功提示的「关闭」按钮可点（回归：点它没反应）===")
        check("成功提示在页面上", pg.locator("#lt-qr-dismiss").count() == 1,
              str(pg.locator("#lt-qr-dismiss").count()))
        pg.locator("#lt-qr-dismiss").click()
        pg.wait_for_timeout(600)
        check("点击「关闭」后提示消失", pg.locator("#lt-qr-dismiss").count() == 0,
              str(pg.locator("#lt-qr-dismiss").count()))

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
