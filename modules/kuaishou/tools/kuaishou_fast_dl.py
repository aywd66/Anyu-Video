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

OUTDIR = os.path.join(_find_root(), "data", "kuaishou")
PROFILE = os.path.join(_find_root(), "runtime", "kuaishou-login-profile")
UID_3X = "3xxjtkcmqqt2fqg"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
CDP = "http://127.0.0.1:9222"
EDGE_PROFILE = os.path.join(OUTDIR, "edge-manual-profile")

DETAIL_Q = ("query visionVideoDetail($photoId: String, $page: String, $webPageArea: String) { "
            "visionVideoDetail(photoId: $photoId, page: $page, webPageArea: $webPageArea) { status type "
            "photo { photoUrl photoH265Url caption duration "
            "manifest { adaptationSet { representation { id qualityType width height avgBitrate url } } } } } }")

def sanitize(name):
    return re.sub(r'[\\/:*?"<>|#\s]+', "_", name).strip("_")[:60] or "kuaishou_video"

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

def dom_collect(pg):
    works = {}
    def snap():
        pairs = pg.evaluate("""
            () => {
                const ids = new Map();
                document.querySelectorAll('a[href*="/short-video/"]').forEach(a => {
                    const m = a.href.match(/\\/short-video\\/([0-9A-Za-z]+)/);
                    if (m) { const t=(a.getAttribute('title')||a.textContent||'').trim(); ids.set(m[1], t.slice(0,60)); }
                });
                document.querySelectorAll('img[src*="clientCacheKey="]').forEach(img => {
                    const m=(img.src||'').match(/clientCacheKey=([0-9A-Za-z]+)\\./);
                    if (m && !ids.has(m[1])) ids.set(m[1], '');
                });
                return Array.from(ids.entries());
            }
        """)
        for pid, cap in pairs:
            if pid not in works:
                works[pid] = {"id": pid, "caption": cap}
    pg.bring_to_front()
    pg.reload(wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(9000)
    snap()
    print("[collect] 首屏:", len(works))
    last, stable = len(works), 0
    for i in range(600):
        try:
            pg.mouse.move(700, 520)
            pg.mouse.wheel(0, 2000)
        except Exception:
            pass
        pg.wait_for_timeout(1100)
        before = len(works)
        snap()
        if len(works) == before == last:
            stable += 1
            if stable >= 25:
                break
        else:
            stable = 0
        last = len(works)
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
    atexit.register(cleanup_debug_edge)  # 所有退出路径(提前 return/报错/正常结束)都保证清理

    from playwright.sync_api import sync_playwright
    cookies = []
    with sync_playwright() as p:
        c0 = p.chromium.launch_persistent_context(PROFILE, channel="msedge", headless=True,
                                                  user_agent=UA, locale="zh-CN", viewport={"width": 1280, "height": 900})
        cookies = c0.cookies()
        c0.close()
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
            profile_url = args.profile or ("https://www.kuaishou.com/profile/" + UID_3X)
            pg.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
            works = dom_collect(pg)
            if args.profile:
                wpath = os.path.join(OUTDIR, "works_list_" + re.sub(r"[^0-9A-Za-z]", "", profile_url.rsplit("/", 1)[-1]) + ".json")
            with open(wpath, "w", encoding="utf-8") as f:
                json.dump(sorted(works.values(), key=lambda x: x["id"]), f, ensure_ascii=False, indent=1)
            print("[collect] 重新收集:", len(works), "->", wpath)
            worklist = json.load(open(wpath, encoding="utf-8"))
        elif not os.path.exists(wpath):
            pg.goto("https://www.kuaishou.com/profile/" + UID_3X, wait_until="domcontentloaded", timeout=60000)
            works = dom_collect(pg)
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
        done_ids, done_fp, skip_ids = set(), set(), set()
        for r in log:
            pid = str(r.get("photoId") or "")
            if r.get("ok"):
                done_ids.add(pid)
                done_fp.add((re.sub(r"[\s#]+", "", (r.get("caption") or "")).lower()[:30], int(r.get("duration_ms") or 0)))
            elif "status_1040" in str(r.get("reason", "")) or "status_208005" in str(r.get("reason", "")):
                skip_ids.add(pid)

        # 统一按台账去重,再应用 limit
        todo = [w for w in worklist if w["id"] not in done_ids and w["id"] not in skip_ids]
        if args.limit:
            todo = todo[: args.limit]
        print("[plan] 待处理 %d | workers=%d sleep=%.2fs" % (len(todo), args.workers, args.sleep))
        if not todo:
            print("[done] 无待下载")
            return

        # ---- 阶段1: 主线程预取全部直链(约1个/秒防风控) ----
        dl_pg = None
        for x in ctx.pages:
            if "www.kuaishou.com/short-video/" in x.url:
                dl_pg = x
                break
        if dl_pg is None:
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
        i = 0
        while i < len(todo):
            w = todo[i]
            pid = w["id"]
            try:
                d = fetch_detail(pid)
                vd = ((d.get("data") or {}).get("visionVideoDetail") or {})
                ph = vd.get("photo") or {}
                if vd.get("status") != 1 or not ph:
                    fail_streak += 1
                    ok_streak = 0
                    if fail_streak >= 2:
                        cur_sleep = min(cur_sleep * 2 + 0.2, 5.0)
                        print("  [throttle] 降速至 %.2fs/个" % cur_sleep)
                    print("  [pre %d/%d] %s 不可用(status_%s), sleep=%.2f" % (i + 1, len(todo), pid, vd.get("status"), cur_sleep))
                    failed.append({"photoId": pid, "caption": w.get("caption", ""), "reason": "status_%s" % vd.get("status")})
                    time.sleep(cur_sleep)
                    i += 1
                    continue
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
        print("[pre] 直链预取完成: %d 个, 耗时 %.1fs (失败 %d)" % (len(jobs), t_pre, len(failed)))

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
