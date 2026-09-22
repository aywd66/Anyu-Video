# -*- coding: utf-8 -*-
"""
kuaishou_cdp_dl.py — 通过 CDP 连接用户真实 Edge 浏览器,执行主页全量作品收集+下载
前置: Edge 已用 --remote-debugging-port=9222 启动,用户已在窗口内登录快手

用法: python kuaishou_cdp_dl.py [输出目录]
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
UID = "3xxjtkcmqqt2fqg"
CDP = "http://127.0.0.1:9222"

DETAIL_Q = ("query visionVideoDetail($photoId: String, $page: String, $webPageArea: String) { "
            "visionVideoDetail(photoId: $photoId, page: $page, webPageArea: $webPageArea) { status type "
            "photo { photoUrl photoH265Url caption duration "
            "manifest { adaptationSet { representation { id qualityType width height avgBitrate url } } } } } }")

def sanitize(name):
    return re.sub(r'[\\/:*?"<>|#\s]+', "_", name).strip("_")[:60] or "kuaishou_video"

def feed_works(body):
    out = []
    try:
        d = json.loads(body)
    except Exception:
        return out
    def walk(o):
        if isinstance(o, dict):
            if "photoId" in o and ("caption" in o or "photoUrl" in o or "headUrl" in o):
                out.append({"id": str(o.get("photoId")), "caption": str(o.get("caption") or ""), "duration": int(o.get("duration") or 0)})
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(d)
    return out

def main():
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
        browser = p.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0]
        print("[cdp] 已连接, pages=%d" % len(ctx.pages))

        collected = {}
        feed_hits = {"n": 0}
        target_page = None

        # 找已打开的快手页或新开
        for pg in ctx.pages:
            if "kuaishou.com" in pg.url:
                target_page = pg
                break
        if target_page is None:
            target_page = ctx.new_page()
            target_page.goto("https://www.kuaishou.com/profile/" + UID, wait_until="domcontentloaded", timeout=60000)
        else:
            if UID not in target_page.url:
                target_page.goto("https://www.kuaishou.com/profile/" + UID, wait_until="domcontentloaded", timeout=60000)

        def on_resp(resp):
            try:
                u = resp.url
                if "/rest/v/profile/feed" not in u and "/live_api/profile/public" not in u:
                    return
                body = resp.text()
                ws = feed_works(body)
                if not ws:
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

        target_page.on("response", on_resp)
        target_page.bring_to_front()
        target_page.wait_for_timeout(6000)
        print("[load] 首屏收集: %d 作品 (若为0请确认已在窗口中登录)" % len(collected))

        # 滚动收集
        last_n, stable = -1, 0
        for i in range(80):
            try:
                target_page.mouse.move(700, 520)
                target_page.mouse.wheel(0, 2400)
            except Exception:
                pass
            target_page.wait_for_timeout(1400)
            n = len(collected)
            if i % 5 == 0:
                print("  r%02d works=%d feed_hits=%d" % (i, n, feed_hits["n"]))
            if n == last_n:
                stable += 1
                if stable >= 20:
                    print("  stable stop: works=%d" % n)
                    break
            else:
                stable = 0
                last_n = n
            if n >= 270:
                break
        print("[collect] 滚动收集完成: %d 作品" % len(collected))

        # gql 分页备用
        if len(collected) < 250:
            print("[gql] GraphQL 分页补漏 ...")
            pcursor, gp = "", 0
            while gp < 100:
                gp += 1
                try:
                    r = target_page.evaluate("""
                        async (args) => {
                            const resp = await fetch('/graphql', {method: 'POST',
                                headers: {'Content-Type': 'application/json'}, credentials: 'include',
                                body: JSON.stringify({operationName: 'visionProfilePhotoList',
                                    variables: {userId: args.uid, pcursor: args.cur, webPageArea: ''},
                                    query: 'query visionProfilePhotoList($userId: String, $pcursor: String, $webPageArea: String) { visionProfilePhotoList(userId: $userId, pcursor: $pcursor, webPageArea: $webPageArea) { result llsid pcursor feeds { type photo { ... on PhotoEntity { id caption duration } } } } }'})});
                            return await resp.text();
                        }
                    """, {"uid": UID, "cur": pcursor})
                except Exception as e:
                    print("  gql exc:", str(e)[:80])
                    break
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
                time.sleep(1.2)

        if not collected:
            print("[FAIL] 未收集到作品")
            sys.exit(2)
        with open(os.path.join(OUTDIR, "works_list.json"), "w", encoding="utf-8") as f:
            json.dump(sorted(collected.values(), key=lambda x: x["id"]), f, ensure_ascii=False, indent=1)
        print("[list] 最终收集 %d 作品, 已存 works_list.json" % len(collected))

        # 批量下载
        todo = [w for w in collected.values() if w["id"] not in done_ids and w["id"] not in skip_ids]
        print("[dl] 待处理 %d" % len(todo))
        ytdlp = [sys.executable, "-m", "yt_dlp"]
        results, failed = [], []
        for i, w in enumerate(todo):
            pid = w["id"]
            fp = (re.sub(r"[\s#]+", "", w["caption"]).lower()[:30], int(w.get("duration") or 0))
            if fp in done_fp:
                results.append({"photoId": pid, "caption": w["caption"], "ok": True, "duplicate": True})
                continue
            try:
                r = target_page.evaluate("""
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

if __name__ == "__main__":
    main()
