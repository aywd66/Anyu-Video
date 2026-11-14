# -*- coding: utf-8 -*-
"""调试：planc-home / bilibili 页面实际渲染内容。"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WEBUI_ROOT = os.path.dirname(HERE)
sys.path.insert(0, WEBUI_ROOT)

import bridge_server as B  # noqa: E402
from aiohttp import web  # noqa: E402

PORT = 8805


async def _h_index(r):
    with open(os.path.join(WEBUI_ROOT, "index.html"), encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html",
                            headers={"Cache-Control": "no-store"})


async def _h_fallback(req):
    return B.j({})


app = web.Application()
app.router.add_get("/api/bridge/health", lambda r: B.j({"mode": "bridge", "ok": True}))
app.router.add_get("/api/bridge/config", B.h_config)
app.router.add_get("/api/bridge/webui-settings", B.h_webui_settings)
app.router.add_get("/api/bridge/env", lambda r: B.j({}))
app.router.add_get("/api/bridge/storage", lambda r: B.j({"path": "downloads", "exists": True}))
app.router.add_get("/api/bridge/tasks", lambda r: B.j({"tasks": []}))
app.router.add_get("/api/bridge/history", lambda r: B.j({"items": []}))
app.router.add_get("/api/bridge/stats", lambda r: B.j({}))
app.router.add_get("/api/bridge/launcher", lambda r: B.j(_safe_local()))
app.router.add_route("*", "/api/bridge/{tail:.*}", _h_fallback)
app.router.add_get("/", _h_index)
app.router.add_get("/index.html", _h_index)
app.router.add_static("/assets/", os.path.join(WEBUI_ROOT, "assets"))


def _safe_local():
    try:
        return B.q_launcher()
    except Exception as e:  # noqa: BLE001
        return {"_err": str(e)}


threading.Thread(target=lambda: web.run_app(app, host="127.0.0.1", port=PORT,
                                            print=None, access_log=None,
                                            shutdown_timeout=0), daemon=True).start()
time.sleep(2.5)


def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        br = p.chromium.launch(headless=True)
        pg = br.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append("console:" + m.text) if m.type == "error" else None)
        pg.goto("http://127.0.0.1:%d/index.html#/planc-home" % PORT,
                wait_until="networkidle")
        pg.wait_for_timeout(1800)
        print("hash:", pg.evaluate("() => window.location.hash"))
        print("errors:", errs[:3])
        body = pg.evaluate("() => document.body.innerText.slice(0, 600)")
        print("---- body ----")
        print(body)
        print("---- has 引导卡:", "去启动台粘贴链接" in pg.content(),
              "| has ph-paste:", pg.locator("#ph-paste").count())
        br.close()


if __name__ == "__main__":
    main()
