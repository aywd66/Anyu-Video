# -*- coding: utf-8 -*-
"""
kuaishou_headless.py — 快手无 GUI 下载器（供 WebUI 桥接服务后台调用）

把「快手一键下载器.pyw」里的核心下载逻辑抽成纯命令行版，去掉了 tkinter 壳，
改为 print 输出（由桥接服务捕获写入任务日志），退出码区分结果：

  0  成功
  2  未登录（需要扫码登录后重试）
  3  解析失败 / 下载失败

用法：
  python kuaishou_headless.py <分享链接> [all|1]
  python kuaishou_headless.py --check-login   # 只查登录态，打印 LOGGED_IN / NOT_LOGGED_IN

与 GUI 版完全同一套逻辑（get_works 解析 + login_ok_in_profile 登录态 +
单作品 CDP 详情 + all 模式调 kuaishou_fast_dl.py），唯一差异是输出走 stdout。
"""
import sys, os, re, json, subprocess, time

# pythonw/管道下 stdout 编码兜底（与 pyw 同口径）
try:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

# 风控守卫（同目录）：跨进程的「冷却 + 当日预算」。见 ks_guard.py 顶部的实测时间线。
# ⚠️ 导入失败绝不能让下载停摆 —— 守卫是保护，不是依赖。
try:
    import ks_guard
except Exception:
    ks_guard = None


def _spawn_fast_dl(cmd):
    """拉起 `kuaishou_fast_dl.py` 子进程 —— **必须带上租约 token**。

    ⚠️⚠️ 这里踩过一次线上事故（2026-09-29，用户报「快手下载不了」）：
        `all`/限量主线原先写的是裸 `subprocess.run(cmd)`，没把 `KS_RUN_TOKEN`
        传给子进程。子进程于是去 `acquire_run("fast_dl")`，看到盘上那把锁 ——
        那是**它自己父进程**的租约 —— 就照冲突处理，直接刹车收工。
        症状极具欺骗性：收集阶段照跑（额度照扣），日志说「已经有一个快手批量
        任务在跑（PID xxx 的 batch）」，而那个 xxx 就是自己的爸爸；
        一个文件都没下，退出码还是 0，任务中心显示 `success`。
        `_resume_pending()` 那条路当年是传了的，所以**只有 resume 能用、全量不能用**。

    → 所有拉起 fast_dl 的地方都必须走本函数，不要再写裸 `subprocess.run`。
       `tests/test_guard_run_token.py` 的 C 段会守住这条。
    """
    env = dict(os.environ)
    try:
        tok = ks_guard.current_token() if ks_guard else None
        if tok:
            env["KS_RUN_TOKEN"] = tok
    except Exception:
        pass
    return subprocess.run(cmd, env=env)


def _find_root():
    _here = os.path.dirname(os.path.abspath(__file__))
    for _d in (_here, os.path.dirname(_here), os.path.dirname(os.path.dirname(_here)),
               os.path.dirname(os.path.dirname(os.path.dirname(_here)))):
        if os.path.isdir(os.path.join(_d, "data", "kuaishou")):
            return _d
    return os.path.dirname(_here)


# ⚠️ 产物根目录可被环境变量 KS_OUTDIR 覆盖（启动台「产物目录」改的就是它）。
#    桥接服务下发这个变量，子进程继承，因此本脚本再调起的 kuaishou_fast_dl.py
#    也会拿到同一个目录 —— 台账 download_log.json 才不会一半在旧目录一半在新目录。
# ⚠️ 台账**跟着产物目录走**是刻意的：换目录 = 换个空台账，作品会重新下一遍。
#    反过来（台账固定不动）更糟：用户为腾空间换了 E 盘，结果因为「都已下载过」
#    一个都不下，且日志只会说「无需下载」，看不出是目录换过了。
_DEFAULT_OUT = os.path.join(_find_root(), "data", "kuaishou")
OUTDIR = os.environ.get("KS_OUTDIR") or _DEFAULT_OUT
# 登录态 profile 可被 KS_PROFILE 覆盖 —— 「多个 ck」唯一的实现方式。见
# kuaishou_fast_dl 里同一处的说明：桶按 did（设备）算，只有换 profile 才有用。
PROFILE = (os.environ.get("KS_PROFILE")
           or os.path.join(_find_root(), "runtime", "kuaishou-login-profile"))
LOG_PATH = os.path.join(OUTDIR, "download_log.json")
LOGIN_KEYS = ("passToken", "kuaishou.server.web_st", "kuaishou.server.web_day7_st", "userId")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
DETAIL_Q = ("query visionVideoDetail($photoId: String, $page: String, $webPageArea: String) { "
            "visionVideoDetail(photoId: $photoId, page: $page, webPageArea: $webPageArea) { status type "
            "photo { photoUrl photoH265Url caption duration "
            "manifest { adaptationSet { representation { id qualityType width height avgBitrate url } } } } } }")


def sanitize(name):
    return re.sub(r'[\\/:*?"<>|#\s]+', "_", name).strip("_")[:60] or "kuaishou_video"


def append_log(entry):
    try:
        log = json.load(open(LOG_PATH, encoding="utf-8")) if os.path.exists(LOG_PATH) else []
    except Exception:
        log = []
    log.append(entry)
    json.dump(log, open(LOG_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def _official_count(pg):
    """读页面上**官方显示**的作品数，用来交叉验证「有没有收全」。

    ⚠️ 2026-09-25 的教训：作者「就这样飞」页面写着 8000+ 作品，收集却停在 6435 个
    列表项 / 4829 个 ID —— 只信自己抓到的数就会以为「抓全了」。
    快手 PC 主页把「粉丝 / 关注 / 作品」写在正文文本里，写法不固定
    （见过 `作品 8000`、`8000 作品`、`1.2万 作品`）。

    ⚠️ 读不到返回 **0 = 未知**，调用方**绝不能把 0 当成「0 个作品」**。
    """
    try:
        txt = pg.evaluate("() => document.body.innerText || ''") or ""
    except Exception:
        return 0
    for pat in (r"作品[^\d]{0,4}([\d,.]+)\s*万?", r"([\d,.]+)\s*万?\s*作品"):
        m = re.search(pat, txt)
        if not m:
            continue
        try:
            n = float(m.group(1).replace(",", "").replace("，", ""))
        except ValueError:
            continue
        if "万" in m.group(0):
            n *= 10000
        return int(n)
    return 0


def _deep_scroll_collect(pg, snip, absorb, max_scrolls, verbose, snap, count, max_items=0, expect=0, known_count=None):
    """全量收集作品 ID：两阶段，缺一不可。

    ⚠️⚠️ **踩过的坑：只做「滚轮滚到底」最多只能拿到 ~124/160 个。**
    原因：快手作者主页是**虚拟列表**（`ul.feed-list`）。滚轮滚到底能把
    `<li>` 全部创建出来（160 个），但每张卡片里的封面图是**懒加载**的 ——
    只有**真正进入过视口**的那一项才会填上 `<img src="...">`，
    而 `clientCacheKey` 就藏在那个 src 里。
    滚轮冲得太快，浏览器来不及加载，于是那 36 项 `<img src="">` 全空 → 一个 ID 都抠不到。

    所以必须两阶段：
      阶段 1（扩展）：滚轮滚到底，把 `<li>` 总数撑满（触发分页请求拿到全部条目）。
      阶段 2（点亮）：**逐个 `scrollIntoView` 每一张卡**，逼懒加载把图补上。
                     这一步是补齐最后那批缺失 ID 的**唯一可靠手段**。
      阶段 3（兜底）：再滚一遍，收尾。
      阶段 4（补滚，仅当 `expect` 已知且没收够）：再来一轮更长等待的滚动 + 重新点亮。

    实测：阶段 1 得 124 个 → 阶段 2+3 后得 **160/160**。

    ⚠️⚠️ `expect` = 页面官方作品数（由 `_official_count()` 读）。大号的分页有时要等
    十几秒才吐下一页，默认的「15 次无增长就收工」（≈13s）会**提前判定到底** ——
    这正是 8000+ 作品的号只扫出 6435 个的直接原因。已知 expect 时阈值放宽到 40，
    且阶段 4 会再补一轮（已点亮的项会秒过，重跑点亮很便宜）。
    """
    # ⚠️ 容器选择器要能**自动降级**：`ul.feed-list` 只在 live 站存在，
    #    www 站主页压根没有列表容器（实测 `containers: {}`）。
    #    若拿不到容器却仍用它当「稳定判据」，n 恒为 0 → 循环立刻判定「到底了」而提前退出，
    #    结果只收集到首屏那十几个（这正是 8000 作品只下 12 个的直接原因）。
    SNIP_UL = """() => {
        const ul = document.querySelector('ul.feed-list');
        if (ul && ul.children.length) return ul.children.length;
        // 降级：用「作品卡」的数量代替（www 站 / 其它布局）
        return document.querySelectorAll('img[src*="clientCacheKey="]').length
             + document.querySelectorAll('a[href*="/short-video/"]').length;
    }"""
    HAS_UL = "() => !!document.querySelector('ul.feed-list')"

    # ⚠️ 2026-09-25 实测：live 主页**文档本身不滚动**（`document.scrollHeight == innerHeight`），
    #    真正的滚动容器是内层 `div.profile-video`。`window.scrollTo` 对它完全无效，
    #    靠的是下面那条「遍历所有溢出元素设 scrollTop」的通配兜底 —— 别把它删掉，
    #    也别以为 `window.scrollTo` 在干活。这里再显式滚一次内层容器，双保险。
    def _scroll_bottom():
        try:
            pg.evaluate("() => { window.scrollTo(0, document.documentElement.scrollHeight); "
                        "const el = document.querySelector('.profile-video'); "
                        "if (el) el.scrollTop = el.scrollHeight; "
                        "document.querySelectorAll('*').forEach(e=>{ "
                        "if (e.scrollHeight > e.clientHeight + 50) e.scrollTop = e.scrollHeight; }); }")
        except Exception:
            pass

    # ⚠️ 稳定阈值：已知官方作品数时放宽到 40（≈36s 不增长才认输）。
    #    默认 15（≈13s）对几千作品的大号太急 —— 分页慢一点就被判成「到底了」。
    need_stable = 60 if expect else 15

    # ── 阶段 1：把列表撑满 ──
    last_n, last_ids, stable, i = -1, -1, 0, 0
    for i in range(max_scrolls):
        _scroll_bottom()
        pg.wait_for_timeout(900)
        absorb(pg.evaluate(snip)["pairs"])
        n = pg.evaluate(SNIP_UL)
        # ⚠️ 稳定判据同时看「容器/卡片数」和「已收集 ID 数」——
        #    任一还在涨就继续滚，避免单一信号失真导致提前退出。
        grew = (n != last_n) or (count() != last_ids)
        if not grew:
            stable += 1
            if stable >= need_stable:
                break
        else:
            stable = 0
        last_n, last_ids = n, count()
        if verbose and (i + 1) % 10 == 0:
            print("[b] 滚动 %-3d 列表项=%d 已收集=%d" % (i + 1, n, last_ids))
        # 只要够数就提前收工（大号全量收集要几十分钟，限量能省大量时间）。
        # ⚠️ 追赶式更新（known_count 提供「已收集里有多少已下载过」）：窗口满了不算完 ——
        #    作者一次发超过 N 条时，最新 N 个可能全是新的，直接停就会永久漏掉窗口外的。
        #    所以窗口满后继续往深翻，直到已收集里出现 ≥3 个已下载过的（= 越过新作品
        #    边界）才收工；列表先翻到底则由上面的稳定判据兜底。
        if max_items and count() >= max_items:
            if known_count is None:
                if verbose:
                    print("[b] 已达限量 %d 个，停止滚动" % max_items)
                break
            if known_count() >= 3:
                if verbose:
                    print("[b] 已追上历史进度（窗口 %d 个里已含 %d 个已下载作品），停止滚动"
                          % (count(), known_count()))
                break
    total = pg.evaluate(SNIP_UL)
    has_ul = pg.evaluate(HAS_UL)
    if verbose:
        print("[b] 阶段1 滚轮结束: 列表项 %d 个（容器=%s）" % (total, "ul.feed-list" if has_ul else "无/降级"))

    # ── 阶段 2：逐项 scrollIntoView，逼懒加载把封面图补上（**关键步骤**）──
    #
    # ⚠️⚠️ 2026-09-25 补的教训：`total` 可能是几千（实测作者「就这样飞」= **6435** 个列表项），
    #     每项 1 次 evaluate + 150ms 等待 → 光这一阶段就要半小时，而**中途一行日志都不打**。
    #     用户（和我）在任务中心看到的是「卡住了」—— 实际它在闷头点亮缩略图，
    #     浏览器缓存还在不停写入（可以用这点判断它是活的）。所以这里做三件事：
    #       ① **已经点亮**的项不再等待，直接跳过 —— 阶段 1 往往已经点亮了大多数，能省一半时间；
    #       ② 每 100 项打一行进度，行里带 `已收集=` 与 `阶段2 点亮 x/N`，
    #          任务中心的阶段文字和进度条才有东西可更新（后端 `task_progress` 认这两个模式）；
    #       ③ absorb 改成每 10 项一次（几千项每次都全量扫 DOM 太贵）——
    #          ⚠️ 前提是**虚拟列表不回收节点**（全程列表项数稳定，已实测），末尾仍会再收一次。
    JUMP = """(idx) => {
        const ul = document.querySelector('ul.feed-list');
        let el = null;
        if (ul && ul.children[idx]) el = ul.children[idx];
        else {
            // 降级：直接滚第 idx 张作品卡
            const cards = document.querySelectorAll('img[src*="clientCacheKey="]');
            if (cards[idx]) el = cards[idx].closest('li,div,article') || cards[idx];
        }
        if (!el) return true;        // 取不到就当已点亮，别白等
        // 回报「此项是否已经点亮」：已点亮就不用等懒加载了
        const lit = !!el.querySelector('img[src*="clientCacheKey="]');
        el.scrollIntoView({block: 'center'});
        return lit;
    }"""

    def _light_all(total, tag="阶段2"):
        """逐项点亮。⚠️ **可以重复调用**：已点亮的项不再等待，所以补滚后再跑一遍很便宜。"""
        if total <= 0:
            return 0
        wait = 300 if total <= 600 else 150
        lit_n = 0
        for idx in range(total):
            try:
                lit = pg.evaluate(JUMP, idx)
            except Exception:
                lit = False
            if lit:
                lit_n += 1
            else:
                pg.wait_for_timeout(wait)
            if idx % 10 == 0 or idx == total - 1:
                absorb(pg.evaluate(snip)["pairs"])
            if verbose and ((idx + 1) % 100 == 0 or idx == total - 1):
                print("[b] %s 点亮 %d/%d 已收集=%d（跳过已点亮的 %d）"
                      % (tag, idx + 1, total, count(), lit_n))
        return lit_n

    if total > 0:
        _light_all(total)

    # ── 阶段 3：兜底再滚一遍 ──
    for _ in range(5):
        try:
            pg.evaluate("() => { document.querySelectorAll('*').forEach(e=>{ "
                        "if (e.scrollHeight > e.clientHeight + 50) e.scrollTop = e.scrollHeight; }); }")
        except Exception:
            pass
        pg.wait_for_timeout(700)
        absorb(pg.evaluate(snip)["pairs"])

    if verbose:
        left = pg.evaluate("""() => {
            const ul = document.querySelector('ul.feed-list');
            if (!ul) return -1;
            let empty = 0;
            Array.from(ul.children).forEach(li => {
                if (!li.querySelector('img[src*="clientCacheKey="]')) empty++;
            });
            return empty;
        }""")
        print("[b] 阶段3 结束: 列表项 %d 个，仍未取到 ID 的 %s 个" % (total, left))

    # ── 阶段 4：补滚（只在「页面官方作品数已知且没收够」时才做）──
    #
    # ⚠️⚠️ 2026-09-25 实测：作者「就这样飞」页面写 8000+ 作品，但阶段 1 停在
    #     6435 个列表项（4829 个 ID）就判定「到底了」—— 分页其实还没吐完，
    #     只是每次要等十几秒。所以已知 expect 时这里再补一轮：等待拉长到 1.5s、
    #     最多再滚 120 次；真有新项出现就再跑一次 `_light_all()`（已点亮的秒过）。
    if expect and not max_items:
        got = count()
        if got < expect * 0.98:
            if verbose:
                print("[b] ⚠️ 页面显示 %d 个作品，目前只收集到 %d 个 → 补滚一轮" % (expect, got))
            stable2, last_got, last_total = 0, got, total
            # ⚠️  patience 要够：实测分页是「吐一批 → 停几十秒 → 再吐一批」
            #    （363 → 停 36s → 519 → 停…）。阈值太小会在两批之间误判「到底了」。
            #    这里给 60 轮 × 1.5s ≈ 90s 的耐心，最多再滚 200 轮。
            for j in range(200):
                _scroll_bottom()
                pg.wait_for_timeout(1500)
                absorb(pg.evaluate(snip)["pairs"])
                n2 = pg.evaluate(SNIP_UL)
                if count() == last_got and n2 == last_total:
                    stable2 += 1
                    if stable2 >= 60:
                        break
                else:
                    stable2 = 0
                last_got, last_total = count(), n2
                if verbose and (j + 1) % 10 == 0:
                    print("[b] 补滚 %-3d 列表项=%d 已收集=%d" % (j + 1, n2, last_got))
            total = last_total
            if total > 0:
                _light_all(total, tag="补滚点亮")
        if verbose:
            print("[b] 收集完成: 已收集 %d 个（页面显示 %d 个）" % (count(), expect))


def probe_works_via_browser(url, wait_ms=8000, deep=False, max_scrolls=600, verbose=True, max_items=0, known_ids=None):
    """playwright 打开页面，从 DOM 抓作品 + 作者名（快手改版后的唯一可靠路径）。

    两种抓法都要试，因为快手 PC 端把作品卡从 <a> 改成了 <div>：
      ① a[href*="/short-video/"]  —— 旧版结构（部分页面仍在用）
      ② img[src*="clientCacheKey="] —— 新版结构；clientCacheKey 的值就是 photoId
         （已用 visionVideoDetail 接口验证：反推的 ID 能命中 status=1）

    ⚠️⚠️ **deep=False 只滚 2 次，只适合「拿前几条试水」**（如单作品模式预览）。
    要拿「作者全部作品」，必须 `deep=True` —— 否则 100+ 作品的号也只返回首屏那一批，
    用户会以为「只解析到 15 个」。（曾因为 all 模式用了浅滚清单而漏掉 100+ 作品。）

    ⚠️ 页面偶发弹**滑块验证**（「请完成安全验证」）会把列表卡在首屏不动。
    此时 DOM 数量会「稳定」在一个很小的值上 —— 单看稳定性无法区分
    「真的到底了」和「被验证码拦住了」，所以要额外报出验证浮层数量。

    返回 (works, author_id, author_name)。
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print("[!] playwright 不可用: %s" % e)
        return [], None, ""

    works, seen = [], set()
    author_id, author_name = None, ""

    # 抓取快照：并集两种选择器 + 统计验证浮层
    SNIP = r"""() => {
        const ids = new Map();
        document.querySelectorAll('a[href*="/short-video/"]').forEach(a => {
            const m = (a.href || '').match(/\/short-video\/([0-9A-Za-z]+)/);
            if (m && !ids.has(m[1])) ids.set(m[1], (a.getAttribute('title') || a.textContent || '').trim().slice(0, 60));
        });
        document.querySelectorAll('img[src*="clientCacheKey="]').forEach(img => {
            const m = (img.src || '').match(/clientCacheKey=([0-9A-Za-z]+)[._\-]/);
            if (m && !ids.has(m[1])) ids.set(m[1], (img.getAttribute('alt') || '').trim().slice(0, 60));
        });
        const ov = document.querySelectorAll(
            '.verify-wrap,.captcha,.nc-container,.nc_wrapper,[class*="verify"],[class*="captcha"]').length;
        const blocked = (document.body.innerText || '').includes('请完成安全验证');
        return {pairs: Array.from(ids.entries()), ov, blocked};
    }"""

    def _absorb(pairs):
        got = 0
        for pid, cap in pairs:
            if pid not in seen:
                seen.add(pid)
                works.append({"photoId": pid, "caption": cap})
                got += 1
        return got

    # 追赶式更新用：当前已收集里有多少是「台账里已下载过」的（越过新作品边界的判据）
    def _known_count():
        if not known_ids:
            return 0
        return sum(1 for w in works if str(w.get("photoId")) in known_ids)

    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                PROFILE, channel="msedge", headless=True, user_agent=UA, locale="zh-CN",
                args=["--window-position=-32000,-32000"],
                viewport={"width": 1500, "height": 950})
            pg = ctx.new_page()
            try:
                pg.goto(url, wait_until="domcontentloaded", timeout=60000)
                pg.wait_for_timeout(wait_ms)
                snap = pg.evaluate(SNIP)
                _absorb(snap["pairs"])
                title = pg.title() or ""
                landed = pg.url or ""
                if verbose:
                    print("[b] 落地页: %s" % landed[:100])
                    print("[b] title=%r 首屏=%d 验证浮层=%d%s"
                          % (title, len(works), snap["ov"], " [被验证码拦住]" if snap["blocked"] else ""))

                if not deep:
                    for _ in range(2):
                        pg.mouse.move(750, 520)
                        pg.mouse.wheel(0, 2500)
                        pg.wait_for_timeout(1800)
                    _absorb(pg.evaluate(SNIP)["pairs"])
                else:
                    # ⚠️ 先读页面官方作品数，交给深滚当「收够了没」的判据
                    expect = _official_count(pg)
                    if verbose:
                        print("[b] 页面显示作品 %s" % (expect if expect else "未知（读不到计数）"))
                    _deep_scroll_collect(pg, SNIP, _absorb, max_scrolls, verbose, snap,
                                         count=lambda: len(works), max_items=max_items,
                                         expect=expect, known_count=_known_count)
                    if verbose:
                        if snap["blocked"]:
                            print("[b] ⚠️ 页面出现过滑块验证，数量可能被截断 —— "
                                  "请在 WebUI 里用有头模式登录并过一次验证后重试")

                # 作者名：兼容两种标题格式
                #   www 站 : 「芝士蒲公英 - 快手」
                #   live 站: 「农村的妈宝萌宝的主页-快手直播」
                for pat in (r"^(.+?)的主页\s*[-—|]\s*快手", r"^(.+?)\s*[-—|]\s*快手"):
                    m = re.match(pat, title)
                    if m:
                        author_name = m.group(1).strip()
                        break
                if not author_name and title and "快手" not in title:
                    author_name = title.strip()
                # 作者 ID：优先从「最终落地 URL」抠（短链跳转后才有真 ID）
                author_id = _extract_author_id(landed) or _extract_author_id(url)
            finally:
                try:
                    pg.close()
                except Exception:
                    pass
            ctx.close()
    except Exception as e:
        print("[!] 浏览器解析异常: %s" % e)
    return works, author_id, author_name


def _extract_author_id(url):
    """从各种快手链接形式里抠作者 ID。"""
    for pat in (r"/profile/([0-9A-Za-z_\-]+)", r"/fw/user/([0-9A-Za-z_\-]+)",
                r"/user/([0-9A-Za-z_\-]+)"):
        m = re.search(pat, url or "")
        if m:
            return m.group(1)
    m = re.search(r"profile/([0-9A-Za-z_\-]+)", url or "")
    return m.group(1) if m else None


def _is_single_work_url(url):
    """判断落地 URL 是否为「单作品页」（而非主页 /profile/）。

    分享短链跳转后的单作品落地页形态（实测）：
      · v.m.chenzhongtech.com/fw/long-video/<id>  （v.kuaishou.com 短链，手机 UA）
      · www.kuaishou.com/short-video/<id>         （PC 端）
      · m.kuaishou.com/photo/<id>                 （m 站旧形态）
    """
    if not url:
        return False
    if "/profile/" in url:
        return False
    return bool(re.search(
        r"/(?:fw/)?(?:short-video|long-video|photo|fw/photo)/[0-9A-Za-z]+", url
    ))


def get_works(short_url):
    """取作品列表 + 作者信息。

    历史方案（SSR 正则抠 `feed_photo|<id>|`）在快手 PC 改版后**已失效**：
    www.kuaishou.com/profile/<id> 会重定向到 m 站首页、或 SSR 里根本不再注入
    作品清单（实测三例真实作者全部 0 命中）。因此改为**优先走浏览器 DOM**，
    仅在浏览器不可用时才回落到旧正则（短链跳转后 m 站有时仍有 SSR）。

    返回 (works, author_id, author_name, final_url)；final_url 是短链跳转后的
    落地页，供上层做「单作品链接 vs 主页链接」的自动识别。
    """
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1", "Accept-Language": "zh-CN,zh;q=0.9"})
    caps, h, final_url = {}, "", short_url or ""
    ids = []
    try:
        r = s.get(short_url, timeout=30, allow_redirects=True)
        h, final_url = r.text, r.url
        for m in re.finditer(r'"caption":"([^"]{2,80})"', h):
            seg = h[max(0, m.start() - 1500):m.start() + 1500]
            pids = re.findall(r'feed_photo\|([0-9]{15,25})\|', seg)
            if pids and pids[0] not in caps:
                caps[pids[0]] = m.group(1)
        ids = re.findall(r'feed_photo\|([0-9]{15,25})\|[0-9]+\|', h)
    except Exception as e:
        print("[!] 短链请求失败（继续走浏览器）: %s" % e)

    # 解析作者 ID / 昵称（先尽量从 SSR 拿，浏览器路径会覆盖）
    author_id = _extract_author_id(final_url) or _extract_author_id(short_url)
    if not author_id:
        eids = re.findall(r'"userEid":"([0-9A-Za-z]+)"', h)
        if eids:
            author_id = eids[0]
    names = re.findall(r'"userName":"([^"]{1,40})"', h)
    author_name = names[0] if names else ""

    # ① 旧正则命中了就直接用（最快，无浏览器开销）
    if ids:
        works, seen = [], set()
        for pid in ids:
            if pid not in seen:
                seen.add(pid)
                works.append({"photoId": pid, "caption": caps.get(pid, "")})
        print("[i] SSR 正则命中 %d 个作品" % len(works))
        return works, author_id, author_name, final_url

    # ② 正则失效 → 走 playwright 浏览器 DOM（快手 PC 改版后的正路）
    print("[i] SSR 无作品（快手已改版），改用浏览器解析 ...")
    # ⚠️⚠️ 候选顺序必须与 `run_download` 的深滚**保持一致：live 站排第一**。
    #    这里曾经是 [www 主页, 原始链接] —— 而 www 站 profile 页没有可滚动的
    #    作品列表容器，浅滚常常「首屏=0」，于是把一个完全正常的作者判成
    #    「提取不到作品」，任务直接以退出码 3 收工。
    #    2026-09-29 用户报「快手下载不了」的第三个坑就是它：同一位作者
    #    （就这样飞 / 3xa6yyk8i6pgsz9），用主页链接（www）必现 0；改用分享短链
    #    就没事 —— 差别只在这个候选顺序（短链会跳到 m 站，m 站还有 SSR）。
    #    而深滚那边早就把 www 垫底了（见 run_download 的实测对照表），
    #    只有这里漏改，于是「能收集、却在前一步被劝退」。
    # ⚠️ 注意这里**只做浅滚**（拿个概览/昵称），全量收集交给 run_download 的 all 分支。
    #    原因：get_works 也被「单作品模式」调用，深滚几百次会让只想下一条视频的用户干等几分钟。
    cands = []
    if author_id:
        cands.append("https://live.kuaishou.com/profile/" + author_id)
    if short_url:
        cands.append(short_url)
    if author_id:
        cands.append("https://www.kuaishou.com/profile/" + author_id)
    tried = []
    for probe_url in cands:
        if not probe_url or probe_url in tried:
            continue
        tried.append(probe_url)
        bworks, baid, bname = probe_works_via_browser(probe_url, deep=False)
        if bworks:
            print("[i] 浏览器命中 %d 个作品（浅滚概览；全量走 all 模式深滚）" % len(bworks))
            return bworks, (baid or author_id), (bname or author_name), final_url
    return [], author_id, author_name, final_url


def login_ok_in_profile():
    """检查 login-profile 是否有登录 Cookie（无窗口：只读 Cookie 存储，不启动浏览器渲染）。"""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                PROFILE, channel="msedge", headless=True,
                user_agent=UA, locale="zh-CN",
                args=["--window-position=-32000,-32000"],
                viewport={"width": 1, "height": 1})
            names = {c["name"] for c in ctx.cookies()}
            ctx.close()
            return any(k in names for k in LOGIN_KEYS)
    except Exception:
        return False


def download_single(work, author_dir):
    """单作品模式：CDP 详情接口 + yt-dlp。

    ⚠️ 台账条目上必须带 `origin: "single"`（成功、失败两条都带）。
       这是「关注」列表区分两类下载的唯一依据：单作品链接下到的东西只算
       「作品档案」，不该把作者自动算成用户关注的人；主页批量（走 fast_dl
       写台账，不带 origin）才算关注。见 webui/bridge_server.py 的 ks_authors。
       批量那条链路刻意**不改** —— 不带 origin = 来源未知 = 按关注处理，
       与升级前的行为一致，老台账也不会因为这次改动把作者踢出关注列表。
    """
    try:
        subprocess.run(
            [sys.executable, "-c", "import sys; sys.path.insert(0, r'%s'); import kuaishou_fast_dl as k; k.ensure_edge()" % _TOOLS_DIR],
            capture_output=True, text=True)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
            ctx = browser.contexts[0]
            # ⚠️ 同一个 sync_playwright() 内不能再嵌一个：会报
            #    "It looks like you are using Playwright Sync API inside the asyncio loop."
            #    原代码嵌了两个 with（p / p2），是历史遗留 bug（第一次录到 sync 上下文里
            #    又开了一个），现在统一用同一个 p 起第二个 context。
            c0 = p.chromium.launch_persistent_context(PROFILE, channel="msedge", headless=True, user_agent=UA, locale="zh-CN", viewport={"width": 1280, "height": 800})
            cks = c0.cookies(); c0.close()
            inj = []
            for c in cks:
                if "kuaishou" not in (c.get("domain") or ""):
                    continue
                cc = {"name": c["name"], "value": c["value"], "domain": c["domain"], "path": c.get("path", "/")}
                if c.get("expires", -1) > 0:
                    cc["expires"] = c["expires"]
                if c.get("httpOnly"):
                    cc["httpOnly"] = True
                if c.get("secure"):
                    cc["secure"] = True
                inj.append(cc)
            ctx.add_cookies(inj)
            pid = work["photoId"]
            pg = ctx.new_page()
            pg.goto("https://www.kuaishou.com/short-video/" + pid, wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(4000)
            r = pg.evaluate(
                """async (args) => {
                    const resp = await fetch('/graphql', {method: 'POST',
                        headers: {'Content-Type': 'application/json'}, credentials: 'include',
                        body: JSON.stringify({operationName: 'visionVideoDetail',
                            variables: {photoId: args.pid, page: 'main', webPageArea: ''},
                            query: args.q})});
                    return await resp.text();
                }""", {"pid": pid, "q": DETAIL_Q})
            d = json.loads(r)
            if ks_guard:
                # 单条也要记账，否则当日总额度会被低估（批量按 400 算，单条却不算）
                ks_guard.commit_detail(1)
            vd = ((d.get("data") or {}).get("visionVideoDetail") or {})
            ph = vd.get("photo") or {}
            if vd.get("status") != 1 or not ph:
                print("[!] 作品不可用 status=%s" % vd.get("status"))
                pg.close()
                return False
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
                pg.close()
                return False
            cands.sort(key=lambda x: -x[0])
            _, vurl, note = cands[0]
            title = sanitize(ph.get("caption") or pid)
            dest = author_dir
            os.makedirs(dest, exist_ok=True)
            outtmpl = os.path.join(dest, title + "_[" + pid + "].%(ext)s")
            print("[5] %s | %s" % (pid, note))
            ytdlp = [sys.executable, "-m", "yt_dlp"]
            final_path = os.path.join(dest, title + "_[" + pid + "].mp4")
            ret = subprocess.run(ytdlp + ["--no-playlist", "--no-overwrites", "-o", outtmpl, vurl])

            # ── 瞬时文件锁恢复 ────────────────────────────────────────────
            # 2026-10-18 实测：HLS 分片 100% 下完、FixupM3u8 重封装也写完了，
            # 最后 `.temp.mp4 → .mp4` 改名撞上新文件被杀软实时扫描的瞬时锁
            # （WinError 5 拒绝访问），yt-dlp 退出码 1 —— 但成品完好在盘上。
            # ① 成品已在盘且非空 → 直接按成功处理；
            # ② 成品不在 → 等几秒重跑一次（--no-overwrites 在，成品若已落盘
            #    会直接跳过返回 0；没落盘则重新下一遍，损失可忽略）。
            if ret.returncode != 0 and os.path.exists(final_path) \
                    and os.path.getsize(final_path) > 100 * 1024:
                print("[recover] 下载 100% 完成且成品已在盘上（%d 字节），"
                      "改名时撞上瞬时文件锁 —— 按成功处理" % os.path.getsize(final_path))
                ret = subprocess.CompletedProcess(ret.args, 0)
            elif ret.returncode != 0:
                print("[recover] 首次退出码 %s，等待 5s 后重试一次（瞬时文件锁通常很快解除）"
                      % ret.returncode)
                time.sleep(5)
                ret = subprocess.run(ytdlp + ["--no-playlist", "--no-overwrites",
                                              "-o", outtmpl, vurl])

            if ret.returncode == 0:
                # 成品就位后清理残留的 .temp.mp4（可能还锁着，尽力而为）
                tmp_left = os.path.join(dest, title + "_[" + pid + "].temp.mp4")
                if os.path.exists(tmp_left):
                    try:
                        os.remove(tmp_left)
                    except OSError:
                        print("[recover] 残留的 .temp.mp4 仍被占用，不影响成品，留待下次清理")
                pg.close()
                append_log({"photoId": pid, "caption": ph.get("caption") or "", "ok": True,
                            "quality": note, "duration_ms": ph.get("duration"),
                            "file": final_path, "origin": "single"})
                print("[完成] 下载成功")
                try:
                    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                    os.path.join(_TOOLS_DIR, "kill_debug_edge.ps1")], capture_output=True, timeout=60)
                except Exception:
                    pass
                return True
            pg.close()
            append_log({"photoId": pid, "caption": ph.get("caption") or "", "ok": False,
                        "quality": note, "duration_ms": ph.get("duration"),
                        "file": None, "origin": "single"})
            print("[完成] 下载失败(码 %d)" % ret.returncode)
            try:
                subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                os.path.join(_TOOLS_DIR, "kill_debug_edge.ps1")], capture_output=True, timeout=60)
            except Exception:
                pass
            return False
    except Exception as e:
        print("[异常] %s" % e)
        return False


def _resume_pending(batch=60, cooldown_min=12, max_wait_rounds=8):
    """断点续跑 `works_list_pending.json`：分批下载；撞到风控就收工，隔天再来。

    ⚠️ `cooldown_min` / `max_wait_rounds` 两个参数**已经不起作用**，保留只是为了
    不破坏外部调用签名。冷却现在由 `ks_guard` 统一决定（首次 90 分钟起、指数退避、
    跨进程持久化），理由见下。

    ## 旧结论是错的（2026-09-25 更正）

    这里原本写着「`visionVideoDetail` 是有配额的接口，一口气请求到第 2 个起就返回
    `status_208005`（实测 20 个里只成功 1 个）」。**那个 20 个里成功 1 个，是在
    会话已经被封的状态下测出来的** —— 把「已封」误当成了「接口的固有配额」。

    同一天的对照数据直接推翻它：连调 55 个详情接口、0 失败；更晚还连调过 12 个、0 失败。
    真正会触发风控的是**规模**：先深滚收集 1790 个作品，之后第 12 个详情就开始全灭。

    ## 所以现在的姿势

    - 能不能跑、跑多少，由 `ks_guard` 统一裁决（当日预算 + 跨进程冷却）。
    - 本轮 0 成功 → 记一次冷却并**立即收工**，不再原地睡 12 分钟 × 8 轮。
      那 96 分钟实测无效（4 小时 26 分钟后仍是封的），而且每轮都要再打一轮请求，
      可能反过来把惩罚窗口往后推。
    - 状态全在台账 `download_log.json` 里，随时取消都不丢进度；
      被守卫拦住时**不算失败**（退出码 0），下次到期后接着跑就行。
    """
    list_path = os.path.join(OUTDIR, "works_list_pending.json")
    log_path = os.path.join(OUTDIR, "download_log.json")
    batch_path = os.path.join(OUTDIR, "works_list_resume.json")

    if not os.path.exists(list_path):
        print("[resume] 没有清单 %s —— 请先用 `collect` 模式扫一遍" % list_path)
        return 3
    try:
        pending = json.load(open(list_path, encoding="utf-8")) or []
    except Exception as e:
        print("[resume] 清单读不了: %s" % e)
        return 3
    if not pending:
        print("[resume] 清单是空的")
        return 3

    def _read_log():
        if not os.path.exists(log_path):
            return [], set(), set()
        try:
            rows = json.load(open(log_path, encoding="utf-8")) or []
        except Exception:
            rows = []
        ok, dead = set(), set()
        for r in rows:
            pid = str(r.get("photoId") or "")
            if not pid:
                continue
            if r.get("ok"):
                ok.add(pid)
            elif "status_1040" in str(r.get("reason", "")):
                # 只有 1040（内容不存在/已删）算彻底没救；**208005 是风控，绝不记死**
                dead.add(pid)
        return rows, ok, dead

    _rows, done, dead = _read_log()
    author_dir = None
    for r in _rows:
        d = os.path.dirname(r.get("file") or "")
        if d and os.path.isdir(d):
            author_dir = d
            break
    if author_dir is None:
        subs = [os.path.join(OUTDIR, n) for n in os.listdir(OUTDIR)] if os.path.isdir(OUTDIR) else []
        subs = [p for p in subs if os.path.isdir(p)]
        if len(subs) == 1:
            author_dir = subs[0]
    if not author_dir:
        print("[resume] 认不出作者目录 —— 请先用普通模式下一次（至少 1 个文件）再续跑")
        return 3
    total = len(pending)
    print("[resume] 清单 %d 个 | 已成功 %d | 彻底失效 %d | 作者目录 %s"
          % (total, len(done), len(dead), author_dir))

    rnd = 0
    while True:
        _rows, done, dead = _read_log()
        todo = [w for w in pending
                if str(w.get("id")) not in done and str(w.get("id")) not in dead]
        if not todo:
            print("[resume] 全部处理完毕 —— 成功 %d / 清单 %d（失效 %d）"
                  % (len(done), total, len(dead)))
            print("[完成] 退出码 0")
            return 0
        cur = todo[:batch]
        rnd += 1
        json.dump(cur, open(batch_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        before = len(done)
        print("[resume] 第 %d 轮：取 %d 个（后面还有 %d 个）" % (rnd, len(cur), len(todo) - len(cur)))

        cmd = [sys.executable, "-u", os.path.join(_TOOLS_DIR, "kuaishou_fast_dl.py"), author_dir,
               "--workers", "4", "--list", batch_path]
        # ⚠️ 走统一助手：它负责把租约 token 塞进子进程 env（见 `_spawn_fast_dl` 的说明）。
        #    子进程看到 KS_RUN_TOKEN 就知道「租约在上层手里」，直接用，
        #    不会跟自己的父进程抢锁（否则 resume 一启动就把自己拦死）。
        t0 = time.time()
        _spawn_fast_dl(cmd)
        _rows, done, dead = _read_log()

        got = len(done) - before

        # ① 先问预算：子进程可能不是因为风控停的，而是当日额度用完了。
        #    这两种「0 成功」含义完全不同，绝不能混为一谈 —— 混了就会把
        #    「今天额度用完」错记成「撞墙」，于是一路退避到 24 小时。
        if ks_guard and ks_guard.allow_detail(1) <= 0:
            print(ks_guard.budget_exhausted_message())
            print("[resume] 本次到此为止（不是失败）。明天再跑同一个模式即可。")
            return 0

        # ② 子进程自己会检测风控（连续 12 个风控信号 = 208005 与 status_None 合计）
        #    并写进守卫 —— 直接问守卫，不用猜。
        #    ⚠️ 2026-10-03：子进程原来只数**纯 208005**，而实测两种信号是交替出现的
        #    （`208005×9 | None×2 | 208005×1 | None×6 | 208005×8`），最长纯连续只有 9，
        #    永远到不了 12 → 守卫里的冷却从未落盘 → 每次运行重新撞墙。子进程已修。
        if ks_guard and ks_guard.blocked_info()[0]:
            print("[resume] 撞上接口风控：" + ks_guard.cooling_message())
            print("[resume] 直接收工 —— 旧行为是原地等 12 分钟 × 8 轮（共 96 分钟），"
                  "实测 4 小时 26 分钟后仍是封的，那一段纯属白等。")
            return 0

        if got == 0:
            # ③ 没有风控信号、预算也还有，却一个都没成 → 记一次冷却并收工，别硬撑。
            if ks_guard:
                mins = ks_guard.note_block(reason="resume 本轮 0 成功（无数可下）")
                print("[resume] 本轮 0 成功，守卫记下冷却 %d 分钟：%s" % (mins, ks_guard.cooling_message()))
            else:
                print("[resume] 本轮 0 成功，先收工。进度不会丢，隔更久再跑同一个模式。")
            return 0

        print("[resume] 第 %d 轮成果 +%d 个，用时 %.1f 分钟 | 累计 %d/%d"
              % (rnd, got, (time.time() - t0) / 60.0, len(done), total))


def run_download(url, mode):
    """核心下载流程（与 GUI 版 run_download 一致），返回退出码。

    mode 支持：
      "all"     —— 收集并下载该作者全部作品（大号会很久，但受当日额度限制）
      "collect" —— **只把作品清单扫出来写盘，一个都不下**（大号专用：先看清有多少）
      "1"       —— 只下最新 1 个
      "<数字>"   —— 只下载最新的 N 个（大号强烈建议用它，见下）
      "resume"  —— **断点续跑现有清单**（见 `_resume_pending`）：不清不重地分批下，
                   撞到风控就收工，由守卫决定几时能再来。
    ⚠️ 几千作品的号：全量 = 收集半小时 + 下载几小时 + 几十 GB。
       用数字限量可以在收集阶段就提前收工，省掉绝大部分等待。
       ⚠️ 但限量**不会**告诉你这个号到底有多少作品 —— 想先摸清家底就用 `collect`。

    ⚠️⚠️ **别一口气全下。** 2026-09-25 的对照数据：连调 55 个详情接口 0 失败，
    但先深滚收集 1790 个作品之后，第 12 个详情就开始全灭（status_208005），
    而且**封了 4 个多小时还没解**。所以真正要防的不是「接口配额」而是「规模」——
    当日额度由 `ks_guard` 统一管，意图就是不让程序有机会一次要几千个。
    """
    # ── 风控刹车：批量操作在冷却期一律拦下；**单条不拦** ──────────────────
    # ⚠️ 为什么单条不拦（2026-09-25 实测，很硬）：在「已被封」的状态下，
    #    单条模式用**完全同一个** `fetch('/graphql')`（同样 page:'main'、同样 cookie）
    #    **成功了** —— 720p 直链拿到、文件落盘。说明被封的不是账号也不是设备，
    #    而是「一张老页面连打几百次」这个**形态**。
    #    → 所以冷却只拦批量（那才是会又攒一遍 208005 的），单条照放，只提醒一句。
    # ── 身份（风控桶）：必须在查冷却之前确定 ────────────────────────────────
    # 桶 = 设备（did），冷却与当日额度都按桶记。
    # 正常路径只查一次 `data\kuaishou\idents.json`（不启动浏览器）；只有
    # 「这份登录态还没登记过」时才读一次 cookie 确认 —— 那是一次性成本。
    # （fast_dl 那边反着来：它本来就要开浏览器，所以登记放在读 cookie 之后，
    #   省掉一次多余的启动。两条路各自最优，但用的是同一套桶逻辑。）
    if ks_guard:
        _bk = ks_guard.set_identity_for_profile(PROFILE)
        if _bk == ks_guard.BUCKET_DEFAULT:
            print("[guard] 这份登录态还没登记身份，读一次 cookie 确认（约几秒，只此一次）...")
            _bk2 = ks_guard.remember_identity(PROFILE, ks_guard.read_profile_cookies(PROFILE))
            if _bk2:
                ks_guard.set_identity(_bk2)
                print("[guard] 已登记：风控桶 %s" % _bk2)
            else:
                print("[guard] 读不到身份字段（这份 profile 没登录？）"
                      "—— 本次按 default 桶算，额度可能与其它未登记的登录态合并。")

    _cool, _cool_until = False, ""
    if ks_guard:
        print(ks_guard.budget_line())
        _cool, _left, _cool_until = ks_guard.blocked_info()

    if str(mode).strip().lower() == "resume":
        # ⚠️ resume 也是批量 —— 同样要先领独占租约，否则「两个窗口同时 resume」
        #    会让两个进程各领一份当日额度（400 → 800），守卫就被自己的第二个
        #    窗口绕过去了。见 ks_guard.acquire_run 的说明。
        if ks_guard:
            _ok, _holder = ks_guard.acquire_run("resume")
            if not _ok:
                print(ks_guard.conflict_message(_holder))
                print("[完成] 退出码 0（守卫主动刹车，不是错误）")
                return 0
        return _resume_pending()

    print("[1] 解析链接 ...")
    # 限量模式：mode 是纯数字且 >1 → 只抓最新的 N 个
    # ⚠️⚠️ **`"0"` 必须表示「不限」，不能被当成 1**（2026-10-03 修）：
    #   旧写法 `max(int(mode), 1)` 会把 `0` 抬成 `1` —— 而本函数内部一直用
    #   `max_items = 0` 表示「不限」（`if max_items and ...` 到处都靠这个 falsy 语义）。
    #   后果：前端「一键更新」若把上限设成 0（= 一个都不漏），请求 `mode="0"` 到了这里
    #   变成 `max_items=1` → **只下最新 1 个**，而且日志里显示「限量 1 个」，
    #   看着像正常限量、实际与用户要的「不限」完全相反 = 静默少下。
    #   「不限」本来就该走深滚全量，所以这里如实传 0。
    max_items = 0
    try:
        if str(mode).strip().isdigit():
            max_items = int(str(mode).strip())
    except Exception:
        max_items = 0

    # ── 解析链接（提前到批量守卫之前）────────────────────────────────────
    # 必须先知道「这是单作品链接还是主页链接」，才能决定要不要去领批量的
    # 风控额度/独占租约 —— 单作品不占批量额度，也不该被批量冷却拦住。
    works, author_id, author_name, landed_url = get_works(url)
    if not works:
        print("[!] 未能从链接提取作品（单作品链接或已删除）")
        return 3

    # ── 自动识别：单作品链接 vs 主页链接 ─────────────────────────────────
    # 用户粘贴的分享短链是「单作品页」时（落地 URL 形如 /short-video/<id>、
    # /fw/long-video/<id>），不管传进来的是什么批量模式，都按单作品直接下：
    # 拿单作品链接去深滚「主页」收集不到东西（feed-list 为空），任务只会
    # 干等几分钟然后 0 收获 —— 2026-10-18 实测：v.kuaishou.com 单作品短链被
    # mode=all 路由去深滚直播站主页，滚动 10 次收集 0 条。
    if (
        str(mode).strip().lower() not in ("resume", "collect")
        and mode != "1"
        and _is_single_work_url(landed_url or url)
    ):
        print("[auto] 识别为单作品链接（落地页 %s），按单作品直接下载"
              % (landed_url or url)[:90])
        mode = "1"
        max_items = 1

    # ── 风控刹车（第二道·批量）：冷却期 + 当日额度 ────────────────────────
    # ⚠️ 只在「会真的去批量收集」的模式上硬拦：`1`（单条）不碰这里 ——
    #    单条在冷却期仍然可用（实测），拦住它是添堵而不是保护。
    # 依据：连调 55 个详情 0 失败，但先深滚收集 1790 个之后第 12 个详情就开始全灭。
    # 所以「要多大」本身就是触发条件，这里把 `all` 的「全量」语义改成「今天这份量」。
    if max_items != 1:
        # 独占租约：同一个账号同一时刻只跑一个批量（多开窗口不会更快，只会
        # 把当日额度各领一份、并且把「温和的串行」变成「激进的并行」）。
        if ks_guard:
            _ok, _holder = ks_guard.acquire_run("batch")
            if not _ok:
                print(ks_guard.conflict_message(_holder))
                print("[完成] 退出码 0（守卫主动刹车，不是错误）")
                return 0
        if ks_guard and _cool:
            print(ks_guard.cooling_message())
            print("[完成] 退出码 0（守卫主动刹车，不是错误）"
                  "—— 批量模式在冷却期会被拦，单条模式仍可用。")
            return 0
        if ks_guard:
            _left = ks_guard.collect_left()
            if _left <= 0:
                print(ks_guard.budget_exhausted_message())
                print("[完成] 退出码 0（守卫主动刹车，不是错误）")
                return 0
            if max_items <= 0:
                print("[guard] 本次按当日额度限量：最多 %d 个"
                      "（`all` 原本是「全量」—— 故意不让一次要几千个）" % _left)
                max_items = _left
            elif max_items > _left:
                print("[guard] 本次按当日额度收窄：%d → %d 个" % (max_items, _left))
                max_items = _left
    elif ks_guard and _cool:
        print("[guard] 当前在风控冷却期（%s 前不建议批量）。"
              "单条不受影响，本次照常；批量请等冷却结束。" % _cool_until)

    author_disp = author_name or (author_id or "未知作者")
    print("[2] 作者: %s (ID: %s) | 作品 %d 个" % (author_disp, author_id or "?", len(works)))
    author_dir = os.path.join(OUTDIR, sanitize(author_disp))
    os.makedirs(author_dir, exist_ok=True)
    print("[2b] 保存到: " + author_dir)
    # ⚠️ `collect` 与 `all` 走同一条深滚路径（否则清单不齐），只是最后不下。
    collect_only = (str(mode).strip().lower() == "collect")
    # `0` 与 `all` 同义（不限 → 走深滚拿全量），别让它掉进下面的「单条」分支。
    unlimited = (str(mode).strip() == "0") or (mode == "all") or collect_only
    targets = works if (unlimited or max_items) else works[:1]

    print("[3] 检查登录态 ...")
    if not login_ok_in_profile():
        print("[NEED_LOGIN] 未登录快手，请在 WebUI 中扫码登录后重试")
        return 2

    # ⚠️ 只有 mode == "1"（或单作品语义）才走单条下载；
    #    mode 是 >1 的数字时是「限量批量」，必须走下面的批量流程。
    #    ⚠️⚠️ `collect` **必须排除在这里之外**：它要走下面的深滚把清单扫全，
    #        落到单条下载就只剩 1 个作品（2026-09-25 真的踩了：说好只扫清单，结果下了一个视频）。
    if mode == "1" or (mode.lower() not in ("all", "collect", "0") and not max_items):
        # ⚠️ `"0"`（不限）已在上方排除：它是「全量」语义、与 `all` 同族，不是单条。
        ok = download_single(targets[0], author_dir)
        return 0 if ok else 3

    # ── all 模式：要拿「全部作品」────────────────────────────────────────────
    #
    # 历史坑（两轮，务必看懂再动）：
    #  ① 一开始写死用 `www.kuaishou.com/profile/<author_id>`。但短链跳转抠到的
    #     author_id 常是「用户号」（形如 v177549463），而 www 的 profile 页要的是
    #     「用户 ID」（形如 3xp3pjmyvy4u2j2）→ www 页返回 0 作品。
    #  ② 于是加了回落：www 探活失败就用「分享页内置清单」。**但那份清单来自
    #     get_works() 的浅滚（只滚 2 次）**，100+ 作品的号也只拿到 ~15 个 ——
    #     用户看到「100 多个作品怎么只解析到 15 个」。
    #  ③ 后来改成深滚，但**候选页把 www 排第一**，而 www 站主页没有列表容器、
    #     稳定判据立刻满足 → 只滚 15 次就退出 → 8000 作品的号只下了 12 个。
    #
    # ✅ 正解：**`live.kuaishou.com/profile/<id>` 的 `ul.feed-list` 才是唯一能
    #    全量懒加载的入口**（实测滚 60 次 → 781 个，且线性增长）。www 站垫底。
    list_path = os.path.join(OUTDIR, "works_list_pending.json")

    def _write_pending_list(ws):
        # ⚠️⚠️ **必须和已有清单合并，不能覆盖。**
        #    大号（8186 作品）一次根本扫不完：实测一轮只能拿到几百到几千个，
        #    站点还会限流。覆盖写会让我们「每跑一次就把上一次的成果清掉」，
        #    跑得越多剩得越少（2026-09-25 真事：1790 条的清单被一次只剩 17 条的
        #    兜底结果覆盖掉）。所以这里按 photoId 求并集，并回报新增了多少。
        old = []
        if os.path.exists(list_path):
            try:
                old = json.load(open(list_path, encoding="utf-8")) or []
            except Exception:
                old = []
        seen_ids = {str(o.get("id")) for o in old if isinstance(o, dict) and o.get("id")}
        added = 0
        for w in ws:
            pid = str(w.get("photoId") or "")
            if pid and pid not in seen_ids:
                seen_ids.add(pid)
                old.append({"id": pid, "caption": w.get("caption", ""), "duration": 0})
                added += 1
        json.dump(old, open(list_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        if added or old:
            print("[4] 清单合并：原有 %d 个 + 本次新增 %d 个 = 共 %d 个 → %s"
                  % (len(old) - added, added, len(old), list_path))

    # 候选页按「最可能成功」排序。
    #
    # ⚠️⚠️ **顺序是实测出来的，别凭直觉改。** 同一个作者实测对比（作者「就这样飞」）：
    #   | 入口 | 列表容器 | 滚 40 次收集到 |
    #   |---|---|---|
    #   | `live.kuaishou.com/profile/<id>` | 有 `ul.feed-list` | **366**（还在涨） |
    #   | 分享短链（自身跳到 live 站）     | 同上            | **321** |
    #   | `www.kuaishou.com/profile/<id>`  | **无容器**      | **12** ← 卡死 |
    # 曾把 www 排在第一位 → 8000 作品的号只下了 12 个。
    # **www 站主页根本没有可滚动的作品列表容器，只能拿到首屏那几个**，必须垫底。
    candidates = []
    if author_id:
        candidates.append(("直播站主页", "https://live.kuaishou.com/profile/" + author_id))
    if url and url not in [c[1] for c in candidates]:
        candidates.append(("原始链接", url))
    if author_id:
        candidates.append(("www主页", "https://www.kuaishou.com/profile/" + author_id))

    # 大号全量要滚很久：默认给足滚动预算。限量模式下会在收集阶段提前收工。
    scroll_budget = 2000 if not max_items else max(60, max_items // 10 + 60)

    # ── 追赶式更新（2026-10-24，应用户要求）─────────────────────────────────
    # 「取最新 N 个」升级为：窗口里**全是新作品**时继续往深翻，直到出现已下载的。
    # 做法：把台账里 ok 的作品 ID 交给收集器当「已下载」判据（见 _deep_scroll_collect
    # 的停止条件）。仅在限量模式（max_items>0）且台账非空时启用；全新作者没有
    # 「已下载」可言，仍按纯限量走（否则会变成全量深滚）。
    _known_ids = set()
    if max_items:
        try:
            _rows = json.load(open(LOG_PATH, encoding="utf-8")) if os.path.exists(LOG_PATH) else []
            _known_ids = {str(r.get("photoId")) for r in _rows if r.get("ok")}
        except Exception:
            _known_ids = set()
        if _known_ids:
            print("[4] 追赶式更新：台账已下载 %d 个作品，将翻到越过新作品边界为止" % len(_known_ids))

    full_works, used = [], ""
    for label, cand in candidates:
        print("[4] 深滚收集作品（%s）: %s%s" % (
            label, cand[:90], (" [限量 %d 个%s]" % (max_items, "，追不上不停" if _known_ids else "")) if max_items else ""))
        # ⚠️ deep=True 时首屏多等一会儿（实测 8s 偶发「页面还没渲染出作品列表」→
        #    首屏=0、列表项=0，稳定判据随后把它当成「到底了」，整个号一个都收不到）。
        ws, cid, cname = probe_works_via_browser(cand, deep=True, wait_ms=12000,
                                                 max_scrolls=scroll_budget,
                                                 max_items=max_items,
                                                 known_ids=(_known_ids or None))
        if ws:
            full_works, used = ws, label
            print("[4] %s 收集到 %d 个作品" % (label, len(ws)))
            if cname and cname != author_disp:
                print("[4] 昵称以页面为准: %r -> %r" % (author_disp, cname))
            break
        print("[4] %s 未取到作品，换下一个入口 ..." % label)

    # 深滚全失败 → 才退回浅滚清单（至少比什么都拿不到强）
    if not full_works:
        if targets:
            print("[4] ⚠️ 所有入口深滚均失败，回落到已解析的 %d 个作品 ..." % len(targets))
            full_works = targets
            _write_pending_list(full_works)
            cmd = [sys.executable, "-u", os.path.join(_TOOLS_DIR, "kuaishou_fast_dl.py"), author_dir,
                   "--workers", "4", "--list", list_path]
        else:
            print("[!] 未能收集到任何作品")
            return 3
    else:
        # ⚠️ 追赶式更新时**不能截断**：收集器已在「越过新作品边界」处停住，
        #    full_works 里窗口外的那些正是这次要补的新作品，截回 max_items 就白追了。
        #    （台账为空的全新作者不启用追赶，len 不会超，走原限量截断。）
        if max_items and len(full_works) > max_items and not _known_ids:
            full_works = full_works[:max_items]
            print("[4] 应用限量: 取最新 %d 个" % max_items)
        _write_pending_list(full_works)
        # ⚠️⚠️ **限量模式必须给 fast_dl 喂「只含本次这些条目」的清单**。
        #    `_write_pending_list` 是**按 photoId 合并**（刻意设计，避免大号越跑越少），
        #    于是 `works_list_pending.json` 里躺着 5619 条 —— 若直接把它传给 fast_dl，
        #    「只下最新 20 个」会变成**把整个大清单全下**（几十 GB），限量形同虚设。
        #    所以另写一份批次清单，主清单保持不动、继续当「家底台账」用。
        if max_items:
            batch_path = os.path.join(OUTDIR, "works_list_batch.json")
            json.dump([{"id": str(w.get("photoId") or ""), "caption": w.get("caption", ""), "duration": 0}
                       for w in full_works if w.get("photoId")],
                      open(batch_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            list_path = batch_path
            print("[4] 限量批次清单 %d 个 → %s" % (len(full_works), batch_path))
        cmd = [sys.executable, "-u", os.path.join(_TOOLS_DIR, "kuaishou_fast_dl.py"), author_dir,
               "--workers", "4", "--list", list_path]

    # 收集阶段也算额度 —— 深滚本身就是在打接口（1790 项那次就是这么把会话打到被降权的）
    if ks_guard:
        ks_guard.commit_collect(len(full_works))
        print(ks_guard.budget_line())   # budget_line 自带头部前缀，别再套一层

    # ⚠️ `collect` = 只把清单扫出来就收工，一个都不下。
    #    大号（几千作品）先跑这个摸清家底，再决定下多少 —— 比 `all` 盲下几十 GB 稳。
    if collect_only:
        print("[完成] 仅收集：清单共 %d 个 → %s" % (len(full_works), list_path))
        print("[完成] 退出码 0")
        return 0

    # 直接继承 stdout/stderr，让 fast_dl 的日志实时进桥接任务日志
    # ⚠️ 必须走 `_spawn_fast_dl`（带上租约 token），不能裸调 subprocess.run ——
    #    裸调会让子进程把自己的父进程当成「另一个批量任务」而刹车，
    #    结果是额度照扣、一个都不下、退出码还是 0。见该函数的说明。
    ret = _spawn_fast_dl(cmd)
    print("[完成] 退出码 %d" % ret.returncode)
    return 0 if ret.returncode == 0 else 3


def main():
    if "--check-login" in sys.argv:
        ok = login_ok_in_profile()
        print("LOGGED_IN" if ok else "NOT_LOGGED_IN")
        sys.exit(0 if ok else 1)

    url = (sys.argv[1].strip() if len(sys.argv) > 1 else "")
    mode = sys.argv[2].strip() if len(sys.argv) > 2 else "all"
    if not url:
        print("[错误] 未提供链接")
        sys.exit(3)
    # ⚠️ 别把数字 mode 吃掉 —— mode 为纯数字表示「只下最新 N 个」（大号很有用）。
    #    仅当既不是 all/1/collect、也不是数字时才回落 all。
    # ⚠️⚠️ `resume`（断点续跑）**必须**在这里就放行 —— 这是第三处 mode 白名单。
    #    2026-09-25 漏了它，mode 被回落成 `all`，于是「断点续跑」变成
    #    「重新深滚收集 + 全下」，白跑十几分钟还把浏览器额度糟蹋了。
    #    三处白名单必须同步：本函数、`run_download` 的分支、桥接服务的入参校验。
    if mode.lower() not in ("all", "1", "collect", "resume") and not mode.isdigit():
        mode = "all"
    if mode.isdigit() and int(mode) < 1:
        mode = "all"
    sys.exit(run_download(url, mode))


if __name__ == "__main__":
    main()
