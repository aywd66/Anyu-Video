# -*- coding: utf-8 -*-
"""端到端：真实浏览器里点封面能播放（作品档案 / 任务记录页）。

跑法（需要桥接服务已在 127.0.0.1:8518 上跑；playwright 装在工程 venv 里）：
    python ops/restart-bridge.py
    runtime\\douyin-venv\\Scripts\\python.exe modules\\douyin\\webui\\tests\\test_media_playback_browser.py

为什么用 msedge 而不是 playwright 自带的 chromium：
    内置 chromium 不含 H.264 等专有编解码器，抖音产物是 mp4/H.264，
    拿它测会「加载失败」—— 那是浏览器的锅，不是页面的锅，会把结论带偏。

为什么必须走真实浏览器：接口层全绿、页面肉眼也正常，仍可能藏着只在浏览器里
    才暴露的问题。写这一版时就真抓到过：`#mp-reveal` 按钮在 `.modal__foot` 里，
    代码却用 `.modal__body` 去 querySelector → null → 绑定抛异常。
"""
import json
import os
import sys
import tempfile

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8518"
URL = BASE + "/#/archive"
# 截图默认落系统临时目录，别往测试目录里堆图；
# 想留证据时用 MEDIA_TEST_SHOT_DIR 指定一个目录即可（本轮就是靠这两张图肉眼确认的）。
SHOT_DIR = os.environ.get("MEDIA_TEST_SHOT_DIR") or tempfile.gettempdir()
PASS = FAIL = 0


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
        browser = p.chromium.launch(channel="msedge", headless=True,
                                    args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append("console.error: " + m.text)
                if m.type == "error" else None)

        page.goto(URL, wait_until="load")
        page.wait_for_selector(".work-card", timeout=30000)
        page.wait_for_timeout(2500)                    # 等封面与接口到位

        cards = page.query_selector_all(".work-card")
        marks = page.query_selector_all(".cover__play")
        print("== 1. 作品档案渲染 ==")
        print("  卡片 %d 张，封面带「可播放」标记 %d 张" % (len(cards), len(marks)))
        check("列表渲染出卡片", len(cards) > 0, len(cards))
        check("封面出现可播放标记", len(marks) > 0, len(marks))
        page.screenshot(path=os.path.join(SHOT_DIR, "shot-1-archive.png"))

        print("\n== 2. 点封面播放视频 ==")
        card = page.query_selector(".work-card:has(.cover__play--video) .cover")
        if not card:
            check("找到视频卡", False)
        else:
            card.click()
            page.wait_for_selector(".modal-mask", timeout=8000)
            info = page.evaluate("""() => {
                const v = document.querySelector('#mp-video');
                return v ? {src: v.getAttribute('src'), rs: v.readyState,
                            err: v.error ? v.error.code : null} : null;
            }""")
            check("弹窗里出现 <video>", info is not None, json.dumps(info, ensure_ascii=False))
            check("src 指向本地媒体接口",
                  bool(info) and "/api/bridge/media?id=" in (info["src"] or ""),
                  info and info["src"])
            try:
                page.wait_for_function(
                    "() => { const v=document.querySelector('#mp-video');"
                    " return v && v.readyState >= 2 && v.videoWidth > 0; }", timeout=20000)
            except Exception as e:
                print("  等待解码超时:", str(e)[:120])
            d = page.evaluate("""() => {
                const v = document.querySelector('#mp-video');
                return v ? {w: v.videoWidth, h: v.videoHeight, d: v.duration} : null;
            }""")
            check("浏览器真的解码出画面", bool(d) and d["w"] > 0,
                  d and ("%dx%d" % (d["w"], d["h"])))
            check("拿到时长", bool(d) and d["d"] > 0, d and d["d"])
            page.evaluate("() => document.querySelector('#mp-video').play().catch(()=>{})")
            page.wait_for_timeout(2500)
            ct = page.evaluate("() => document.querySelector('#mp-video').currentTime")
            check("时间在推进（真的在播）", ct and ct > 0.3, ct)
            page.screenshot(path=os.path.join(SHOT_DIR, "shot-2-player.png"))
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)

        print("\n== 3. 图文作品翻页 ==")
        card = page.query_selector(".work-card:has(.cover__play--image) .cover")
        if not card:
            print("  （本页没有图集作品，跳过）")
        else:
            card.click()
            page.wait_for_selector(".modal-mask", timeout=8000)
            page.wait_for_timeout(1500)
            g = page.evaluate("""() => {
                const i = document.querySelector('#mp-img');
                return i ? {nw: i.naturalWidth, nh: i.naturalHeight} : null;
            }""")
            check("图片加载成功", bool(g) and g["nw"] > 0, g and ("%dx%d" % (g["nw"], g["nh"])))
            nxt = page.query_selector(".media-nav--next")
            if nxt:
                nxt.click()
                page.wait_for_timeout(900)
                txt = page.query_selector("#mp-count").inner_text()
                check("翻页生效", txt.startswith("2 /"), txt)
            page.screenshot(path=os.path.join(SHOT_DIR, "shot-3-gallery.png"))
            page.keyboard.press("Escape")

        print("\n== 4. 页面报错 ==")
        real = [e for e in errors if "favicon" not in e.lower()]
        for e in real[:6]:
            print("   !", e[:160])
        check("无 JS 报错", not real, len(real))
        browser.close()

    print("\n通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("截图目录: %s" % SHOT_DIR)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
