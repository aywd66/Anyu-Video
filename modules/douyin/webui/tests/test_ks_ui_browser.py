# -*- coding: utf-8 -*-
"""端到端：真实浏览器里验证「作品档案」「关注管理」两个页面的快手标签。

跑法（需要桥接服务已在 127.0.0.1:8518 上跑）：
    python ops/restart-bridge.py
    runtime\\douyin-venv\\Scripts\\python.exe modules\\douyin\\webui\\tests\\test_ks_ui_browser.py

关键设计：**请求拦截**。
    点「更新」会真的调快手下载器、点「确认删除」会真的改台账 —— 拿真实数据
    做验证等于拿用户的库当耗材。所以把写请求全拦下来，只断言"前端发出的
    请求体对不对"，再用假响应把界面流程走完。后端逻辑由 test_ks_archive.py
    用真实台账副本覆盖，两边互补。

为什么必须走真实浏览器：接口全绿、代码读起来也对，仍可能藏只在浏览器里才
    暴露的问题（选择器作用域、弹窗里 querySelector 拿到 null、标签切换后
    状态没重置……）。这个项目已经踩过好几次。
"""
import json
import os
import sys
import tempfile
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8518"
SHOT_DIR = os.environ.get("KS_TEST_SHOT_DIR") or tempfile.gettempdir()
PASS = FAIL = 0

# 被拦下的写请求（真发出去一个都算问题）
sent = {"ks_download": [], "delete": [], "profile": [], "dy_download": []}


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


def stub(route, body=None, status=200):
    route.fulfill(status=status, content_type="application/json",
                  body=json.dumps(body if body is not None else {"ok": True}))


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append("console.error: " + m.text)
                if m.type == "error" else None)

        # 全部写操作拦下 —— 一个都不许真发出去
        page.route("**/api/bridge/kuaishou/download",
                   lambda r: (sent["ks_download"].append(_body(r)),
                              stub(r, {"task": {"id": "stub-ks", "status": "pending"}})))
        page.route("**/api/bridge/ks/author/delete",
                   lambda r: (sent["delete"].append(_body(r)),
                              stub(r, {"ok": True, "author": "stub", "deleted": 157,
                                       "worksLeft": 998, "backup": [r".tasks\ks-backups\x.json"]})))
        page.route("**/api/bridge/ks/author/profile",
                   lambda r: (sent["profile"].append(_body(r)),
                              stub(r, {"ok": True, "author": "stub", "profileId": "3xstub"})))
        page.route("**/api/bridge/download",
                   lambda r: (sent["dy_download"].append(_body(r)), stub(r, {"task": {"id": "stub-dy"}})))

        # ==================================================== 作品档案页
        print("=" * 66)
        print("作品档案页（#/archive）")
        print("=" * 66)
        page.goto(BASE + "/#/archive", wait_until="load")
        page.wait_for_selector(".srctabs", timeout=30000)
        # 等真正的作品卡渲染出来（首次请求要扫媒体索引，慢一点是正常的）
        page.wait_for_selector(".work-card", timeout=60000)
        page.wait_for_timeout(1200)

        tabs = page.query_selector_all(".srctab")
        check("来源标签有 2 个", len(tabs) == 2, len(tabs))
        check("第一个是抖音", tabs[0].inner_text().startswith("抖音"), tabs[0].inner_text().replace("\n", " "))
        check("第二个是快手", tabs[1].inner_text().startswith("快手"), tabs[1].inner_text().replace("\n", " "))
        check("默认选中抖音", "is-on" in (tabs[0].get_attribute("class") or ""))
        # 数量是异步取的，要在切换前读（切换会重建标签 DOM，数字会短暂为空）
        check("抖音标签带作品数", any(ch.isdigit() for ch in tabs[0].inner_text()),
              tabs[0].inner_text().replace("\n", " "))
        check("快手标签带作品数", any(ch.isdigit() for ch in tabs[1].inner_text()),
              tabs[1].inner_text().replace("\n", " "))
        dy_cards = len(page.query_selector_all(".work-card"))
        check("抖音默认列表有卡片", dy_cards > 0, dy_cards)
        page.screenshot(path=os.path.join(SHOT_DIR, "ks-1-archive-douyin.png"))

        # 切到快手
        page.query_selector_all(".srctab")[1].click()
        page.wait_for_timeout(2500)
        tabs = page.query_selector_all(".srctab")
        check("切换后快手选中", "is-on" in (tabs[1].get_attribute("class") or ""))
        ks_cards = len(page.query_selector_all(".work-card"))
        check("快手列表有卡片", ks_cards > 0, ks_cards)
        stats_txt = page.eval_on_selector("#ar-stats", "e => e.innerText") \
            if page.query_selector("#ar-stats") else ""
        check("统计显示快手作品数", "作品总数" in stats_txt, stats_txt.replace("\n", " ")[:60])

        # 快手的筛选器该收起
        check("快手下「类型」下拉已隐藏", page.eval_on_selector("#ar-type", "e => e.hidden"))
        check("快手下「仅收藏」已隐藏", page.eval_on_selector("#ar-fav", "e => e.hidden"))
        authors_opt = page.eval_on_selector_all("#ar-author option", "els => els.length")
        check("快手作者下拉有 5 位 + 全部", authors_opt == 6, authors_opt)

        # 卡片按钮构成：有播放、无收藏
        first = page.query_selector(".work-card")
        acts = page.eval_on_selector_all(".work-card:first-child [data-act]",
                                         "els => els.map(e => e.dataset.act)")
        check("快手卡片有播放入口", "play" in acts, acts)
        check("快手卡片没有收藏按钮（台账没有收藏概念）", "fav" not in acts, acts)
        check("快手卡片有打开目录与复制路径", "folder" in acts and "path" in acts, acts)

        # 播放：点封面 → 弹窗 + video 真的在动
        page.query_selector(".work-card .cover--clickable").click()
        page.wait_for_selector(".modal-mask", timeout=15000)
        page.wait_for_timeout(2500)
        info = page.evaluate("""() => {
            const v = document.querySelector('#mp-video');
            if (!v) return {no: true};
            return {rs: v.readyState, w: v.videoWidth, h: v.videoHeight,
                    dur: v.duration, t: v.currentTime, src: v.getAttribute('src')};
        }""")
        check("播放器里是 video 元素", not info.get("no"))
        check("src 指向快手媒体接口", str(info.get("src", "")).startswith("/api/bridge/ks/media"),
              info.get("src"))
        check("视频元数据已加载（readyState>=1）", (info.get("rs") or 0) >= 1, info)
        check("解析出真实分辨率", (info.get("w") or 0) > 0 and (info.get("h") or 0) > 0,
              "%sx%s" % (info.get("w"), info.get("h")))
        page.wait_for_timeout(1500)
        t2 = page.evaluate("() => { const v = document.querySelector('#mp-video'); return v ? v.currentTime : -1; }")
        check("播放进度在前进", t2 > (info.get("t") or 0), "%.2f -> %.2f" % (info.get("t") or 0, t2))
        page.screenshot(path=os.path.join(SHOT_DIR, "ks-2-archive-player.png"))
        if page.query_selector("[data-modal-close]"):
            page.query_selector("[data-modal-close]").click()
            page.wait_for_timeout(400)

        # 搜索
        page.fill("#ar-search", "成熟女人")
        page.wait_for_timeout(1200)
        n_search = len(page.query_selector_all(".work-card"))
        check("快手下搜索能过滤", 0 < n_search < ks_cards, "%d -> %d" % (ks_cards, n_search))
        page.fill("#ar-search", "")
        page.wait_for_timeout(1000)

        # 切回抖音：筛选器与收藏按钮要恢复
        page.query_selector_all(".srctab")[0].click()
        page.wait_for_selector(".work-card", timeout=60000)
        page.wait_for_timeout(1800)
        check("切回抖音后「类型」下拉恢复", not page.eval_on_selector("#ar-type", "e => e.hidden"))
        acts_dy = page.eval_on_selector_all(".work-card:first-child [data-act]",
                                            "els => els.map(e => e.dataset.act)")
        check("切回抖音后收藏按钮回来", "fav" in acts_dy, acts_dy)
        check("切回抖音后作者下拉恢复", not page.eval_on_selector("#ar-author", "e => e.hidden"))

        # ==================================================== 关注管理页
        print()
        print("=" * 66)
        print("关注管理页（#/creators）")
        print("=" * 66)
        page.goto(BASE + "/#/creators", wait_until="load")
        page.wait_for_selector(".srctabs", timeout=30000)
        page.wait_for_timeout(1500)
        page.query_selector_all(".srctab")[1].click()
        page.wait_for_timeout(1800)

        cards = page.query_selector_all("[data-creator]")
        check("快手作者 5 位", len(cards) == 5, len(cards))
        upd = page.query_selector_all('[data-act="update"]')
        setp = page.query_selector_all('[data-act="set-profile"]')
        # ⚠️ 别把「几位有主页链接」写死：手动映射是会变的（用户填过一次就多一位）。
        #    期望值从接口现算 —— 断言的是**规则**（有链接给「更新」、没链接给「设置主页」），
        #    而不是某一天的数据快照。写死过一次，用户一填主页测试就红。
        ks_authors = json.loads(
            urllib.request.urlopen(BASE + "/api/bridge/ks/authors", timeout=10)
            .read().decode("utf-8"))["authors"]
        want_upd = len([a for a in ks_authors if (a.get("profileUrl") or "").strip()])
        want_setp = len(ks_authors) - want_upd
        check("有主页链接的 %d 位显示「更新」" % want_upd, len(upd), want_upd)
        check("缺链接的 %d 位显示「设置主页」" % want_setp, len(setp), want_setp)
        check("两类入口加起来正好等于作者数",
              len(upd) + len(setp) == len(cards), True)
        check("快手卡片没有「下载」主按钮（全量下载挪进 ⋯）",
              len(page.query_selector_all('[data-act="download"]')) == 0)
        check("快手卡片仍有「⋯」菜单", len(page.query_selector_all('[data-act="more"]')) == 5)
        synctxt = page.eval_on_selector("#cr-syncbar", "e => e.innerText")
        check("状态条写明数据来自快手台账", "快手台账" in synctxt, synctxt.replace("\n", " ")[:70])
        check("状态条有「一键更新最新作品」", bool(page.query_selector("#cr-update-all")))
        page.screenshot(path=os.path.join(SHOT_DIR, "ks-3-creators.png"))

        # 点「更新」→ 拦截并检查请求体
        first_url = [a for a in ks_authors if (a.get("profileUrl") or "").strip()][0]["profileUrl"]
        upd[0].click()
        page.wait_for_timeout(900)
        check("「更新」发出了 1 个快手下载请求", len(sent["ks_download"]) == 1, len(sent["ks_download"]))
        if sent["ks_download"]:
            b = sent["ks_download"][0]
            # 直接对齐接口里那条链接，而不是"必须是 /profile/ 开头" ——
            # 手动填的可能是分享短链，那也应当被原样发出去（下载器自己会跟随跳转）。
            check("请求体 url 就是卡片上那条作者主页链接", b.get("url"), first_url)
            check("请求体 mode = 20（只取最新 20 个）", b.get("mode"), "20")
            check("没有误发抖音下载请求", len(sent["dy_download"]) == 0, len(sent["dy_download"]))
        check("更新后仍停在关注管理页", "creators" in page.url, page.url)

        # 「⋯」菜单
        page.query_selector_all('[data-act="more"]')[0].click()
        page.wait_for_selector("[data-more-act]", timeout=10000)
        page.wait_for_timeout(500)
        more = page.eval_on_selector_all("[data-more-act]", "els => els.map(e => e.dataset.moreAct)")
        check("⋯ 菜单有 5 项", len(more) == 5, more)
        check("含「更新该作者作品」", "br-update" in more, more)
        check("含「设置主页链接」", "br-profile" in more, more)
        check("含「下载该作者全部作品」", "br-download-all" in more, more)
        check("含「删除作者信息」", "br-delete" in more, more)
        check("菜单里没有抖音专属的「复制主页链接」", "br-copy" not in more, more)
        page.screenshot(path=os.path.join(SHOT_DIR, "ks-4-creators-menu.png"))

        # 删除弹窗文案
        page.click('[data-more-act="br-delete"]')
        page.wait_for_selector(".danger-note", timeout=10000)
        page.wait_for_timeout(500)
        note = page.eval_on_selector(".danger-note", "e => e.innerText")
        check("删除弹窗提到「快手台账」", "台账" in note, note.replace("\n", " ")[:70])
        check("删除弹窗写明磁盘文件保留", "原样保留" in note)
        check("删除弹窗指明备份目录", "ks-backups" in note)
        check("删除弹窗提醒会重新下载", "重新下载" in note)
        page.screenshot(path=os.path.join(SHOT_DIR, "ks-5-creators-delete.png"))

        # 取消 → 一个请求都不发
        page.query_selector("[data-modal-close]").click()
        page.wait_for_timeout(600)
        check("点「取消」不发任何删除请求", len(sent["delete"]) == 0, len(sent["delete"]))

        # 设置主页弹窗（只看不存，避免污染真实映射文件）
        page.query_selector_all('[data-act="set-profile"]')[0].click()
        page.wait_for_selector("#__ksp_url", timeout=10000)
        page.wait_for_timeout(400)
        placeholder = page.get_attribute("#__ksp_url", "placeholder")
        check("设置主页弹窗给出链接格式示例", "kuaishou.com/profile" in (placeholder or ""), placeholder)
        page.query_selector("[data-modal-close]").click()
        page.wait_for_timeout(400)
        check("只看不存：没有写主页映射", len(sent["profile"]) == 0, len(sent["profile"]))

        # 一键更新的确认弹窗
        page.click("#cr-update-all")
        page.wait_for_timeout(700)
        mask_txt = page.eval_on_selector(".modal-mask", "e => e.innerText") \
            if page.query_selector(".modal-mask") else ""
        check("一键更新有确认弹窗", "一键更新" in mask_txt, mask_txt.replace("\n", " ")[:60])
        check("弹窗提到快手风控", "风控" in mask_txt)
        page.screenshot(path=os.path.join(SHOT_DIR, "ks-6-creators-update-all.png"))
        page.query_selector("[data-modal-close]").click()
        page.wait_for_timeout(400)

        # 切回抖音：卡片该恢复「下载」按钮
        page.query_selector_all(".srctab")[0].click()
        page.wait_for_timeout(1600)
        check("切回抖音后「下载」按钮回来",
              len(page.query_selector_all('[data-act="download"]')) > 0)
        check("切回抖音后状态条写明来自数据库",
              "工程数据库" in (page.eval_on_selector("#cr-syncbar", "e => e.innerText") or ""))

        # 收尾：全程不该有任何真发出去的写请求
        print()
        print("拦截统计：", {k: len(v) for k, v in sent.items()})
        check("抖音下载请求 0 个", len(sent["dy_download"]) == 0, len(sent["dy_download"]))
        check("删除请求 0 个", len(sent["delete"]) == 0, len(sent["delete"]))
        check("主页映射写入 0 个", len(sent["profile"]) == 0, len(sent["profile"]))
        check("快手下载请求恰好 1 个（就是那次点「更新」）",
              len(sent["ks_download"]) == 1, len(sent["ks_download"]))
        check("页面 JS 报错 0 条", len(errors) == 0, errors[:3])

        browser.close()

    print()
    print("=" * 66)
    print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
