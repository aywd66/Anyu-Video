# -*- coding: utf-8 -*-
"""
kuaishou_dl.py v2 — 快手主页/作品分享链接下载工具（配合 yt-dlp 使用）

原理说明（全程为正常浏览器公开数据访问，不破解签名、不绕过验证码、不登录）：
1. 短链（v.kuaishou.com/xxx）用移动 UA 访问，识别「主页分享」或「单作品分享」；
2. 主页分享 → 从移动端分享落地页 SSR 数据提取作品列表（feed_photo|photoId 锚点）；
3. 用真实浏览器渲染作品页，在页面上下文内调用页面自身的公开 GraphQL 接口
   visionVideoDetail，取 photoUrl + photoH265Url + manifest 全部码率轨道；
4. 最优画质选择：manifest 多轨道时选 avgBitrate 最高者（同码率优先 .mp4 直链），
   无 manifest 数据时用 photoUrl；
5. 交给 yt-dlp 下载（--no-overwrites 支持断点续传/跳过已完成）。

用法:
  python kuaishou_dl.py <分享链接> [输出目录] [作品序号|all]
示例:
  python kuaishou_dl.py "https://v.kuaishou.com/J0MdxkzY" "E:/录屏/spxz" all
  python kuaishou_dl.py "https://v.kuaishou.com/J0MdxkzY" "E:/录屏/spxz" 1
"""
import sys, io, json, re, shutil, subprocess, os, time
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---- 合并项目路径解析:定位项目根(含 data\kuaishou),找不到时回落到脚本上级目录 ----
def _find_root():
    _here = os.path.dirname(os.path.abspath(__file__))
    for _d in (_here, os.path.dirname(_here), os.path.dirname(os.path.dirname(_here)),
               os.path.dirname(os.path.dirname(os.path.dirname(_here)))):
        if os.path.isdir(os.path.join(_d, "data", "kuaishou")):
            return _d
    return os.path.dirname(_here)

_HERE_DATA = os.path.join(_find_root(), "data", "kuaishou")

UA_PC = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
UA_MOB = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"

DETAIL_Q = (
    "query visionVideoDetail($photoId: String, $page: String, $webPageArea: String) { "
    "visionVideoDetail(photoId: $photoId, page: $page, webPageArea: $webPageArea) { status type "
    "photo { photoUrl photoH265Url caption duration "
    "manifest { adaptationSet { representation { id qualityType width height avgBitrate url } } } } } }"
)

def die(msg):
    print("[kuaishou_dl] FAIL:", msg)
    sys.exit(2)

def parse_works_from_mobile_share(short_url):
    """主页分享：用移动 UA 访问原始短链，从移动端落地页 SSR 数据提取作品列表"""
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": UA_MOB, "Accept-Language": "zh-CN,zh;q=0.9"})
    r = s.get(short_url, timeout=30, allow_redirects=True)
    h = r.text
    # SSR 锚点：serverExpTag "feed_photo|<photoId>|<userId>|..." 与 caption 配对
    caps = {}
    for m in re.finditer(r'"caption":"([^"]{2,80})"', h):
        seg = h[max(0, m.start() - 1500):m.start() + 1500]
        pids = re.findall(r'feed_photo\|([0-9]{15,25})\|', seg)
        if pids and pids[0] not in caps:
            caps[pids[0]] = m.group(1)
    ids = re.findall(r'feed_photo\|([0-9]{15,25})\|[0-9]+\|', h)
    works, seen = [], set()
    for pid in ids:
        if pid not in seen:
            seen.add(pid)
            works.append({"photoId": pid, "caption": caps.get(pid, "")})
    return works

def _gql_page(page, pid):
    return page.evaluate("""
        async (args) => {
            const resp = await fetch('/graphql', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                credentials: 'include',
                body: JSON.stringify({operationName: 'visionVideoDetail',
                    variables: {photoId: args.pid, page: 'main', webPageArea: ''},
                    query: args.q})
            });
            return await resp.text();
        }
    """, {"pid": pid, "q": DETAIL_Q})

def get_best_source(page, photo_id):
    """查询详情并选择最优画质源，返回 (url, caption, duration_ms, note)"""
    r = _gql_page(page, photo_id)
    try:
        data = json.loads(r)
    except Exception:
        die("详情响应解析失败: %s" % r[:200])
    if data.get("errors"):
        msgs = "; ".join(e.get("message", "")[:80] for e in data["errors"][:2])
        die("详情接口错误: %s" % msgs)
    vd = (data.get("data") or {}).get("visionVideoDetail") or {}
    ph = vd.get("photo") or {}
    if vd.get("status") != 1 or not ph:
        die("作品不可用 status=%s (photoId=%s)" % (vd.get("status"), photo_id))
    caption = ph.get("caption") or ""
    duration = ph.get("duration") or 0
    cands = []  # (bitrate, url, note)
    mf = ph.get("manifest") or {}
    for aset in (mf.get("adaptationSet") or []):
        for rep in (aset.get("representation") or []):
            u = rep.get("url") or ""
            if not u:
                continue
            br = rep.get("avgBitrate") or 0
            qt = rep.get("qualityType") or "?"
            is_mp4 = ".mp4" in u
            note = "%s %sx%s %skbps%s" % (qt, rep.get("width"), rep.get("height"), br, "" if is_mp4 else " [HLS]")
            # 同码率下 mp4 直链优先：mp4 加 0.5 权重
            score = br + (0.5 if is_mp4 else 0)
            cands.append((score, u, note))
    if cands:
        cands.sort(key=lambda x: -x[0])
        score, url, note = cands[0]
        return url, caption, duration, note
    pu = ph.get("photoUrl")
    if pu:
        return pu, caption, duration, "photoUrl(默认轨道)"
    die("无可用视频源 (photoId=%s)" % photo_id)

def sanitize(name):
    return re.sub(r'[\\/:*?"<>|#\s]+', "_", name).strip("_")[:60] or "kuaishou_video"

def main():
    if len(sys.argv) < 2:
        die("用法: python kuaishou_dl.py <分享链接> [输出目录] [作品序号|all]")
    url = sys.argv[1]
    outdir = sys.argv[2] if len(sys.argv) > 2 else _HERE_DATA
    mode = sys.argv[3] if len(sys.argv) > 3 else "1"

    print("[1] 解析分享链接:", url)
    works = parse_works_from_mobile_share(url)
    if not works:
        die("未能从分享页提取作品列表")
    print("[2] 提取到 %d 个作品" % len(works))
    if mode.lower() == "all":
        targets = list(range(1, len(works) + 1))
    else:
        try:
            idx = int(mode)
        except ValueError:
            die("作品序号必须是整数或 all")
        if idx < 1 or idx > len(works):
            die("作品序号超出范围 1-%d" % len(works))
        targets = [idx]

    os.makedirs(outdir, exist_ok=True)
    ytdlp = [sys.executable, "-m", "yt_dlp"]
    if not os.path.exists(os.path.join(os.path.dirname(sys.executable), "yt-dlp.exe")) and shutil.which("yt-dlp") is None:
        print("[kuaishou_dl] WARN: 未找到 yt-dlp（既无环境内 yt-dlp.exe 也无 PATH 上的 yt-dlp），将尝试 python -m yt_dlp")

    from playwright.sync_api import sync_playwright
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        ctx = browser.new_context(user_agent=UA_PC, locale="zh-CN", viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        for seq, idx in enumerate(targets):
            work = works[idx - 1]
            pid = work["photoId"]
            title_hint = work["caption"] or ""
            print("\n--- [%d/%d] 作品 %s | %s" % (seq + 1, len(targets), pid, title_hint[:40]))
            try:
                page.goto("https://www.kuaishou.com/short-video/" + pid, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)
                vurl, cap, dur, note = get_best_source(page, pid)
            except SystemExit as e:
                print("  跳过:", e)
                results.append({"seq": idx, "photoId": pid, "caption": title_hint, "ok": False, "err": str(e)})
                continue
            title = sanitize(cap or title_hint or ("work_" + pid))
            outtmpl = os.path.join(outdir, title + "_[" + pid + "].%(ext)s")
            print("  画质:", note, "| 时长(ms):", dur)
            ret = subprocess.run(ytdlp + ["--no-playlist", "--no-overwrites", "-o", outtmpl, vurl])
            ok = ret.returncode == 0
            results.append({"seq": idx, "photoId": pid, "caption": cap or title_hint,
                            "ok": ok, "quality": note, "duration_ms": dur,
                            "file": outtmpl.replace("%(ext)s", "mp4") if ok else None})
            time.sleep(1.2)  # 温和节奏，降低风控触发
        browser.close()

    okc = sum(1 for r in results if r["ok"])
    print("\n=== SUMMARY: %d/%d 成功 ===" % (okc, len(results)))
    for r in results:
        print(("OK " if r["ok"] else "FAIL ") + r["photoId"] + " | " + (r.get("caption") or "")[:36] + (" | " + r.get("quality", "") if r.get("quality") else ""))
    with open(os.path.join(outdir, "download_log.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    sys.exit(0 if okc == len(results) else 1)

if __name__ == "__main__":
    main()
