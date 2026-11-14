# -*- coding: utf-8 -*-
"""
kuaishou_fast_dl.py — 快手主页全量下载（并行加速版）

相比串行版的提速点（画质选择逻辑完全不变，仍取 manifest 最优码率轨道）：
1. 直链预取阶段：主线程批量调详情接口（保持约 1 个/秒 的温和节奏，防风控），
   先把全部作品的下载地址拿齐；
2. 下载阶段：ThreadPoolExecutor 多线程并行 yt-dlp（默认 4 线程），互不影响；
3. 固定 1.2s 等待移除，改为任务间 0.25s 错峰。

用法:
  python kuaishou_fast_dl.py [输出目录] [--workers 4] [--sleep 0.25] [--limit N] [--recollect]

前置: E:\\录屏\\spxz\\login-profile 内有扫码登录态（kuaishou_login.py 生成）
"""
import sys, io, os, json, re, shutil, time, subprocess, argparse, threading, atexit
from concurrent.futures import ThreadPoolExecutor, as_completed

# 风控守卫（同目录）：跨进程的「冷却 + 当日预算」。见 ks_guard.py 顶部的实测时间线。
# ⚠️ 导入失败**绝不能让下载器停摆** —— 守卫是保护，不是依赖。
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ks_guard
except Exception:
    ks_guard = None

# ---- Merged-project path resolution (auto-locate repo root containing data/kuaishou) ----
def _find_root():
    _here = os.path.dirname(os.path.abspath(__file__))
    for _d in (_here, os.path.dirname(_here), os.path.dirname(os.path.dirname(_here)),
               os.path.dirname(os.path.dirname(os.path.dirname(_here)))):
        if os.path.isdir(os.path.join(_d, "data", "kuaishou")):
            return _d
    return os.path.dirname(_here)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

# 产物根目录可被 KS_OUTDIR 覆盖（启动台「产物目录」改的就是它；与 kuaishou_headless 同口径）
_DEFAULT_OUT = os.path.join(_find_root(), "data", "kuaishou")
OUTDIR = os.environ.get("KS_OUTDIR") or _DEFAULT_OUT
# 登录态 profile 可被 KS_PROFILE 覆盖 —— 这是「多个 ck」唯一的实现方式。
# ⚠️ 换一份 profile（= 换一个 --user-data-dir）才会拿到新的 `did`（设备 cookie），
#    风控桶是按 did 算的，所以**只有在换 profile 时**多 ck 才有意义。
#    同一个 profile 里登出再登另一个号：`userId` 变、`did` 不变 → 还是同一个桶。
PROFILE = (os.environ.get("KS_PROFILE")
           or os.path.join(_find_root(), "runtime", "kuaishou-login-profile"))
UID_3X = "3xxjtkcmqqt2fqg"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
CDP = "http://127.0.0.1:9222"
# ⚠️ 登录用的 Edge profile **永远固定在默认目录**，绝不跟着产物目录搬走：
#    它是登录态（扫码成果），搬到新盘会让用户莫名其妙地「又要扫一次码」，
#    还会在新位置留下第二份 profile。产物能换地方，登录态不能。
EDGE_PROFILE = os.path.join(_DEFAULT_OUT, "edge-manual-profile")

DETAIL_Q = ("query visionVideoDetail($photoId: String, $page: String, $webPageArea: String) { "
            "visionVideoDetail(photoId: $photoId, page: $page, webPageArea: $webPageArea) { status type "
            "photo { photoUrl photoH265Url caption duration "
            "manifest { adaptationSet { representation { id qualityType width height avgBitrate url } } } } } }")

def sanitize(name):
    return re.sub(r'[\\/:*?"<>|#\s]+', "_", name).strip("_")[:60] or "kuaishou_video"

# ---- 风控信号判定（2026-10-03 实测校准）------------------------------------
# ⚠️⚠️ **风控有「两种长相」，少认一种，刹车就等于没有。**
#
# 快手的风控在这条 GraphQL 上不是只回一种码：
#   - `status_208005`  —— 明确的验证拦截（`Need captcha`）；
#   - `status_None`    —— `visionVideoDetail` 整个字段缺失/为空，接口**根本没给状态码**。
# 两者是**同一个风控的两种表现**（挡下来的时候，接口有时给码、有时直接给空壳）。
#
# 旧代码只认 `208005`，而且每遇到一个别的状态就把 `block_streak` 清零。后果是
# 刹车条件（「连续 12 个 208005」）在真实数据下**永远达不到**：
# 2026-10-03 台账里的实际顺序是
#     `208005×9 | None×2 | 208005×1 | None×6 | 208005×8`
# 纯 208005 的最长连续只有 **9**。于是 `note_block()` 一次都没被调用 →
# 守卫的 `block_count` 恒为 0、`blocked_until` 恒为 0.0 → 跨进程冷却从未落盘 →
# 每次运行都重新撞墙、重新失败 26 次。用户看到的症状就是「快手下载失败」，
# 而日志里**没有任何风控冷却记录**，看起来像「偶发网络问题」。
#
# 修法：两种信号都算进连续计数，且**不因 status_None 清零**。
# 只有真正的内容级失效（`status_1040` 已删除）才该清零 —— 它跟风控无关。
RISK_STATUS = ("208005", "None")
BLOCK_STREAK_TRIP = 12


def _is_risk_status(st):
    """这个 status 是不是「接口风控信号」（区别于内容真的失效）。

    `st` 是 `str(vd.get("status"))`，所以「字段缺失」会变成字符串 `"None"` ——
    这正是被漏认的那一种，别把它当普通失败。
    """
    return str(st) in RISK_STATUS

def cdp_alive():
    import urllib.request
    try:
        urllib.request.urlopen(CDP + "/json/version", timeout=3)
        return True
    except Exception:
        return False

def ensure_edge():
    if cdp_alive():
        print("[edge] 调试实例已在运行")
        return
    print("[edge] 启动调试 Edge ...")
    subprocess.Popen([EDGE, "--remote-debugging-port=9222", "--user-data-dir=" + EDGE_PROFILE,
                      "--no-first-run", "about:blank"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        time.sleep(1)
        if cdp_alive():
            print("[edge] 就绪")
            return
    raise SystemExit("[edge] 启动失败")

def inject_cookies(ctx, cookies):
    inject = []
    for c in cookies:
        if "kuaishou" not in (c.get("domain") or ""):
            continue
        cc = {"name": c["name"], "value": c["value"], "domain": c["domain"], "path": c.get("path", "/")}
        if c.get("expires", -1) > 0:
            cc["expires"] = c["expires"]
        if c.get("httpOnly"):
            cc["httpOnly"] = True
        if c.get("secure"):
            cc["secure"] = True
        inject.append(cc)
    ctx.add_cookies(inject)
    return len(inject)

def dom_collect(pg, limit=0):
    """收集作者主页的全部作品 ID。

    `limit>0` 时收够就停 —— 由风控守卫传入当日剩余额度。**必须能提前收手**：
    2026-09-25 那次就是一口气深滚了 1790 个，之后详情接口才开始全灭
    （对照：同一天早些时候只收集 55 个时，55 个详情调用 0 失败）。

    ⚠️⚠️ **必须两阶段，只滚轮是不够的。**
    快手作者主页是**虚拟列表** `ul.feed-list`：
      - 滚轮到底能把 `<li>` 全部建出来（实测 160 个），
      - 但卡片封面图是**懒加载**的，只有真进过视口的项才填上
        `<img src="...clientCacheKey=<photoId>...">`；
        滚得太快 → 那批 `<img src="">` 全空 → **一个 ID 都抠不到**。
    实测只滚轮 = 124 个；加「逐项 scrollIntoView」= **160/160**。
    """
    works = {}
    def snap():
        pairs = pg.evaluate("""
            () => {
                const ids = new Map();
                // ① 旧版结构：作品卡是 <a href="/short-video/<id>">
                document.querySelectorAll('a[href*="/short-video/"]').forEach(a => {
                    const m = a.href.match(/\\/short-video\\/([0-9A-Za-z]+)/);
                    if (m) { const t=(a.getAttribute('title')||a.textContent||'').trim(); ids.set(m[1], t.slice(0,60)); }
                });
                // ② 新版结构（快手 PC 改版后）：卡是 <div>，但封面图带 clientCacheKey=<photoId>
                document.querySelectorAll('img[src*="clientCacheKey="]').forEach(img => {
                    const m=(img.src||'').match(/clientCacheKey=([0-9A-Za-z]+)[._\\-]/);
                    if (m && !ids.has(m[1])) ids.set(m[1], (img.getAttribute('alt')||'').trim().slice(0,60));
                });
                return Array.from(ids.entries());
            }
        """)
        for pid, cap in pairs:
            if pid not in works:
                works[pid] = {"id": pid, "caption": cap}
    UL = "() => (document.querySelector('ul.feed-list') || {}).children?.length || 0"

    pg.bring_to_front()
    pg.reload(wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(9000)
    snap()
    print("[collect] 首屏:", len(works))

    # ── 阶段 1：滚轮撑满列表 ──
    last, last_n, stable = 0, 0, 0
    for i in range(600):
        try:
            pg.evaluate("() => { window.scrollTo(0, document.documentElement.scrollHeight); "
                        "document.querySelectorAll('*').forEach(e=>{ "
                        "if (e.scrollHeight > e.clientHeight + 50) e.scrollTop = e.scrollHeight; }); }")
        except Exception:
            pass
        pg.wait_for_timeout(900)
        before = len(works)
        snap()
        n = pg.evaluate(UL)
        if limit and len(works) >= limit:
            print("[collect] 已收满当日额度 %d 个，停止滚动"
                  "（不是失败 —— 是守卫主动刹车，剩下的明天继续）" % limit)
            break
        if len(works) == before == last and n == last_n:
            stable += 1
            if stable >= 15:
                break
        else:
            stable = 0
        last, last_n = len(works), n
    total = pg.evaluate(UL)
    print("[collect] 阶段1 滚轮结束: 列表项=%d 已收集=%d" % (total, len(works)))

    # ── 阶段 2：逐项点亮（补懒加载，**关键**）──
    # 受额度限制时只点亮前 limit 项：阶段 2 每一项都会真的滚到视口、触发封面加载，
    # 滚满整个大列表本身就是要打的请求量（1790 项那次就是这么把会话打到被降权的）。
    _upto = min(total, limit) if limit else max(total, 0)
    for idx in range(_upto):
        try:
            pg.evaluate("""(idx) => {
                const ul = document.querySelector('ul.feed-list');
                if (!ul) return;
                const li = ul.children[idx];
                if (li) li.scrollIntoView({block: 'center'});
            }""", idx)
        except Exception:
            pass
        pg.wait_for_timeout(320)
        snap()
    print("[collect] 阶段2 逐项点亮完成: %d" % len(works))

    # ── 阶段 3：兜底 ──
    for _ in range(5):
        try:
            pg.evaluate("() => { document.querySelectorAll('*').forEach(e=>{ "
                        "if (e.scrollHeight > e.clientHeight + 50) e.scrollTop = e.scrollHeight; }); }")
        except Exception:
            pass
        pg.wait_for_timeout(700)
        snap()
    print("[collect] 阶段3 结束: 共 %d 个作品" % len(works))
    return works

def cleanup_debug_edge():
    """只关闭脚本拉起的调试 Edge(edge-manual-profile),不动用户自己开的 Edge"""
    try:
        cleanup_ps1 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kill_debug_edge.ps1")
        if os.path.exists(cleanup_ps1):
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", cleanup_ps1],
                           capture_output=True, timeout=60)
            print("[cleanup] 调试 Edge 已关闭(不影响你正常使用的 Edge)")
    except Exception:
        pass

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir", nargs="?", default=OUTDIR)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--sleep", type=float, default=0.25)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--recollect", action="store_true")
    ap.add_argument("--list", default="", help="指定作品清单 JSON(id/caption/duration),跳过收集")
    ap.add_argument("--profile", default="", help="指定作者主页 URL,滚动收集全量作品")
    args = ap.parse_args()
    outdir = args.outdir
    os.makedirs(outdir, exist_ok=True)

    # 所有退出路径(提前 return/报错/正常结束)都保证清理调试 Edge。
    # ⚠️⚠️ **只在「进程正常结束」时有效**：任务被取消走的是 `taskkill /F`，atexit 不执行。
    #    2026-09-25 实测后果：9222 上的自动化 Edge 留在系统里（9 个进程、约 250MB），
    #    还把 `edge-manual-profile` 锁住，并且从**上一次运行**的脏页面继续发请求。
    #    所以注册必须放在下面「守卫刹车」之前 —— 那条提前 return 的路径也要走到清理。
    atexit.register(cleanup_debug_edge)

    # ── 身份（风控桶）：**必须在查冷却之前**确定 ────────────────────────────
    # 桶 = 设备（did）。冷却与当日额度都是**按桶**存的：桶没定就问，等于把别的
    # 设备的冷却算到自己头上，或者反过来把自己的额度送出去。
    # 这里只查注册表（`data\kuaishou\idents.json`），**不启动浏览器** ——
    # 冷却期要 0.5 秒内收工，为了读 cookie 去拉一个浏览器就把这个性质毁了。
    _bk = ks_guard.BUCKET_DEFAULT if ks_guard else ""
    if ks_guard:
        _bk = ks_guard.set_identity_for_profile(PROFILE)
        if _bk == ks_guard.BUCKET_DEFAULT:
            print("[guard] 本窗口身份未登记（第一次用这份登录态）—— 先按 default 算，"
                  "读到 cookie 后会登记并改按真桶算。")

    # ── 风控刹车（第一道）：冷却没到就直接收工 ────────────────────────────
    # 旧行为是「撞墙后原地等 12 分钟、最多等 8 轮」（96 分钟）。2026-09-25 实测
    # 4 小时 26 分钟后仍然是封的 —— 那 96 分钟纯属白等，而且每等一轮都要再打
    # 一轮请求，可能反过来把惩罚窗口往后推。所以这里改成：认了，收工，几时再来
    # 由守卫写在盘上，跨进程跨任务都算数。
    _climit = 0
    if ks_guard:
        print(ks_guard.budget_line())
        if ks_guard.blocked_info()[0]:
            print(ks_guard.cooling_message())
            print("[完成] 退出码 0（守卫主动刹车，不是错误）")
            return
        _climit = ks_guard.collect_left()

        # ── 风控刹车（第一道半）：批量独占 ──────────────────────────────────
        # 「多开几个窗口并行跑会不会更快」——不会，而且会更糟：
        #  ① 预算是「读-判断-写」，两个进程会各领一份 400 → 实际打 800，
        #     守卫就被自己的第二个窗口绕过去了；
        #  ② 两路并行打接口，正是 09-25 撞出 status_208005 的那个形状。
        # 所以批量必须先领租约，**单条不领**（单条在冷却期实测可用）。
        # ⚠️ 由 `kuaishou_headless.py --resume` 拉起来的子进程会带 KS_RUN_TOKEN
        #    环境变量，直接借用上层的租约，不会跟自己的父进程打架。
        _ok, _holder = ks_guard.acquire_run("fast_dl")
        if not _ok:
            print(ks_guard.conflict_message(_holder))
            # ⚠️ 加这条提示，是因为踩过一次：日志说「另一个批量任务在跑」，
            #    而那个 PID 其实就是**自己的父进程**（调用方裸调 subprocess.run，
            #    漏传了 KS_RUN_TOKEN）。没有这句，人会去找一个根本不存在的窗口。
            print("[guard] ⚠️ 若本进程是某个任务**自己**拉起的子进程，那上面的「占用者」"
                  "就是它自己的父进程 —— 原因是租约 token 没传下来。"
                  "调用方应使用 `kuaishou_headless._spawn_fast_dl()` 拉起本脚本，"
                  "裸 `subprocess.run(cmd)` 会漏掉 KS_RUN_TOKEN。")
            print("[完成] 退出码 0（守卫主动刹车，不是错误）")
            return

    from playwright.sync_api import sync_playwright
    cookies = []
    with sync_playwright() as p:
        c0 = p.chromium.launch_persistent_context(PROFILE, channel="msedge", headless=True,
                                                  user_agent=UA, locale="zh-CN", viewport={"width": 1280, "height": 900})
        cookies = c0.cookies()
        c0.close()

    if ks_guard:
        # 读到真 cookie 了 → 登记「这份 profile 是哪个设备/哪个号」，下次启动
        # 就能免浏览器查到。首次跑时会发现「真桶 ≠ 启动时假定的 default」，
        # 那时把口径换过去（补问真桶冷却 + 租约挪到真桶）。
        _bk2 = ks_guard.remember_identity(PROFILE, cookies)
        if _bk2 and _bk2 != _bk:
            _ok2, _note = ks_guard.switch_bucket(_bk2, "fast_dl")
            if _note:
                print(_note)
            if not _ok2:
                print("[完成] 退出码 0（守卫主动刹车，不是错误）")
                return
            _climit = ks_guard.collect_left()          # 额度按真桶重算
    ensure_edge()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0]
        n = inject_cookies(ctx, cookies)
        print("[login] 注入 Cookie:", n)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()

        wpath = os.path.join(OUTDIR, "works_list.json")
        if args.list:
            worklist = json.load(open(args.list, encoding="utf-8-sig"))
            print("[list] 使用指定清单: %s (%d 个作品)" % (args.list, len(worklist)))
        elif args.recollect or args.profile:
            # 按指定作者主页 URL 滚动收集全量(支持任意作者,不再固定 UID_3X)
            if ks_guard and _climit <= 0:
                print(ks_guard.budget_exhausted_message())
                return
            profile_url = args.profile or ("https://www.kuaishou.com/profile/" + UID_3X)
            pg.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
            works = dom_collect(pg, limit=_climit)
            if ks_guard:
                ks_guard.commit_collect(len(works))
            if args.profile:
                wpath = os.path.join(OUTDIR, "works_list_" + re.sub(r"[^0-9A-Za-z]", "", profile_url.rsplit("/", 1)[-1]) + ".json")
            with open(wpath, "w", encoding="utf-8") as f:
                json.dump(sorted(works.values(), key=lambda x: x["id"]), f, ensure_ascii=False, indent=1)
            print("[collect] 重新收集:", len(works), "->", wpath)
            worklist = json.load(open(wpath, encoding="utf-8"))
        elif not os.path.exists(wpath):
            if ks_guard and _climit <= 0:
                print(ks_guard.budget_exhausted_message())
                return
            pg.goto("https://www.kuaishou.com/profile/" + UID_3X, wait_until="domcontentloaded", timeout=60000)
            works = dom_collect(pg, limit=_climit)
            if ks_guard:
                ks_guard.commit_collect(len(works))
            with open(wpath, "w", encoding="utf-8") as f:
                json.dump(sorted(works.values(), key=lambda x: x["id"]), f, ensure_ascii=False, indent=1)
            print("[collect] 重新收集:", len(works))
            worklist = json.load(open(wpath, encoding="utf-8"))
        else:
            worklist = json.load(open(wpath, encoding="utf-8"))

        # 台账去重(作者子目录无独立台账时回落主台账)
        log_path = os.path.join(outdir, "download_log.json")
        if not os.path.exists(log_path):
            log_path = os.path.join(OUTDIR, "download_log.json")
        log = json.load(open(log_path, encoding="utf-8")) if os.path.exists(log_path) else []
        def _permanent_fail(reason):
            """这条失败是不是「重试也没用」—— 只有是，才丢进永久跳过名单。

            ⚠️⚠️ **status_208005 绝不能算永久失败**（2026-09-25 实证）：
            连着跑第二轮收集后，同一批 20 个里 **18 个报 208005、1 个成功** ——
            这是**接口风控**，作品好好的。旧代码把它塞进 `skip_ids`，后果是这 18 部
            **以后永远不再重试**，且日志里连句提示都没有 = 静默丢片。
            只有 status_1040（内容不存在/已删除，实测重跑始终同一结果）才跳过。
            """
            return "status_1040" in str(reason or "")

        done_ids, done_fp, skip_ids = set(), set(), set()
        for r in log:
            pid = str(r.get("photoId") or "")
            if r.get("ok"):
                done_ids.add(pid)
                done_fp.add((re.sub(r"[\s#]+", "", (r.get("caption") or "")).lower()[:30], int(r.get("duration_ms") or 0)))
            elif _permanent_fail(r.get("reason", "")):
                skip_ids.add(pid)

        # 统一按台账去重,再应用 limit
        #
        # ⚠️⚠️ **同一个作品有两种 ID 写法**，只按 ID 字符串去重会漏判：
        #     `19 位纯数字`（旧 SSR 正则 `feed_photo|<id>|` 抠到的）
        #     `3x…短 ID`（改版后封面图 clientCacheKey 抠到的）
        #    实测二者调 visionVideoDetail 返回**同一条 caption + 同一 duration**。
        #    → 曾因此把同一批视频下了两遍（15 个重复文件，白占空间）。
        #
        #    所以第二重判据用 (caption, duration) 指纹 —— 两种 ID 写法下它是恒定的。
        # ⚠️⚠️ **判重复必须「跨 ID 形态」才可信。** 最初我写成任意指纹相同即判重复，
        #    实测**误判**：有两条**不同**视频文案相同、时长只差 12ms
        #    （`3xasj4qzn3tuihu` 2269601B vs `3xrt93ntgc2ivzm` 2651873B），
        #    按指纹它们会被当成同一部 → **静默漏掉一条真实视频**。
        #    「少下一部」比「多下一份」严重得多（后者可见可删，前者无声无息）。
        #    → 所以限定：**只有当台账里存在「另一种 ID 形态」的相同指纹时才判重复**。
        #      这正是「同一作品两种写法」的确凿特征；同为短 ID 的一律不猜。
        def _fp(caption, dur):
            return (re.sub(r"[\s#]+", "", (caption or "")).lower()[:40], int(dur or 0))

        def _kind(pid):
            """ID 形态：n=19 位纯数字（旧 SSR 抠的），s=3x… 短 ID（封面图抠的），?=未知形态。"""
            pid = str(pid or "")
            if pid.isdigit():
                return "n"
            if re.fullmatch(r"3x[0-9a-z]+", pid):
                return "s"
            return "?"

        # 台账指纹 -> 该指纹出现过哪些 ID 形态
        fp_kinds = {}
        for r in log:
            if not r.get("ok"):
                continue
            k = _fp(r.get("caption"), r.get("duration_ms"))
            fp_kinds.setdefault(k, set()).add(_kind(r.get("photoId")))

        def _is_duplicate(w):
            cap = (w.get("caption") or "").strip()
            dur = int(w.get("duration") or 0)
            # ⚠️ 安全阀：DOM 抓的 caption 常是空串，不设这个判断会让所有条目塌缩成
            #    ("", 0) 而互相判重 → 第一条之后全部被跳过，**静默什么也不下**。
            if not (cap and dur > 0):
                return False
            kinds = fp_kinds.get(_fp(cap, dur))
            if not kinds:
                return False
            mine = _kind(w.get("id"))
            return any(k != mine for k in kinds)  # 只认「跨形态」重复

        todo = [w for w in worklist
                if w["id"] not in done_ids and w["id"] not in skip_ids and not _is_duplicate(w)]
        if args.limit:
            todo = todo[: args.limit]
        n_fp = sum(1 for w in worklist if _is_duplicate(w))
        if n_fp:
            print("[plan] 跨形态去重: 跳过 %d 个（同一作品的旧数字型 ID 已下过）" % n_fp)

        # ── 风控刹车（第二道）：详情接口的当日额度 ────────────────────────
        # 这一道才是拦「一口气要几千个」的地方。对照数据：连调 55 个 0 失败，
        # 而收集 1790 个之后第 12 个详情就全线 208005。额度用完就停，明天接着跑；
        # 台账保证不会重下，所以「拆到多天」不损失任何东西。
        if ks_guard:
            _want = len(todo)
            _allow = ks_guard.allow_detail(_want)
            # ⚠️ `_want == 0`（台账去重后没有待下的）时 allow_detail(0) 恒为 0，
            #    旧代码会把「无需下载」误报成「额度已用完」。只有真的想要额度
            #    却拿不到时才算耗尽。
            if _want > 0 and _allow <= 0:
                print(ks_guard.budget_exhausted_message())
                return
            if _allow < _want:
                print("[guard] 今日详情额度只剩 %d 次 → 本次只跑前 %d 个（清单里还有 %d 个）"
                      % (_allow, _allow, _want - _allow))
                print("[guard] 这是刻意的：让平台看到的是「慢慢地要 %d 个」，"
                      "而不是「一口气要 %d 个」—— 后者正是 2026-09-25 撞墙的形态。"
                      % (_allow, _want))
                todo = todo[:_allow]

        print("[plan] 待处理 %d | workers=%d sleep=%.2fs" % (len(todo), args.workers, args.sleep))
        if not todo:
            # ⚠️ 这里必须说清「为什么是 0」，否则用户会把它读成「下载失败」。
            #    最常见原因就是重复提交同一链接 —— 台账已全部去重。
            n_all, n_done, n_skip = len(worklist), len(done_ids), len(skip_ids)
            print("[done] 无需下载：清单共 %d 个，已下载 %d 个、已跳过 %d 个（台账去重生效）"
                  % (n_all, n_done, n_skip))
            print("[done] 如需重新下载，请先在台账中移除对应记录，或换一个作者链接。")
            return

        # ---- 阶段1: 主线程预取全部直链(约1个/秒防风控) ----
        # ⚠️ 不复用上一次运行留下的页面。调试 Edge 是常驻的（任务进程被 kill 时
        #    atexit 不执行，它就留在那儿），所以 `ctx.pages` 里很可能躺着一个几小时
        #    前就已经进风控、甚至挂着验证浮层的旧页面 —— 在它上面 fetch 会一直拿到
        #    208005，看起来就像「风控还没解」，其实只是页面脏了。
        #    每次运行另开一页；只关掉自动化留下的 /short-video/ 旧页，不动别的。
        for x in list(ctx.pages):
            try:
                if "/short-video/" in (x.url or ""):
                    x.close()
            except Exception:
                pass
        dl_pg = ctx.new_page()
        dl_pg.goto("https://www.kuaishou.com/short-video/" + todo[0]["id"], wait_until="domcontentloaded", timeout=60000)
        dl_pg.wait_for_timeout(4000)

        def fetch_detail(pid):
            r = dl_pg.evaluate(
                """async (args) => {
                    const resp = await fetch('/graphql', {method: 'POST',
                        headers: {'Content-Type': 'application/json'}, credentials: 'include',
                        body: JSON.stringify({operationName: 'visionVideoDetail',
                            variables: {photoId: args.pid, page: 'main', webPageArea: ''},
                            query: args.q})});
                    return await resp.text();
                }""", {"pid": pid, "q": DETAIL_Q})
            return json.loads(r)

        # 自适应限流:连续 2 个 status_None 则降速(加倍 sleep,上限 5s),成功 10 次后逐步恢复
        t0 = time.time()
        jobs, failed = [], []
        cur_sleep = args.sleep
        fail_streak = 0
        ok_streak = 0
        dup_skip = 0
        block_streak = 0
        blocked_hit = False   # 本轮是否撞过风控（决定跑完要不要清冷却）
        n_calls = 0          # 实际打出去的详情请求数（成功失败都算），用来核销当日额度
        n_risk = 0           # 本轮累计的风控信号数（含 208005 与 status_None）
        i = 0
        while i < len(todo):
            w = todo[i]
            pid = w["id"]
            try:
                d = fetch_detail(pid)
                n_calls += 1
                vd = ((d.get("data") or {}).get("visionVideoDetail") or {})
                ph = vd.get("photo") or {}
                if vd.get("status") != 1 or not ph:
                    fail_streak += 1
                    ok_streak = 0
                    st = str(vd.get("status"))
                    # ⚠️ 208005 是**接口风控**，不是作品失效 —— 见 `_permanent_fail` 的说明。
                    #    连着撞到一定数量就说明这条路当下走不通了，继续按 5s/个硬撑
                    #    纯属浪费时间（5619 个 × 5s ≈ 8 小时）。
                    #    → 立即收手：已就绪的先下，剩下的**不写失败台账**，留着下次安全重试。
                    #
                    # ⚠️⚠️ **风控信号有「两种长相」，必须一起算**（2026-10-03 实测踩到）：
                    #     `status_208005`（明确的验证拦截）与 `status_None`
                    #     （`visionVideoDetail` 整个字段缺失/为空，接口没给状态码）
                    #     **是同一个风控的两种表现**。台账里实测的顺序是
                    #     `208005×9 | None×2 | 208005×1 | None×6 | 208005×8` ——
                    #     纯 208005 的**最长连续只有 9**，而旧代码要求连续 12 个才刹车，
                    #     并且每遇到一个 status_None 就把计数**清零**。
                    #     → 刹车条件永远达不到，本轮 26 个风控信号全部照跑完，
                    #       `note_block()` 从没被调用 → 守卫里的 `block_count` 一直是 0、
                    #       `blocked_until` 一直是 0.0 → **跨进程的冷却从未落盘**，
                    #       每次运行都重新撞一遍墙。用户看到的就是「快手下载失败」。
                    #     现在：两种信号都计入 `block_streak`（不因 status_None 清零）。
                    if _is_risk_status(st):
                        block_streak += 1
                        n_risk += 1
                    else:
                        block_streak = 0
                    if block_streak >= BLOCK_STREAK_TRIP:
                        print("  [blocked] 连续 %d 个风控信号（208005/None 合计）= 接口风控，"
                              "作品本身没失效。" % block_streak)
                        print("  [blocked] 已就绪的 %d 个照常下载；余下 %d 个**未记账**，"
                              "下次接着来不会重下。" % (len(jobs), len(todo) - i))
                        # ⚠️ 旧文案写的是「隔 30~60 分钟再跑」—— 2026-09-25 实测这是错的：
                        #    撞墙后 4 小时 26 分钟仍是封的。所以冷却改由守卫统一决定
                        #    （首次 90 分钟起、指数退避），跨进程持久化，不再靠日志里一句建议。
                        if ks_guard:
                            _mins = ks_guard.note_block(
                                reason="连续 %d 个风控信号（208005/None 合计）" % block_streak)
                            blocked_hit = True
                            print("  [blocked] 守卫已记下冷却 %d 分钟：%s"
                                  % (_mins, ks_guard.cooling_message()))
                        i = len(todo)   # 跳出预取循环；剩下的不记失败台账
                        continue
                    if fail_streak >= 2:
                        cur_sleep = min(cur_sleep * 2 + 0.2, 5.0)
                        print("  [throttle] 降速至 %.2fs/个" % cur_sleep)
                    print("  [pre %d/%d] %s 不可用(status_%s), sleep=%.2f" % (i + 1, len(todo), pid, vd.get("status"), cur_sleep))
                    # ⚠️ 风控信号**不写失败台账**（`status_1040` 那类才写）：
                    #    这些作品本身没问题，只是这一轮接口被挡着。写进台账会让
                    #    「失败清单」凭空多出几十条，用户误以为片子坏了。
                    if not _is_risk_status(st):
                        failed.append({"photoId": pid, "caption": w.get("caption", ""), "reason": "status_%s" % vd.get("status")})
                    time.sleep(cur_sleep)
                    i += 1
                    continue
                block_streak = 0
                ok_streak += 1
                fail_streak = 0
                if ok_streak >= 10 and cur_sleep > args.sleep:
                    cur_sleep = max(args.sleep, cur_sleep / 1.6)
                    ok_streak = 0
                    print("  [throttle] 恢复至 %.2fs/个" % cur_sleep)
                cands = []
                for aset in ((ph.get("manifest") or {}).get("adaptationSet") or []):
                    for rep in (aset.get("representation") or []):
                        u = rep.get("url") or ""
                        if u:
                            cands.append(((rep.get("avgBitrate") or 0) + (0.5 if ".mp4" in u else 0), u,
                                          "%s %sx%s %skbps" % (rep.get("qualityType"), rep.get("width"), rep.get("height"), rep.get("avgBitrate"))))
                if not cands and ph.get("photoUrl"):
                    cands = [(0, ph["photoUrl"], "photoUrl")]
                if not cands:
                    failed.append({"photoId": pid, "caption": w.get("caption", ""), "reason": "no_source"})
                    time.sleep(cur_sleep)
                    i += 1
                    continue
                # ⚠️⚠️ **必须在「刚拿到 detail」这里再判一次重**（2026-09-25 事故）：
                #    收集阶段从封面图 DOM 抠 caption，`img[alt]` 在改版后是空的 ——
                #    实测 5619 条清单的 caption **全是空串**、duration 全是 0，
                #    于是上面那个 `_is_duplicate` 被它的安全阀（要求 caption 非空且 dur>0）
                #    **全部挡掉 → 恒不判重**。
                #    而此刻 `visionVideoDetail` 已经返回真实 caption + duration，
                #    指纹终于可靠 —— 实测 `3xg4cur9utwqnys` 与 `5209820537914262695`
                #    SHA256 完全相同（9892172 字节），缺这一步就白下第二份。
                cap_now = ph.get("caption") or ""
                dur_now = int(ph.get("duration") or 0)
                if cap_now and dur_now > 0:
                    k_now = _fp(cap_now, dur_now)
                    kinds_now = fp_kinds.get(k_now)
                    if kinds_now and any(k != _kind(pid) for k in kinds_now):
                        dup_skip += 1
                        print("  [dup %d/%d] %s 已下过 —— 同一作品的另一种 ID 写法"
                              % (i + 1, len(todo), pid))
                        time.sleep(cur_sleep)
                        i += 1
                        continue
                    # ⚠️ 立刻登记本形态：同一批次里若同时混着两种写法，第二条才能认出来
                    fp_kinds.setdefault(k_now, set()).add(_kind(pid))
                cands.sort(key=lambda x: -x[0])
                _, vurl, note = cands[0]
                title = sanitize(ph.get("caption") or w.get("caption") or pid)
                outtmpl = os.path.join(outdir, title + "_[" + pid + "].%(ext)s")
                jobs.append({"pid": pid, "url": vurl, "note": note, "outtmpl": outtmpl,
                             "caption": ph.get("caption") or w.get("caption", ""), "duration_ms": ph.get("duration")})
                if (len(jobs)) % 20 == 0:
                    print("  [pre %d/%d] 直链就绪 %d (sleep=%.2f)" % (i + 1, len(todo), len(jobs), cur_sleep))
            except Exception as e:
                failed.append({"photoId": pid, "caption": w.get("caption", ""), "reason": str(e)[:120]})
            time.sleep(cur_sleep)
            i += 1
        t_pre = time.time() - t0
        if dup_skip:
            print("[pre] 跨形态去重: 跳过 %d 个（同一作品换了个 ID 写法，台账里已有）" % dup_skip)
        print("[pre] 直链预取完成: %d 个, 耗时 %.1fs (失败 %d)" % (len(jobs), t_pre, len(failed)))
        if n_risk:
            print("[pre] ⚠️ 本轮撞到 %d 个风控信号（208005/None 合计）—— "
                  "已就绪的 %d 个不受影响，会照常下载。" % (n_risk, len(jobs)))

        # 核销当日额度（按实际打出去的请求数，不是计划数）；这轮有成功且没撞墙 → 清冷却
        if ks_guard:
            ks_guard.commit_detail(n_calls)
            # ⚠️⚠️ `n_risk == 0` 这个条件是 2026-10-03 补的（之前只看 `blocked_hit`）：
            #     `blocked_hit` 只在**连续**风控够 12 个时才置位，而实测的信号是
            #     208005 与 status_None **交替**出现（最长纯 208005 连续只有 9）。
            #     于是会出现「撞了 26 个风控信号、但一次都没触发刹车」的一轮 ——
            #     旧条件只看 `len(jobs) > 0`（预取到了几个直链）就调 `note_ok()`，
            #     **把上一轮记下的冷却清掉**。这等于每次都在给自己提前解封：
            #     冷却刚写盘 90 分钟，下一轮就因为「还抢到了几个直链」被抹平。
            #     → 判据改成「这一轮一个风控信号都没有」才算跑顺了。
            if not blocked_hit and n_risk == 0 and len(jobs) > 0:
                ks_guard.note_ok()
            elif n_risk:
                print("[pre] 本轮有风控信号，**不清除**已有冷却（避免自我解封）。")
            print(ks_guard.budget_line())   # 自带头部前缀，别再套一层

        # ---- 阶段2: 并行下载 ----
        ytdlp = [sys.executable, "-m", "yt_dlp"]
        results = []
        lock = threading.Lock()
        done_ct = {"n": 0}

        def download(job):
            ret = subprocess.run(ytdlp + ["--no-playlist", "--no-overwrites", "-o", job["outtmpl"], job["url"]],
                                 capture_output=True, text=True)
            ok = ret.returncode == 0
            with lock:
                done_ct["n"] += 1
                print("  [dl %d/%d] %s %s | %s" % (done_ct["n"], len(jobs), job["pid"], "OK" if ok else "FAIL", job["note"]))
            return {"photoId": job["pid"], "caption": job["caption"], "ok": ok, "quality": job["note"],
                    "duration_ms": job["duration_ms"],
                    "file": job["outtmpl"].replace("%(ext)s", "mp4") if ok else None,
                    "reason": None if ok else "ytdlp_exit_%d" % ret.returncode}

        t1 = time.time()
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = [ex.submit(download, j) for j in jobs]
            for fu in as_completed(futures):
                results.append(fu.result())
        t_dl = time.time() - t1
        print("[dl] 并行下载完成: %.1fs (%.1f 个/分钟, %d 线程)" % (t_dl, len(results) / max(t_dl, 0.1) * 60, args.workers))

        # 台账合并(统一写主台账)
        main_log = os.path.join(OUTDIR, "download_log.json")
        alllog = log + results + [{"photoId": f["photoId"], "caption": f.get("caption", ""), "ok": False, "reason": f.get("reason")} for f in failed]
        with open(main_log, "w", encoding="utf-8") as f:
            json.dump(alllog, f, ensure_ascii=False, indent=1)
        fl = os.path.join(OUTDIR, "failed_list.json")
        old = json.load(open(fl, encoding="utf-8")) if os.path.exists(fl) else []
        with open(fl, "w", encoding="utf-8") as f:
            json.dump(old + failed, f, ensure_ascii=False, indent=1)
        okn = sum(1 for x in results if x.get("ok"))
        print("=== SUMMARY: 下载 %d/%d, 预取失败 %d, 总耗时 %.1fs (预取 %.1fs + 下载 %.1fs) ==="
              % (okn, len(jobs), len(failed), time.time() - t0, t_pre, t_dl))

if __name__ == "__main__":
    main()
