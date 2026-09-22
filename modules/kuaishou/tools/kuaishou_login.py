# -*- coding: utf-8 -*-
"""
kuaishou_login.py v2 — 扫码自动获取快手 Cookie

v2 修复:
- 全量 Cookie 轮询(所有 kuaishou 域),不只查主域
- 双重校验: Cookie 登录项 + 页面内 GraphQL checkLogin
- 无缓冲输出(-u)确保后台日志实时可见
- 持久化会话(user_data_dir):登录态落盘,后续脚本直接复用
- 任意时刻按 Ctrl+C 退出; 窗口标题变化不影响检测
"""
import sys, io, os, time, json
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

# ---- 合并项目路径解析 ----
def _find_root():
    _here = os.path.dirname(os.path.abspath(__file__))
    for _d in (_here, os.path.dirname(_here), os.path.dirname(os.path.dirname(_here)),
               os.path.dirname(os.path.dirname(os.path.dirname(_here)))):
        if os.path.isdir(os.path.join(_d, "data", "kuaishou")):
            return _d
    return os.path.dirname(_here)

_ROOT = _find_root()
COOKIE_OUT = os.path.join(_ROOT, "data", "kuaishou", "cookie.txt")
PROFILE_DIR = os.path.join(_ROOT, "runtime", "kuaishou-login-profile")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
LOGIN_KEYS = ("passToken", "kuaishou.server.web_st", "kuaishou.server.web_day7_st", "userId")

def dump_all(cookies):
    keep = [c for c in cookies if "kuaishou" in (c.get("domain") or "")]
    s = "; ".join("%s=%s" % (c["name"], c["value"]) for c in keep)
    os.makedirs(os.path.dirname(COOKIE_OUT), exist_ok=True)
    with open(COOKIE_OUT, "w", encoding="utf-8") as f:
        f.write(s)
    return len(keep)

def main():
    wait_s = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, channel="msedge", headless=False,
            user_agent=UA, locale="zh-CN", viewport={"width": 1360, "height": 900},
            args=["--start-maximized"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        print("[1] 打开 kuaishou.com ...")
        try:
            page.goto("https://www.kuaishou.com/profile/" + "3xxjtkcmqqt2fqg", wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print("    goto warn:", str(e)[:100])
        page.wait_for_timeout(3000)
        for sel in ["text=登录", "button:has-text('登录')", "[class*='login']"]:
            try:
                loc = page.locator(sel).first
                if loc.is_visible():
                    loc.click(timeout=4000)
                    print("[2] 已点击登录按钮,请扫码")
                    break
            except Exception:
                continue
        print("[2] 请在窗口中用快手 App 扫码登录(最长等待 %d 秒)..." % wait_s)

        deadline = time.time() + wait_s
        saved = False
        tick = 0
        while time.time() < deadline:
            tick += 1
            try:
                cookies = ctx.cookies()
            except Exception:
                cookies = []
            names = {c["name"] for c in cookies}
            hit = [n for n in LOGIN_KEYS if n in names]
            if hit:
                print("[3] Cookie 命中登录项: %s" % ",".join(hit))
                time.sleep(4)
                cookies = ctx.cookies()
                n = dump_all(cookies)
                print("[4] 已保存 %d 项 Cookie -> %s" % (n, COOKIE_OUT))
                # 二次校验: 页面内 checkLogin
                try:
                    pg = ctx.pages[0] if ctx.pages else ctx.new_page()
                    pg.goto("https://www.kuaishou.com", wait_until="domcontentloaded", timeout=30000)
                    pg.wait_for_timeout(2500)
                    r = pg.evaluate("""
                        async () => {
                            const resp = await fetch('/graphql', {method: 'POST',
                                headers: {'Content-Type': 'application/json'}, credentials: 'include',
                                body: JSON.stringify({operationName: 'checkLogin', variables: {},
                                    query: 'query checkLogin { checkLogin }'})});
                            return await resp.text();
                        }
                    """)
                    print("[5] checkLogin:", r[:80])
                    ok = '"checkLogin":true' in r
                except Exception as e:
                    print("[5] checkLogin EXC:", str(e)[:80], "(仍按 Cookie 命中处理)")
                    ok = True
                if ok:
                    saved = True
                    break
                else:
                    print("    checkLogin=false, 继续等待扫码...")
            if tick % 10 == 0:
                print("    ...等待扫码 (%ds) cookies=%d" % (tick * 2, len(cookies)))
            time.sleep(2)
        try:
            ctx.close()
        except Exception:
            pass
    if saved:
        print("[DONE] 登录态已持久化到 %s 且 cookie.txt 已导出" % PROFILE_DIR)
        sys.exit(0)
    print("[TIMEOUT] 未检测到登录")
    sys.exit(3)

if __name__ == "__main__":
    main()
