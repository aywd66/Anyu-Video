# -*- coding: utf-8 -*-
"""端到端：快手标签页的**几何自检 + 配色取证**（只读，不改任何数据）。

跑法（需要桥接服务已在 127.0.0.1:8518 上跑）：
    python ops/restart-bridge.py
    runtime\\douyin-venv\\Scripts\\python.exe modules\\douyin\\webui\\tests\\test_ks_visual.py

为什么单独一个文件：test_ks_ui_browser.py 管的是**交互与写请求**（点按钮发什么请求），
这里管的是**渲染出来长什么样、有没有撑破布局** —— 两个问题、两套断言，混在一起
失败时定位会变慢。

两个断言背后的真实坑：
    1) **横向溢出**：来源标签 `.srctabs` 是新增的一行，窄屏上很容易把工具行挤出去。
       只看宽屏会漏，所以宽屏 + 420px 各测一次。
    2) **卡片配色**：`cover()` 原先只按作品封面的 URL 取色，快手作品**没有 cover 字段**
       → 上千张卡片会全部同色。修法是改用 `id` 哈希做种子。这里直接读实际渲染色，
       断言颜色种类够多 —— 光看代码"改了"不算数。
"""
import os
import sys
import tempfile

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8518"
SHOT_DIR = os.environ.get("KS_TEST_SHOT_DIR") or tempfile.gettempdir()
PASS = FAIL = 0

# 读 documentElement 的横向溢出 + 越界元素
GEO_JS = """() => {
    const de = document.documentElement;
    const bad = [];
    document.querySelectorAll('.content *').forEach(el => {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && (r.right > de.clientWidth + 1 || r.left < -1)) {
            bad.push((el.className || el.tagName) + ' @' + Math.round(r.right));
        }
    });
    return {sw: de.scrollWidth, cw: de.clientWidth, bad: bad.slice(0, 5),
            cards: document.querySelectorAll('.work-card').length};
}"""


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   " + label + (("  " + str(extra)) if extra else ""))
    else:
        FAIL += 1
        print("  [FAIL] " + label + (("  " + str(extra)) if extra else ""))


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1080})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        print("== A. 快手标签页渲染 ==")
        page.goto(BASE + "/#/archive", wait_until="load")
        page.wait_for_selector(".work-card", timeout=60000)
        page.wait_for_timeout(1500)

        tabs = page.query_selector_all(".srctab")
        check("来源标签存在且恰好 2 个（抖音/快手）", len(tabs) == 2, len(tabs))

        tabs[1].click()                                  # 切到快手
        page.wait_for_timeout(2500)
        check("切到快手后有作品卡", len(page.query_selector_all(".work-card")) > 0,
              len(page.query_selector_all(".work-card")))
        check("快手标签自身带数量且非空",
              (tabs[1].inner_text() or "").strip() not in ("", "快手"),
              (tabs[1].inner_text() or "").replace("\n", " "))

        # 快手卡片不该有「收藏」按钮（收藏是本地 localStorage 概念，快手侧没接）
        fav = page.evaluate(
            "() => Array.from(document.querySelectorAll('.work-card'))"
            ".filter(c => c.innerText.includes('收藏')).length")
        check("快手卡片没有「收藏」按钮", fav == 0, fav)

        print()
        print("== B. 配色确实按 id 打散（不是全同色）==")
        colors = page.evaluate("""() => {
            const set = new Set();
            document.querySelectorAll('.work-card .cover, .work-card [class*=cover]')
              .forEach(e => { const bg = getComputedStyle(e).backgroundImage
                                        || getComputedStyle(e).backgroundColor;
                              if (bg) set.add(bg); });
            return {n: set.size, cards: document.querySelectorAll('.work-card').length};
        }""")
        check("封面配色种类 >= 6（全同色说明 id 哈希没生效）",
              colors["n"] >= 6, "色种 %d / 卡 %d" % (colors["n"], colors["cards"]))

        print()
        print("== C. 宽屏几何 ==")
        geo = page.evaluate(GEO_JS)
        check("宽屏无横向溢出", geo["sw"] <= geo["cw"] + 1,
              "%d <= %d" % (geo["sw"], geo["cw"]))
        check("宽屏无越界元素", not geo["bad"], geo["bad"])
        page.screenshot(path=os.path.join(SHOT_DIR, "ks-7-archive-kuaishou.png"))

        print()
        print("== D. 窄屏(420px)几何：来源标签会不会把工具行挤坏 ==")
        page.set_viewport_size({"width": 420, "height": 900})
        page.wait_for_timeout(900)
        geo2 = page.evaluate(GEO_JS)
        check("窄屏无横向溢出", geo2["sw"] <= geo2["cw"] + 1,
              "%d <= %d" % (geo2["sw"], geo2["cw"]))
        check("窄屏无越界元素", not geo2["bad"], geo2["bad"])
        check("窄屏下仍能看到作品卡", geo2["cards"] > 0, geo2["cards"])
        page.screenshot(path=os.path.join(SHOT_DIR, "ks-9-archive-mobile.png"))

        print()
        print("== E. 关注管理页的快手标签 ==")
        page.set_viewport_size({"width": 1440, "height": 1080})
        page.goto(BASE + "/#/creators", wait_until="load")
        page.wait_for_timeout(2200)
        ctabs = page.query_selector_all("#cr-src .srctab")
        check("关注页来源标签 2 个", len(ctabs) == 2, len(ctabs))
        ctabs[1].click()
        page.wait_for_timeout(2200)
        # 作者卡片的稳定标识是 data-creator（.card 是通用类，列表/网格两种视图都有）
        n_cards = len(page.query_selector_all("[data-creator]"))
        check("快手作者卡片可见", n_cards > 0, n_cards)
        check("快手作者卡片上有「更新」或「设置主页」入口",
              len(page.query_selector_all('[data-act="update"], [data-act="set-profile"]')) > 0)
        cgeo = page.evaluate(GEO_JS)
        check("关注页(快手)无横向溢出", cgeo["sw"] <= cgeo["cw"] + 1,
              "%d <= %d" % (cgeo["sw"], cgeo["cw"]))
        page.screenshot(path=os.path.join(SHOT_DIR, "ks-8-creators-kuaishou.png"))

        check("页面 JS 报错 0 条", len(errors) == 0, errors[:3])
        browser.close()

    print()
    print("=" * 66)
    print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
