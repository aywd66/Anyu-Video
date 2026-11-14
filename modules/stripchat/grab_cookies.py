#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stripchat 浏览器 Cookie 抓取器
==============================

背景：stripchat 的 cam API（/api/front/v2/models/...）对非浏览器客户端一律
返回 HTTP 418 —— 主页能过、API 被拦，说明看的不是 IP 而是"是否真浏览器"
（JS 写入的 Cookie 集 / 指纹）。换代理节点无效。

思路与原版 OneRecord 的 StripChatBrowserCookieGrabber 一致：用真实 Edge
（playwright + 用户指定的 Chrome，有头模式 —— 无头会被识别）访问一次房间页，
让 JS 跑完、Cookie 落齐，再把整包 Cookie + UA 交给录制器复用。

用法（由桥接服务调用，需 kuaishou-env 的 playwright）：
    python grab_cookies.py <房间URL> <输出JSON路径> [超时秒=40]

输出 JSON：{"ua": "...", "cookies": {"name": "value", ...}}
失败时退出码非 0，stderr 给出中文原因。
"""
import json
import os
import sys
import time

DEFAULT_TIMEOUT = 40
CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def main():
    if len(sys.argv) < 3:
        print("用法: grab_cookies.py <房间URL> <输出JSON> [超时秒]", file=sys.stderr)
        return 2
    room_url = sys.argv[1].rstrip("/")
    out_path = sys.argv[2]
    timeout_s = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_TIMEOUT

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("未安装 playwright（应在 kuaishou-env 运行时里执行本脚本）", file=sys.stderr)
        return 3

    try:
        with sync_playwright() as p:
            # 有头模式：无头浏览器的特征会被反爬识别，宁可短暂弹出窗口。
            # 窗口开小一点，减少打扰。浏览器用用户指定的 Chrome。
            launch_kw = dict(headless=False,
                             args=["--window-size=560,420", "--no-first-run"])
            if os.path.exists(CHROME_EXE):
                launch_kw["executable_path"] = CHROME_EXE
            else:
                launch_kw["channel"] = "chrome"
            browser = p.chromium.launch(**launch_kw)
            try:
                ctx = browser.new_context(
                    viewport={"width": 540, "height": 380},
                    locale="en-US",
                )
                page = ctx.new_page()
                page.goto(room_url, wait_until="domcontentloaded", timeout=timeout_s * 1000)

                # 等页面 JS 把 Cookie 写齐：轮询直到出现关键 Cookie 或超时。
                # cf_clearance 只在被质询时才有；多数时候等 __cf_bm 稳定即可。
                deadline = time.time() + timeout_s
                got_key = None
                while time.time() < deadline:
                    cookies = {c["name"]: c["value"] for c in ctx.cookies()}
                    if "cf_clearance" in cookies:
                        got_key = "cf_clearance"
                        break
                    if "__cf_bm" in cookies and "_cfuvid" in cookies:
                        # 再给它几秒，让页面自己的 JS Cookie 也写完
                        time.sleep(4)
                        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
                        got_key = "__cf_bm+_cfuvid"
                        break
                    time.sleep(1)
                cookies = {c["name"]: c["value"] for c in ctx.cookies()}
                ua = page.evaluate("navigator.userAgent")
                if not cookies:
                    print("页面已打开但没有拿到任何 Cookie", file=sys.stderr)
                    return 4
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump({"ua": ua, "cookies": cookies,
                               "via": got_key or "partial",
                               "grabbed_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                              f, ensure_ascii=False, indent=1)
                return 0
            finally:
                browser.close()
    except Exception as e:
        print("浏览器抓取失败：%s" % e, file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
