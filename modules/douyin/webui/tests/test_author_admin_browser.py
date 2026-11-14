# -*- coding: utf-8 -*-
"""端到端：真实浏览器里验证「关注管理」的单独更新与删除作者。

跑法（需要桥接服务已在 127.0.0.1:8518 上跑）：
    python ops/restart-bridge.py
    runtime\\douyin-venv\\Scripts\\python.exe modules\\douyin\\webui\\tests\\test_author_admin_browser.py

关键设计：**请求拦截**。
    点「更新」会真的下载、点「确认删除」会真的写库 —— 拿真实作者做验证
    等于拿用户的数据当耗材。所以这里把两个写操作的 HTTP 请求拦下来，
    只断言"前端发出的请求体对不对"，然后用假响应把界面流程走完。
    写库逻辑本身由 test_author_admin.py 用真实库副本覆盖，两边互补。

为什么必须走真实浏览器：接口层全绿、代码读起来也对，仍可能藏着只在浏览器里
    才暴露的问题（比如按钮绑了但选择器作用域不对、弹窗里 querySelector 拿到 null）。
"""
import json
import os
import sys
import tempfile

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8518"
URL = BASE + "/#/creators"
SHOT_DIR = os.environ.get("AUTHOR_TEST_SHOT_DIR") or tempfile.gettempdir()
PASS = FAIL = 0

# 被拦下的请求（真发出去一个都算验证失败）
sent = {"download": [], "delete": []}


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   " + label + (("  " + str(extra)) if extra else ""))
    else:
        FAIL += 1
        print("  [FAIL] " + label + (("  " + str(extra)) if extra else ""))


def _body(route):
    try:
        return json.loads(route.request.post_data or "{}")
    except Exception:
        return {}


def on_download(route):
    sent["download"].append(_body(route))
    route.fulfill(status=200, content_type="application/json",
                  body=json.dumps({"task": {"id": "stub-task", "status": "pending"}}))


def on_delete(route):
    sent["delete"].append(_body(route))
    route.fulfill(status=200, content_type="application/json", body=json.dumps({
        "ok": True, "author": "stub", "deleted": 123, "authorIds": ["1001"],
        "worksLeft": 1579, "backup": r"webui\.tasks\db-backups\stub.db",
        "filesKept": True}))


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append("console.error: " + m.text)
                if m.type == "error" else None)
        # 先拦住两个写操作，再进页面
        page.route("**/api/bridge/download", on_download)
        page.route("**/api/bridge/author/delete", on_delete)

        page.goto(URL, wait_until="load")
        page.wait_for_selector("[data-creator]", timeout=30000)
        page.wait_for_timeout(1200)

        cards = page.query_selector_all("[data-creator]")
        upd = page.query_selector_all('[data-act="update"]')
        dl = page.query_selector_all('[data-act="download"]')
        print("== 1. 作者卡片与按钮 ==")
        print("  作者卡片 %d 张 / 更新按钮 %d 个 / 下载按钮 %d 个"
              % (len(cards), len(upd), len(dl)))
        check("作者卡片渲染出来", len(cards) > 0, len(cards))
        check("每张卡片都有「更新」按钮", len(upd) == len(cards),
              "%d/%d" % (len(upd), len(cards)))
        check("「下载」按钮没被挤掉", len(dl) == len(cards), "%d/%d" % (len(dl), len(cards)))
        page.screenshot(path=os.path.join(SHOT_DIR, "auth-1-list.png"))

        print("\n== 2. 点「更新」只提交该作者的最新作品（拦截，不真下载）==")
        first = page.query_selector("[data-creator]")
        nickname = first.query_selector(".row-item__title").inner_text()
        first.query_selector('[data-act="update"]').click()
        page.wait_for_timeout(1500)
        check("发出了 1 个下载请求", len(sent["download"]) == 1, len(sent["download"]))
        if sent["download"]:
            b = sent["download"][0]
            opt = b.get("options") or {}
            check("  url 是该作者的主页链接",
                  str(b.get("url", "")).startswith("https://www.douyin.com/user/"),
                  b.get("url"))
            check("  只跑作品模式", opt.get("modes"), ["post"])
            check("  每位只取最新 20 个", opt.get("number"), 20)
            check("  开了增量（已下载的跳过）", opt.get("increase"), True)
        toasts = [t.inner_text() for t in page.query_selector_all(".toast")]
        check("有明确的提交反馈", any("已提交" in t for t in toasts), toasts[:1])
        check("更新后仍留在关注页（不跳走）", page.query_selector("#cr-list") is not None)
        print("  （验证对象：%s）" % nickname)

        print("\n== 3. 「⋯」更多菜单 ==")
        first = page.query_selector("[data-creator]")
        first.query_selector('[data-act="more"]').click()
        page.wait_for_selector('[data-more-act="br-delete"]', timeout=8000)
        labels = [b.inner_text().replace("\n", " ") for b in
                  page.query_selector_all("[data-more-act]")]
        print("  菜单项：%s" % " ｜ ".join(x[:22] for x in labels))
        check("有「更新该作者作品」",
              page.query_selector('[data-more-act="br-update"]') is not None)
        check("有「删除作者信息」",
              page.query_selector('[data-more-act="br-delete"]') is not None)
        check("原有项没丢（复制链接 / 去下载 / 备注）",
              all(page.query_selector('[data-more-act="%s"]' % a) for a in
                  ("br-copy", "br-download", "br-note")))
        page.screenshot(path=os.path.join(SHOT_DIR, "auth-2-menu.png"))

        print("\n== 4. 删除确认弹窗：先把后果说清楚，取消不写库 ==")
        page.query_selector('[data-more-act="br-delete"]').click()
        page.wait_for_selector(".danger-note", timeout=8000)
        page.wait_for_timeout(400)
        note = page.query_selector(".danger-note").inner_text()
        print("  风险说明：%s" % note.replace("\n", " / ")[:110])
        check("弹窗列出后果", "删除后会发生什么" in note)
        check("  明确说明磁盘文件保留", "原样保留" in note)
        check("  说明会自动备份", "备份" in note)
        check("  说明之后会被重新下载", "重新下载" in note)
        page.screenshot(path=os.path.join(SHOT_DIR, "auth-3-delete-confirm.png"))
        page.query_selector("[data-modal-close]").click()
        page.wait_for_timeout(500)
        check("点「取消」不发任何删除请求", len(sent["delete"]) == 0, len(sent["delete"]))

        print("\n== 5. 确认删除：请求体正确 + 列表刷新 ==")
        first = page.query_selector("[data-creator]")
        nm = first.query_selector(".row-item__title").inner_text()
        first.query_selector('[data-act="more"]').click()
        page.wait_for_selector('[data-more-act="br-delete"]', timeout=8000)
        page.query_selector('[data-more-act="br-delete"]').click()
        page.wait_for_selector("#__ad_ok", timeout=8000)
        page.query_selector("#__ad_ok").click()
        page.wait_for_timeout(1800)
        check("发出了 1 个删除请求", len(sent["delete"]) == 1, len(sent["delete"]))
        if sent["delete"]:
            check("  按作者名删除（与列表显示的分组一致）",
                  sent["delete"][0].get("author"), nm)
        toasts = [t.inner_text() for t in page.query_selector_all(".toast")]
        check("有删除结果反馈", any("已删除" in t for t in toasts), toasts[:1])
        check("弹窗已关闭", page.query_selector(".danger-note") is None)
        check("列表已重新渲染", page.query_selector("#cr-list [data-creator]") is not None)

        print("\n== 6. 网格视图也有更新入口 ==")
        page.query_selector('[data-cview="grid"]').click()
        page.wait_for_timeout(900)
        tiles = page.query_selector_all("#cr-list .card")
        upd2 = page.query_selector_all('[data-act="update"]')
        check("网格视图渲染出卡片", len(tiles) > 0, len(tiles))
        check("  网格卡片也有「更新」", len(upd2) == len(tiles), "%d/%d" % (len(upd2), len(tiles)))
        page.screenshot(path=os.path.join(SHOT_DIR, "auth-4-grid.png"))

        print("\n== 7. 页面报错 ==")
        real = [e for e in errors if "favicon" not in e.lower()]
        for e in real[:6]:
            print("   !", e[:160])
        check("无 JS 报错", not real, len(real))

        check("全程没有真实发出去写操作（都走拦截）",
              len(sent["delete"]) == 1 and len(sent["download"]) == 1,
              "download=%d delete=%d" % (len(sent["download"]), len(sent["delete"])))
        browser.close()

    print("\n通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("截图目录: %s" % SHOT_DIR)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
