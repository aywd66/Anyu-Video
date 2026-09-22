# -*- coding: utf-8 -*-
"""
kuaishou_full_dl.py — 快手主页全量下载（登录态版）

用法:
  1. 按照同目录《Cookie-获取指南.md》导出 www.kuaishou.com 的 Cookie 字符串
  2. python kuaishou_full_dl.py <Cookie字符串或cookie.txt路径> [输出目录]

流程:
  ① Cookie 注入 Playwright 浏览器上下文
  ② GraphQL visionProfilePhotoList 分页拉取主页全部作品(userId+pcursor，登录态可翻页)
     备用：live_api/profile/public
  ③ 与 download_log.json 台账去重(已下载/1040 不可用均跳过)
  ④ 逐作品 visionVideoDetail 取最优画质轨道(按 avgBitrate)→ yt-dlp 下载
  ⑤ 生成/合并 download_log.json 台账，失败清单写入 failed_list.json
"""
import sys, io, json, re, shutil, subprocess, os, time
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---- 合并项目路径解析:定位项目根(含 data\kuaishou),找不到时回落到脚本自身目录 ----
def _find_root():
    _here = os.path.dirname(os.path.abspath(__file__))
    for _d in (_here, os.path.dirname(_here), os.path.dirname(os.path.dirname(_here)),
               os.path.dirname(os.path.dirname(os.path.dirname(_here)))):
        if os.path.isdir(os.path.join(_d, "data", "kuaishou")):
            return os.path.dirname(_d) if os.path.basename(_d) == "tools" else _d
    return os.path.dirname(_here)

_HERE_DATA = os.path.join(_find_root(), "data", "kuaishou")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
UID = "3xxjtkcmqqt2fqg"  # 郁闷装深沉

PL_Q = (
    "query visionProfilePhotoList($userId: String, $pcursor: String, $webPageArea: String) { "
    "visionProfilePhotoList(userId: $userId, pcursor: $pcursor, webPageArea: $webPageArea) { "
    "result llsid pcursor feeds { type photo { ... on PhotoEntity { id caption duration } } } } }"
)
DETAIL_Q = (
    "query visionVideoDetail($photoId: String, $page: String, $webPageArea: String) { "
    "visionVideoDetail(photoId: $photoId, page: $page, webPageArea: $webPageArea) { status type "
    "photo { photoUrl photoH265Url caption duration "
    "manifest { adaptationSet { representation { id qualityType width height avgBitrate url } } } } } }"
)

def die(msg):
    print("[full_dl] FAIL:", msg)
    sys.exit(2)

def load_cookies(src):
    """支持三种格式: ① cookie.txt/json 文件路径 ② JSON ③ 原始 Cookie 头字符串"""
    raw = src
    if os.path.exists(src):
        raw = open(src, encoding="utf-8").read().strip()
    # 尝试 JSON
    try:
        j = json.loads(raw)
        if isinstance(j, list):
            out = []
            for c in j:
                out.append({"name": c["name"], "value": c["value"],
                            "domain": c.get("domain", ".kuaishou.com"),
                            "path": c.get("path", "/")})
            return out
        if isinstance(j, dict):
            return [{"name": k, "value": v, "domain": ".kuaishou.com", "path": "/"} for k, v in j.items()]
    except Exception:
        pass
    # 原始字符串: name=value; name2=value2
    pairs = re.findall(r'([A-Za-z0-9_\.]+)=([^;]+)', raw)
    if not pairs:
        die("无法解析 Cookie（既不是文件也不是 name=value 串）")
    return [{"name": k.strip(), "value": v.strip().strip('"'), "domain": ".kuaishou.com", "path": "/"} for k, v in pairs]

def sanitize(name):
    return re.sub(r'[\\/:*?"<>|#\s]+', "_", name).strip("_")[:60] or "kuaishou_video"

def main():
    if len(sys.argv) < 2:
        die("用法: python kuaishou_full_dl.py <Cookie字符串或文件路径> [输出目录]")
    cookies = load_cookies(sys.argv[1])
    outdir = sys.argv[2] if len(sys.argv) > 2 else _HERE_DATA
    names = [c["name"] for c in cookies]
    has_login = any(n in ("passToken", "kuaishou.server.web_st", "kuaishou.server.web_day7_st", "userId") for n in names)
    print("[1] Cookie 解析成功: %d 项 (%s...), 登录关键项=%s" % (len(cookies), ",".join(names[:6]), has_login))
    if not has_login:
        die("Cookie 中缺少登录项(passToken/web_st/userId)——请确认从已登录的 kuaishou.com 页面导出")

    # 台账与去重集
    log_path = os.path.join(outdir, "download_log.json")
    log = json.load(open(log_path, encoding="utf-8")) if os.path.exists(log_path) else []
    done_ids, done_fp, skip_ids = set(), set(), set()
    for r in log:
        pid = str(r.get("photoId") or "")
        if r.get("ok"):
            done_ids.add(pid)
            fp = (re.sub(r"[\s#]+", "", (r.get("caption") or "")).lower()[:30], int(r.get("duration_ms") or 0))
            done_fp.add(fp)
        elif "status_1040" in str(r.get("reason", "")):
            skip_ids.add(pid)

    ytdlp = [sys.executable, "-m", "yt_dlp"]
    if not os.path.exists(os.path.join(os.path.dirname(sys.executable), "yt-dlp.exe")) and shutil.which("yt-dlp") is None:
        print("[kuaishou_full_dl] WARN: 未找到 yt-dlp,将尝试 python -m yt_dlp")

    from playwright.sync_api import sync_playwright
    # 优先复用扫码登录的持久化会话(登录态完整,含 HttpOnly Cookie)
    # 注意:快手风控对 headless 降权,必须用有头窗口(headless=False)才能保持登录态
    profile = os.path.join(_find_root(), "runtime", "kuaishou-login-profile")
    if not os.path.isdir(profile):
        profile = os.path.join(outdir, "login-profile")
    use_profile = os.path.isdir(profile)
    if use_profile:
        print("[1b] 使用持久化登录会话(有头模式):", profile)
    with sync_playwright() as p:
        if use_profile:
            ctx = p.chromium.launch_persistent_context(
                profile, channel="msedge", headless=False,
                user_agent=UA, locale="zh-CN", viewport={"width": 1280, "height": 900})
        else:
            browser = p.chromium.launch(channel="msedge", headless=False)
            ctx = browser.new_context(user_agent=UA, locale="zh-CN", viewport={"width": 1280, "height": 900})
            ctx.add_cookies(cookies)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.kuaishou.com", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        # 登录校验
        chk = page.evaluate("""
            async () => {
                const resp = await fetch('/graphql', {method: 'POST',
                    headers: {'Content-Type': 'application/json'}, credentials: 'include',
                    body: JSON.stringify({operationName: 'checkLogin', variables: {},
                        query: 'query checkLogin { checkLogin }'})});
                return await resp.text();
            }
        """)
        print("[2] checkLogin:", chk[:100])
        if '"checkLogin":true' not in chk:
            print("[2b] checkLogin=false(风控降权常见),继续以列表接口实测为准...")

        # ② 分页拉取全部作品
        works, pcursor, pages = {}, "", 0
        while pages < 200:
            pages += 1
            r = page.evaluate("""
                async (args) => {
                    const resp = await fetch('/graphql', {method: 'POST',
                        headers: {'Content-Type': 'application/json'}, credentials: 'include',
                        body: JSON.stringify({operationName: 'visionProfilePhotoList',
                            variables: {userId: args.uid, pcursor: args.cur, webPageArea: ''},
                            query: args.q})});
                    return await resp.text();
                }
            """, {"uid": UID, "cur": pcursor, "q": PL_Q})
            try:
                d = json.loads(r)
            except Exception:
                die("列表响应解析失败: %s" % r[:150])
            pl = ((d.get("data") or {}).get("visionProfilePhotoList") or {})
            feeds = pl.get("feeds") or []
            new = 0
            for f in feeds:
                ph = f.get("photo") or {}
                if ph.get("id"):
                    works[ph["id"]] = {"id": ph["id"], "caption": ph.get("caption") or "", "duration": ph.get("duration") or 0}
                    new += 1
            nxt = pl.get("pcursor")
            print("  列表P%02d: feeds=%d new=%d total=%d next=%s" % (pages, len(feeds), new, len(works), str(nxt)[:20] or "<END>"))
            if not nxt or not feeds:
                break
            pcursor = str(nxt)
            time.sleep(1.0)
        if not works:
            die("列表拉取为空——登录态不完整或接口变更；建议重新扫码后立即运行")
        print("[3] 主页作品总数:", len(works))

        # ③④ 去重 + 批量下载
        todo = [w for w in works.values() if str(w["id"]) not in done_ids and str(w["id"]) not in skip_ids]
        print("[4] 待下载: %d (已下载 %d, 1040跳过 %d)" % (len(todo), len(done_ids), len(skip_ids)))
        results, failed = [], []
        for i, w in enumerate(todo):
            pid = str(w["id"])
            fp = (re.sub(r"[\s#]+", "", w["caption"]).lower()[:30], int(w["duration"] or 0))
            if fp in done_fp:
                print("  [%d/%d] %s 内容重复, 跳过" % (i + 1, len(todo), pid))
                results.append({"photoId": pid, "caption": w["caption"], "ok": True, "duplicate": True})
                continue
            try:
                # 登录态下从同一页面上下文直接调详情接口,无需逐作品导航(更快更稳)
                r = page.evaluate("""
                    async (args) => {
                        const resp = await fetch('/graphql', {method: 'POST',
                            headers: {'Content-Type': 'application/json'}, credentials: 'include',
                            body: JSON.stringify({operationName: 'visionVideoDetail',
                                variables: {photoId: args.pid, page: 'main', webPageArea: ''},
                                query: args.q})});
                        return await resp.text();
                    }
                """, {"pid": pid, "q": DETAIL_Q})
                d = json.loads(r)
                vd = ((d.get("data") or {}).get("visionVideoDetail") or {})
                ph = vd.get("photo") or {}
                if vd.get("status") != 1 or not ph:
                    reason = "status_%s" % vd.get("status")
                    print("  [%d/%d] %s 不可用(%s)" % (i + 1, len(todo), pid, reason))
                    failed.append({"photoId": pid, "caption": w["caption"], "reason": reason})
                    results.append({"photoId": pid, "caption": w["caption"], "ok": False, "reason": reason})
                    continue
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
                    failed.append({"photoId": pid, "caption": w["caption"], "reason": "no_source"})
                    continue
                cands.sort(key=lambda x: -x[0])
                _, vurl, note = cands[0]
                title = sanitize(ph.get("caption") or w["caption"] or pid)
                outtmpl = os.path.join(outdir, title + "_[" + pid + "].%(ext)s")
                print("  [%d/%d] %s | %s | %s" % (i + 1, len(todo), pid, (ph.get("caption") or "")[:24], note))
                ret = subprocess.run(ytdlp + ["--no-playlist", "--no-overwrites", "-o", outtmpl, vurl])
                ok = ret.returncode == 0
                results.append({"photoId": pid, "caption": ph.get("caption") or w["caption"],
                                "ok": ok, "quality": note, "duration_ms": ph.get("duration"),
                                "file": outtmpl.replace("%(ext)s", "mp4") if ok else None})
                if ok:
                    done_fp.add(fp)
                else:
                    failed.append({"photoId": pid, "caption": w["caption"], "reason": "ytdlp_exit_%d" % ret.returncode})
                time.sleep(1.2)
            except Exception as e:
                print("  [%d/%d] %s EXC: %s" % (i + 1, len(todo), pid, str(e)[:90]))
                failed.append({"photoId": pid, "caption": w["caption"], "reason": str(e)[:150]})
    try:
        ctx.close()
    except Exception:
        pass

    # ⑤ 台账合并
    alllog = log + results
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(alllog, f, ensure_ascii=False, indent=1)
    with open(os.path.join(outdir, "failed_list.json"), "w", encoding="utf-8") as f:
        json.dump(failed, f, ensure_ascii=False, indent=1)
    okn = sum(1 for x in results if x.get("ok") and not x.get("duplicate"))
    print("=== DONE: 新下载 %d, 失败 %d, 台账总数 %d ===" % (okn, len(failed), len(alllog)))
    sys.exit(0)

if __name__ == "__main__":
    main()
