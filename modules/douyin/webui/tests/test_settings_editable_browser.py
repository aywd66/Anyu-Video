# -*- coding: utf-8 -*-
"""端到端：真实浏览器里验证「设置页在 bridge 模式下能改」。

跑法（自带服务，不依赖外部已启动的桥接）：
    runtime\\douyin-venv\\Scripts\\python.exe modules\\douyin\\webui\\tests\\test_settings_editable_browser.py

⚠️ 为什么这个文件值得存在（2026-10-03）：
   「设置只能看不能改」这个故障，**所有静态检查都查不出来**：
     · `node --check` 过了（语法没问题）；
     · `bridge_server.py` 里 `_read_config`/`_write_config` 都在（看着有写入能力）；
     · 连「有没有 POST 路由」的自检也过了。
   真跑起来才暴露两件事：
     ① bridge 分支把整个表单换成了只读表格 —— **一个输入框都没有**；
     ② 我第一版修复里 `renderBridgeConfig` 用了 `cfg.SETTINGS_SCHEMA`，而
        `cfg` 只是 `render()` 里的局部 const → ReferenceError →
        页面变成「读取工程配置失败：cfg is not defined」。
        这个错 `node --check` 完全查不出（跨作用域引用只在运行时炸）。
   → **跨作用域的引用错误、DOM 结构变化，只能靠真浏览器断言。**

隔离：只把 `CONFIG_PATH` / `_WEBUI_SETTINGS_PATH` 指到临时目录的**副本**，
     静态资源仍从真实目录读（页面要能加载）。**绝不碰用户的 config.yml。**
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WEBUI_ROOT = os.path.dirname(HERE)                      # .../douyin/webui
TOOLBOX = os.path.abspath(os.path.join(WEBUI_ROOT, "..", "..", ".."))
sys.path.insert(0, WEBUI_ROOT)

import bridge_server as B  # noqa: E402

TMP = tempfile.mkdtemp(prefix="setcfg_")
B.CONFIG_PATH = os.path.join(TMP, "config.yml")
shutil.copy2(os.path.join(TOOLBOX, "modules", "douyin", "config.yml"), B.CONFIG_PATH)
B._WEBUI_SETTINGS_PATH = os.path.join(TMP, "webui_settings.json")

from aiohttp import web  # noqa: E402
import yaml  # noqa: E402

PORT = 8799
FAILS = []


def check(label, cond, extra=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + label + ((" -> " + extra) if extra and not cond else ""))
    if not cond:
        FAILS.append(label)


async def _index(r):
    with open(os.path.join(WEBUI_ROOT, "index.html"), encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html",
                            headers={"Cache-Control": "no-store"})


app = web.Application()
app.router.add_get("/api/bridge/config", B.h_config)
app.router.add_post("/api/bridge/config", B.h_config_save)
app.router.add_post("/api/bridge/config/reset", B.h_config_reset)
app.router.add_get("/api/bridge/webui-settings", B.h_webui_settings)
app.router.add_get("/api/bridge/env", lambda r: B.j({}))
# ⚠️ detectMode() 认的是 `d.mode === 'bridge'`（不是 ok）。桩形状给错的话
#    前端会回落 mock 模式、压根不调写接口 —— 那是测试自己错，不是产品错。
app.router.add_get("/api/bridge/health", lambda r: B.j({"mode": "bridge", "ok": True}))
app.router.add_get("/api/bridge/tasks", lambda r: B.j({"tasks": []}))
app.router.add_get("/api/bridge/history", lambda r: B.j({"items": []}))
app.router.add_get("/api/bridge/stats", lambda r: B.j({}))
app.router.add_get("/api/bridge/storage", lambda r: B.j({"path": "downloads", "exists": True}))
app.router.add_post("/api/bridge/storage/check", lambda r: B.j({"ok": True, "abs": TMP}))
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
        pg = br.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto("http://127.0.0.1:%d/index.html" % PORT, wait_until="networkidle")
        pg.wait_for_timeout(1000)
        pg.evaluate("window.location.hash = '#/settings'")
        pg.wait_for_timeout(1500)

        print("=== A. 渲染出可编辑控件（原故障：bridge 模式一个输入框都没有）===")
        ns = pg.locator(".switch[data-set]").count()
        ni = pg.locator("input[data-set]").count()
        nsel = pg.locator("select[data-set]").count()
        print("  开关 %d / 输入框 %d / 下拉 %d" % (ns, ni, nsel))
        check("bridge 模式下有可编辑控件", (ns + ni + nsel) > 0)
        check("渲染没有落进错误分支", not errs, str(errs[:2]))
        check("没有「读取工程配置失败」", "读取工程配置失败" not in pg.content())

        sel = 'input[data-set="creators_update_limit"]'
        selq = 'select[data-set="video_quality"]'
        check("上一轮加的「一键更新每人取最新」可编辑", pg.locator(sel).count() == 1)
        check("画质下拉存在", pg.locator(selq).count() == 1)

        print()
        print("=== B. 改值 → 发 POST → 真落盘 ===")
        n0 = pg.evaluate("() => performance.getEntriesByType('resource')"
                         ".filter(r => r.name.includes('/api/bridge/config')"
                         " && r.name.includes('POST')).length")
        pg.fill(sel, "55")
        pg.locator(sel).press("Tab")
        pg.wait_for_timeout(1500)
        w = json.load(open(B._WEBUI_SETTINGS_PATH, encoding="utf-8"))
        check("网页端专属键落盘", w.get("creators_update_limit") == 55, str(w))

        pg.select_option(selq, "original")
        pg.wait_for_timeout(1500)
        d = yaml.safe_load(open(B.CONFIG_PATH, encoding="utf-8-sig"))
        check("config.yml 里的键落盘", d.get("video_quality") == "original", str(d.get("video_quality")))
        check("备份已生成", os.path.exists(B.CONFIG_PATH + ".bak"))

        print()
        print("=== C. 非法值：后端拒绝 + 界面回滚 ===")
        d_before = yaml.safe_load(open(B.CONFIG_PATH, encoding="utf-8-sig")).get("thread")
        pg.fill('input[data-set="thread"]', "999")
        pg.locator('input[data-set="thread"]').press("Tab")
        pg.wait_for_timeout(1500)
        d_after = yaml.safe_load(open(B.CONFIG_PATH, encoding="utf-8-sig")).get("thread")
        check("越界值没写进 config.yml", d_after == d_before, "%s -> %s" % (d_before, d_after))
        shown = pg.input_value('input[data-set="thread"]')
        check("界面回滚成原值（不是显示 999）", shown != "999", "界面显示 " + shown)

        print()
        print("=== D. 白名单外的字段只读（别让用户改了必失败）===")
        ro = pg.evaluate("() => { const el = document.querySelector('input[data-set=\"path\"]');"
                         " return el ? { ro: el.readOnly, v: el.value } : null; }")
        check("path 只读（它有专门的存储位置面板）", bool(ro and ro["ro"]), str(ro))
        check("path 仍显示真实值（只读≠不显示）", bool(ro and ro["v"]), str(ro))

        print()
        print("=== E. 重开页面：值从真实配置回填 ===")
        pg.reload(wait_until="networkidle")
        pg.wait_for_timeout(1800)
        check("creators_update_limit 回填 55", pg.input_value(sel) == "55",
              "显示 " + pg.input_value(sel))
        check("video_quality 回填 original", pg.input_value(selq) == "original",
              "显示 " + pg.input_value(selq))

        print()
        print("=== F. 2026-11-08 新可写项：database_path / proxy ===")
        dp = 'input[data-set="database_path"]'
        px = 'input[data-set="proxy"]'
        ro_dp = pg.evaluate("() => { const el = document.querySelector('" + dp + "');"
                            " return el ? el.readOnly : null; }")
        ro_px = pg.evaluate("() => { const el = document.querySelector('" + px + "');"
                            " return el ? el.readOnly : null; }")
        check("database_path 已可编辑（不再只读）", ro_dp is False, str(ro_dp))
        check("proxy 已可编辑（不再只读）", ro_px is False, str(ro_px))
        pg.fill(dp, "dy_downloader.db")
        pg.locator(dp).press("Tab")
        pg.wait_for_timeout(1200)
        d = yaml.safe_load(open(B.CONFIG_PATH, encoding="utf-8-sig"))
        check("database_path 落盘", d.get("database_path") == "dy_downloader.db",
              str(d.get("database_path")))
        pg.fill(px, "http://127.0.0.1:7890")
        pg.locator(px).press("Tab")
        pg.wait_for_timeout(1200)
        d = yaml.safe_load(open(B.CONFIG_PATH, encoding="utf-8-sig"))
        check("proxy 落盘", d.get("proxy") == "http://127.0.0.1:7890", str(d.get("proxy")))

        print()
        print("=== G. 全部恢复默认（确认 → 写回出厂值 → 页面刷新）===")
        pg.fill('input[data-set="thread"]', "5")
        pg.locator('input[data-set="thread"]').press("Tab")
        pg.wait_for_timeout(1200)
        pg.locator("#st-reset").click()
        pg.locator("#__confirm_ok").click()      # C.confirm 的确认按钮（modal）
        pg.wait_for_timeout(2600)                # reset → 700ms 后 reload
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(1000)
        d = yaml.safe_load(open(B.CONFIG_PATH, encoding="utf-8-sig"))
        check("恢复默认后 thread 回出厂 16", d.get("thread") == 16, str(d.get("thread")))
        check("恢复默认后 proxy 回空", d.get("proxy") == "", repr(d.get("proxy")))
        check("database_path 不参与重置（环境相关键，保持原值）",
              d.get("database_path") == "dy_downloader.db", str(d.get("database_path")))
        shown = pg.input_value('input[data-set="thread"]')
        check("界面立即反映默认值（thread=16）", shown == "16", "显示 " + shown)
        check("数据库路径保持用户值", pg.input_value(dp) == "dy_downloader.db",
              "显示 " + pg.input_value(dp))

        print()
        print("=== H. 确认弹窗点取消 → 不重置 ===")
        pg.fill('input[data-set="thread"]', "5")
        pg.locator('input[data-set="thread"]').press("Tab")
        pg.wait_for_timeout(1200)
        pg.locator("#st-reset").click()
        pg.locator(".modal__foot [data-modal-close]").click()   # 点「取消」
        pg.wait_for_timeout(1500)
        d = yaml.safe_load(open(B.CONFIG_PATH, encoding="utf-8-sig"))
        check("取消后 thread 仍为 5（未被重置）", d.get("thread") == 5, str(d.get("thread")))
        # 收尾：确认恢复，把临时 config.yml 回出厂（不影响下一轮断言）
        pg.locator("#st-reset").click()
        pg.locator("#__confirm_ok").click()
        pg.wait_for_timeout(2600)
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(600)

        br.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print()
    print("== 结果：%s（失败 %d 项）==" % ("全部通过" if not FAILS else "有失败项", len(FAILS)))
    for f in FAILS:
        print("   - " + f)
    sys.exit(0 if not FAILS else 1)
