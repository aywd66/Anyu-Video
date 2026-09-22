# -*- coding: utf-8 -*-
"""
kuaishou_onestop.py v2 — 扫码登录 + 主页全量作品下载(一条龙)

v2 关键变化:不再依赖 checkLogin 接口(会被风控干扰)。
- 登录判定 = Cookie 存在登录项 且 页面自身作品流接口 /rest/v/profile/feed 返回非空作品
- 列表收集 = 双通道:
    A) 拦截页面滚动加载的 profile/feed 响应(页面自身签名请求,最稳)
    B) 活跃会话内 GraphQL visionProfilePhotoList 分页(备用)
- 下载 = 活跃会话内 visionVideoDetail 取最优轨道 → yt-dlp
"""
import sys, io, os, json, re, shutil, time, subprocess
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

_DEFAULT_OUT = os.path.join(_find_root(), "data", "kuaishou")

OUTDIR = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_OUT
PROFILE = os.path.join(OUTDIR, "login-profile")
UID = "3xxjtkcmqqt2fqg"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
LOGIN_KEYS = ("passToken", "kuaishou.server.web_st", "kuaishou.server.web_day7_st", "userId")

DETAIL_Q = ("query visionVideoDetail($photoId: String, $page: String, $webPageArea: String) { "
            "visionVideoDetail(photoId: $photoId, page: $page, webPageArea: $webPageArea) { status type "
            "photo { photoUrl photoH265Url caption duration "
            "manifest { adaptationSet { representation { id qualityType width height avgBitrate url } } } } } }")

def sanitize(name):
    return re.sub(r'[\\/:*?"<>|#\s]+', "_", name).strip("_")[:60] or "kuaishou_video"

def feed_works(body):
    """从 /rest/v/profile/feed 响应提取作品(自适应多种结构)"""
    out = []
    try:
        d = json.loads(body)
    except Exception:
        return out
    def walk(o):
        if isinstance(o, dict):
            # PhotoEntity 样式
            if "photoId" in o and ("caption" in o or "photoUrl" in o):
                pid = str(o.get("photoId"))
                out.append({"id": pid, "caption": str(o.get("caption") or ""), "duration": int(o.get("duration") or 0)})
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(d)
    return out

def main():
    os.makedirs(OUTDIR, exist_ok=True)
    log_path = os.path.join(OUTDIR, "download_log.json")
    log = json.load(open(log_path, encoding="utf-8")) if os.path.exists(log_path) else []
    done_ids, done_fp, skip_ids = set(), set(), set()
    for r in log:
        pid = str(r.get("photoId") or "")
        if r.get("ok"):
            done_ids.add(pid)
            done_fp.add((re.sub(r"[\s#]+", "", (r.get("caption") or "")).lower()[:30], int(r.get("duration_ms") or 0)))
        elif "status_1040" in str(r.get("reason", "")):
            skip_ids.add(pid)
    print("[init] 台账: 已下载=%d 1040跳过=%d" % (len(done_ids), len(skip_ids)))

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE, channel="msedge", headless=False,
            user_agent=UA, locale="zh-CN", viewport={"width": 1300, "height": 950})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        feed_hits = {"n": 0}
        collected = {}

        def on_resp(resp):
            try:
                u = resp.url
                if "/rest/v/profile/feed" not in u and "/live_api/profile/public" not in u:
                    return
                body = resp.text()
                ws = feed_works(body)
                if not ws:
                    # live_api 形态
                    try:
                        d = json.loads(body)
                        data = d.get("data") or {}
                        for x in (data.get("list") or []):
                            if x.get("type") == "work" and x.get("id"):
                                ws.append({"id": x["id"], "caption": "", "duration": 0})
                    except Exception:
                        pass
                if ws:
                    feed_hits["n"] += 1
                    for w in ws:
                        collected[w["id"]] = w
            except Exception:
                pass

        page.on("response", on_resp)
        page.goto("https://www.kuaishou.com/profile/" + UID, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        print("[load] 首屏收集作品:", len(collected))

        # 登录判定:Cookie 登录项存在 + 滚动后 feed 有新作品(接口可用即视为有效登录/会话)
        names = {c["name"] for c in (ctx.cookies() if hasattr(ctx, "cookies") else [])}
        has_login_cookie = any(k in names for k in LOGIN_KEYS)
        print("[login] Cookie 登录项:", has_login_cookie)

        # --- 滚动收集(页面自身请求) ---
        print("[scroll] 开始面板滚动收集(最多 60 轮,作品数到 260 或连续 18 轮无增长即止) ...")
        last_n, stable = -1, 0
        for i in range(60):
            page.mouse.move(700, 500)
            page.mouse.wheel(0, 2200)
            page.wait_for_timeout(1300)
            n = len(collected)
            if i % 5 == 0:
                print("  r%02d works=%d feed_hits=%d" % (i, n, feed_hits["n"]))
            if n == last_n:
                stable += 1
                if stable >= 18:
                    print("  stable, stop at works=%d" % n)
                    break
            else:
                stable = 0
                last_n = n
            if n >= 260:
                print("  reached 260+, stop")
                break
        print("[scroll] 收集完成: %d 作品 (feed_hits=%d)" % (len(collected), feed_hits["n"]))

        # --- 备用通道:GraphQL 分页(活跃会话内可能可用) ---
        if len(collected) < 100 and has_login_cookie:
            print("[gql] 尝试 GraphQL 分页备用通道 ...")
            pcursor, gp = "", 0
            while gp < 100:
                gp += 1
                r = page.evaluate("""
                    async (args) => {
                        const resp = await fetch('/graphql', {method: 'POST',
                            headers: {'Content-Type': 'application/json'}, credentials: 'include',
                            body: JSON.stringify({operationName: 'visionProfilePhotoList',
                                variables: {userId: args.uid, pcursor: args.cur, webPageArea: ''},
                                query: 'query visionProfilePhotoList($userId: String, $pcursor: String, $webPageArea: String) { visionProfilePhotoList(userId: $userId, pcursor: $pcursor, webPageArea: $webPageArea) { result llsid pcursor feeds { type photo { ... on PhotoEntity { id caption duration } } } } }'})});
                        return await resp.text();
                    }
                """, {"uid": UID, "cur": pcursor})
                try:
                    d = json.loads(r)
                except Exception:
                    break
                pl = ((d.get("data") or {}).get("visionProfilePhotoList") or {})
                feeds = pl.get("feeds") or []
                new = 0
                for f in feeds:
                    ph = f.get("photo") or {}
                    if ph.get("id"):
                        pid = str(ph["id"])
                        if pid not in collected:
                            collected[pid] = {"id": pid, "caption": ph.get("caption") or "", "duration": ph.get("duration") or 0}
                            new += 1
                nxt = pl.get("pcursor")
                print("  gqlP%02d feeds=%d new=%d total=%d next=%s" % (gp, len(feeds), new, len(collected), str(nxt)[:16] or "<END>"))
                if not nxt or not feeds:
                    break
                pcursor = str(nxt)
                time.sleep(1.0)

        if not collected:
            print("[FAIL] 未收集到任何作品")
            ctx.close()
            sys.exit(2)
        print("[list] 总计收集作品:", len(collected))

        # --- 下载 ---
        todo = [w for w in collected.values() if w["id"] not in done_ids and w["id"] not in skip_ids]
        print("[dl] 待处理 %d (已下载 %d 跳过 %d)" % (len(todo), len(done_ids), len(skip_ids)))
        ytdlp = [sys.executable, "-m", "yt_dlp"]
        results, failed = [], []
        for i, w in enumerate(todo):
            pid = w["id"]
            fp = (re.sub(r"[\s#]+", "", w["caption"]).lower()[:30], int(w.get("duration") or 0))
            if fp in done_fp:
                results.append({"photoId": pid, "caption": w["caption"], "ok": True, "duplicate": True})
                continue
            try:
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
                outtmpl = os.path.join(OUTDIR, title + "_[" + pid + "].%(ext)s")
                print("  [%d/%d] %s | %s | %s" % (i + 1, len(todo), pid, (ph.get("caption") or "")[:22], note))
                ret = subprocess.run(ytdlp + ["--no-playlist", "--no-overwrites", "-o", outtmpl, vurl])
                ok = ret.returncode == 0
                results.append({"photoId": pid, "caption": ph.get("caption") or w["caption"],
                                "ok": ok, "quality": note, "duration_ms": ph.get("duration"),
                                "file": outtmpl.replace("%(ext)s", "mp4") if ok else None})
                if ok:
                    done_fp.add(fp)
                    done_ids.add(pid)
                else:
                    failed.append({"photoId": pid, "caption": w["caption"], "reason": "ytdlp_exit_%d" % ret.returncode})
                time.sleep(1.2)
            except Exception as e:
                print("  [%d/%d] %s EXC: %s" % (i + 1, len(todo), pid, str(e)[:90]))
                failed.append({"photoId": pid, "caption": w["caption"], "reason": str(e)[:150]})

        alllog = log + results
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(alllog, f, ensure_ascii=False, indent=1)
        with open(os.path.join(OUTDIR, "failed_list.json"), "w", encoding="utf-8") as f:
            json.dump(failed, f, ensure_ascii=False, indent=1)
        okn = sum(1 for x in results if x.get("ok") and not x.get("duplicate"))
        print("=== DONE: 新下载 %d, 失败 %d, 台账总数 %d ===" % (okn, len(failed), len(alllog)))
        ctx.close()

if __name__ == "__main__":
    main()
