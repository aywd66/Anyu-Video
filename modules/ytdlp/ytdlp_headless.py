#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""yt-dlp 万能站点下载器（与 ty_headless.py 同一套任务对接约定）。

定位：工具箱里抖音/快手/B站/TikTok/YouTube 都有专属模块后，这里补上
yt-dlp 剩余的**全部其它站点**（官方仓库内置 ~1754 个提取器：Twitter/X、
Instagram、微博、Vimeo、Twitch 回放、小红书……）以及 yt-dlp 独有的
后处理能力（音频提取、字幕、SponsorBlock、存档去重）。

调用约定（与 ty/bilibili headless 一致）：
  用法：python ytdlp_headless.py <command> ...
    download <url...> [选项]   下载（详见 argparse）
    preview  <url>             解析预览：stdout 输出一段 JSON，stderr 走日志
    update                     在当前环境内 pip 升级 yt-dlp
环境变量：
  YTDLP_OUTDIR    产物根目录（默认 downloads/ytdlp）
  YTDLP_PROXY     显式代理（如 http://127.0.0.1:7897）；不设则自动探测，
                  直连可达的站点（微博等境内站）优先直连
  YTDLP_COOKIES   cookies.txt 路径（--cookies-file 未给时兜底）
  YTDLP_ARCHIVE   存档文件路径（默认 data/ytdlp/archive.txt）
  YTDLP_CONF      高级配置文件（默认 data/ytdlp/yt-dlp.conf，存在即生效）

退出码：0 成功（含部分成功）/ 2 参数无效或站点不可达 / 1 下载失败。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLBOX_ROOT = os.path.dirname(os.path.dirname(_HERE))          # 工具箱根目录
DEFAULT_OUTDIR = os.path.join(_TOOLBOX_ROOT, "downloads", "ytdlp")
DEFAULT_DATA = os.path.join(_TOOLBOX_ROOT, "data", "ytdlp")

MARK = "[ytdlp]"

# 与 ty_headless 相同的本地代理常见端口（Clash Verge 7897，老 Clash 7890，v2rayN 10808/10809）
_PROXY_PORTS_HTTP = [7897, 7890, 7891, 10809, 10808]
_PROXY_PORTS_SOCKS = [1080]

QUALITY_FILTER = {
    "best": "bv*+ba/b",
    "2160": "bv*[height<=2160]+ba/b[height<=2160]",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]",
    "720": "bv*[height<=720]+ba/b[height<=720]",
    "480": "bv*[height<=480]+ba/b[height<=480]",
}


def log(msg):
    print(msg, flush=True)


def _ensure_ascii_ca():
    """与 ty_headless 同因同治：venv 路径含中文时 curl_cffi 加载不了 certifi
    的 CA 包，把证书复制到纯 ASCII 路径再用环境变量指过去。"""
    try:
        import certifi
        src = certifi.where()
        base = os.environ.get("ProgramData") or tempfile.gettempdir()
        dst = os.path.join(base, "yt_dlp_cacert.pem")
        if not os.path.exists(dst) or os.path.getmtime(dst) < os.path.getmtime(src):
            shutil.copyfile(src, dst)
        for k in ("SSL_CERT_FILE", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE"):
            os.environ[k] = dst
    except Exception as e:
        log("%s ⚠️ CA 证书 ASCII 化失败（一般不影响下载）: %s" % (MARK, e))


def ytdlp_cmd():
    # 本脚本由 kuaishou-env 解释器拉起（yt_dlp 装在该环境），sys.executable 即可
    return [sys.executable, "-m", "yt_dlp"]


def find_ffmpeg():
    cands = []
    try:
        p = shutil.which("ffmpeg")
        if p:
            cands.append(p)
    except Exception:
        pass
    try:
        import imageio_ffmpeg
        cands.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


# ---------------------------------------------------------------- 代理探测

def _url_reachable(url, proxy=None, timeout=4):
    import urllib.error
    import urllib.request
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    op = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        r = op.open(req, timeout=timeout)
        return r.status < 500
    except urllib.error.HTTPError as e:
        # 4xx 也是"服务器有响应"= 可达。很多站点根本没有 robots.txt（404），
        # 按"不可达"误判会让境内可用站被错送去要求代理（ty_headless 同款
        # 探测只探测了 robots.txt 存在的 YouTube/TikTok，没暴露这个坑）。
        return e.code < 500
    except Exception:
        return False


def pick_proxy(url):
    """按目标站探测代理：直连可达（境内站）就不走代理；直连不通再试本地端口。
    返回代理 URL、None（直连可用）或 ""（需要代理但没找到）。"""
    p = urlparse(url)
    probe = "%s://%s/robots.txt" % (p.scheme or "https", p.netloc)
    explicit = (os.environ.get("YTDLP_PROXY") or "").strip()
    if explicit:
        if _url_reachable(probe, proxy=explicit, timeout=6):
            return explicit
        log("%s ⚠️ YTDLP_PROXY=%r 经它访问不了目标站，继续自动探测" % (MARK, explicit))
    if _url_reachable(probe, timeout=4):
        log("%s 直连可用，不走代理：%s" % (MARK, p.netloc))
        return None
    for port in _PROXY_PORTS_HTTP:
        cand = "http://127.0.0.1:%d" % port
        if _url_reachable(probe, proxy=cand, timeout=3):
            log("%s 检测到本地代理 %s 可用" % (MARK, cand))
            return cand
    for port in _PROXY_PORTS_SOCKS:
        cand = "socks5://127.0.0.1:%d" % port
        if _url_reachable(probe, proxy=cand, timeout=3):
            log("%s 检测到本地 SOCKS 代理 %s 可用" % (MARK, cand))
            return cand
    return ""   # 空串 = 需要代理但没有


# ---------------------------------------------------------------- 参数构造

def base_args(outdir):
    args = ["--no-overwrites", "--newline", "--retries", "5",
            "--no-keep-video", "--merge-output-format", "mp4",
            "--no-mtime",
            "-P", "home:" + outdir,
            "-P", "temp:" + os.path.join(outdir, ".tmp"),
            "-o", "%(uploader,channel,uploader_id|未知作者)s/%(title).80s_[%(id)s].%(ext)s"]
    ff = find_ffmpeg()
    if ff:
        args += ["--ffmpeg-location", ff]
    else:
        log("%s ⚠️ 未找到 ffmpeg：合并/转码/字幕烧录类操作会受限" % MARK)
    return args


def cookie_args(opts):
    browser = (opts.get("cookies_browser") or "").strip()
    cfile = (opts.get("cookies_file") or "").strip() or (os.environ.get("YTDLP_COOKIES") or "").strip()
    if cfile and os.path.exists(cfile):
        return ["--cookies", cfile]
    if browser and browser != "none":
        return ["--cookies-from-browser", browser]
    return []


def config_arg():
    conf = (os.environ.get("YTDLP_CONF") or os.path.join(DEFAULT_DATA, "yt-dlp.conf")).strip()
    if conf and os.path.exists(conf):
        return ["--config-location", conf]
    return []


def archive_arg(enabled):
    if not enabled:
        return []
    arc = (os.environ.get("YTDLP_ARCHIVE") or os.path.join(DEFAULT_DATA, "archive.txt")).strip()
    os.makedirs(os.path.dirname(arc) or ".", exist_ok=True)
    return ["--download-archive", arc]


def build_download_args(url, opts, outdir, proxy):
    """把一次下载的全部选项翻译成 yt-dlp 参数。"""
    args = base_args(outdir) + config_arg()
    if proxy:
        args += ["--proxy", proxy]
    args += cookie_args(opts)

    # 画质
    q = (opts.get("quality") or "best").strip()
    fmt = QUALITY_FILTER.get(q)
    if fmt:
        args += ["-f", fmt]

    # 播放列表：1 = 只要单条（链接指向列表时不整单下）；all/纯数字 N = 放行整单（N 限量）
    mode = str(opts.get("mode") or "all").strip().lower()
    if mode == "1":
        args += ["--no-playlist"]
    elif mode.isdigit():
        args += ["--yes-playlist", "--playlist-items", "1:%s" % mode]
    else:
        args += ["--yes-playlist"]

    # 音频提取
    af = (opts.get("audio_format") or "").strip().lower()
    if af and af != "none":
        args += ["-x", "--audio-format", af, "--embed-thumbnail", "--embed-metadata"]
    else:
        args += ["--embed-metadata"]

    # 字幕：write = 外挂 srt；embed = 内嵌；none
    subs = (opts.get("subs") or "none").strip().lower()
    if subs in ("write", "embed"):
        langs = (opts.get("subs_langs") or "zh-CN,zh-Hans,en").strip()
        args += ["--sub-langs", langs]
        if subs == "embed":
            args += ["--embed-subs"]
        else:
            args += ["--write-subs", "--write-auto-subs", "--convert-subs", "srt"]

    # SponsorBlock（YouTube）：标记广告/推广段落
    if opts.get("sponsorblock"):
        args += ["--sponsorblock-mark", "all"]

    args += archive_arg(bool(opts.get("archive")))
    args += [url]
    return args


# ---------------------------------------------------------------- preview

def preview(url, proxy):
    """解析预览：先 flat-playlist 快速判型；单视频再取完整信息（含格式列表）。"""
    cmd = ytdlp_cmd() + ["--flat-playlist", "-J", "--no-warnings"]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd += [url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "解析超时（180 秒）", "url": url}
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        return {"ok": False, "error": "\n".join(tail) or "yt-dlp 退出码 %d" % r.returncode, "url": url}
    try:
        d = json.loads(r.stdout)
    except Exception:
        return {"ok": False, "error": "解析输出不是合法 JSON", "url": url}

    if d.get("_type") == "playlist":
        entries = d.get("entries") or []
        names = [(e.get("title") or e.get("id") or "") for e in entries[:20]]
        return {"ok": True, "type": "playlist", "url": url,
                "title": d.get("title") or "", "uploader": d.get("channel") or d.get("uploader") or "",
                "count": len(entries), "entries_preview": names}
    # 单视频：再跑一次完整 -J 拿格式列表（flat 模式拿不到 formats）
    cmd = ytdlp_cmd() + ["-J", "--no-warnings"]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd += [url]
    try:
        r2 = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=180)
    except subprocess.TimeoutExpired:
        # flat 那次已经证明链接能解析，完整信息拿不到就给基本盘
        return {"ok": True, "type": "video", "url": url,
                "title": d.get("title") or "", "uploader": d.get("uploader") or "",
                "duration": d.get("duration"), "formats": []}
    if r2.returncode != 0:
        return {"ok": True, "type": "video", "url": url,
                "title": d.get("title") or "", "uploader": d.get("uploader") or "",
                "duration": d.get("duration"), "formats": []}
    try:
        full = json.loads(r2.stdout)
    except Exception:
        full = d
    fmts = []
    for f in (full.get("formats") or [])[-40:]:
        if not f.get("format_id"):
            continue
        fmts.append({"id": f.get("format_id"),
                     "ext": f.get("ext") or "",
                     "h": f.get("height"), "fps": f.get("fps"),
                     "note": (f.get("format_note") or "")[:40],
                     "size": f.get("filesize") or f.get("filesize_approx")})
    return {"ok": True, "type": "video", "url": url,
            "title": full.get("title") or d.get("title") or "",
            "uploader": full.get("uploader") or d.get("uploader") or "",
            "duration": full.get("duration") or d.get("duration"),
            "webpage_url": full.get("webpage_url") or url,
            "formats": fmts}


# ---------------------------------------------------------------- commands

def cmd_download(opts):
    urls = [u.strip() for u in (opts.pop("urls") or []) if (u or "").strip().startswith("http")]
    if not urls:
        log("%s [错误] 未提供 http(s) 链接" % MARK)
        return 2
    _ensure_ascii_ca()

    outdir = (os.environ.get("YTDLP_OUTDIR") or DEFAULT_OUTDIR).strip()
    os.makedirs(os.path.join(outdir, ".tmp"), exist_ok=True)
    log("%s 保存到: %s" % (MARK, outdir))

    ok = 0
    for i, u in enumerate(urls, 1):
        proxy = pick_proxy(u)
        if proxy == "":
            log("%s ❌ %s 直连不通，也没探测到可用的本地代理。请先打开代理软件"
                "（如 Clash Verge）再重试，或设置环境变量 YTDLP_PROXY。" % (MARK, urlparse(u).netloc))
            # 与任务中心的解析约定一致：失败也要有可数的标记行（[ytdlp] 失败 i/N）
            log("%s 失败 %d/%d（站点不可达）" % (MARK, i, len(urls)))
            continue
        args = build_download_args(u, opts, outdir, proxy)
        log("%s (%d/%d) 开始：%s" % (MARK, i, len(urls), u[:100]))
        rc = subprocess.run(ytdlp_cmd() + args).returncode
        if rc == 0:
            ok += 1
            # 桥接的任务中心按 `[ytdlp] 完成 i/N` 计数（与 [ty]/[bili] 同约定）
            log("%s 完成 %d/%d" % (MARK, i, len(urls)))
        else:
            log("%s 失败 %d/%d（退出码 %d）" % (MARK, i, len(urls), rc))
    total = len(urls)
    log("[完成] %s" % ("下载成功" if ok == total else
                       ("部分成功 %d/%d" % (ok, total) if ok else "下载失败")))
    return 0 if ok else 1


def cmd_preview(url):
    if not (url or "").strip().startswith("http"):
        print(json.dumps({"ok": False, "error": "未提供 http(s) 链接"}, ensure_ascii=False))
        return 2
    _ensure_ascii_ca()
    proxy = pick_proxy(url)
    if proxy == "":
        print(json.dumps({"ok": False, "error": "目标站直连不通，也没探测到本地代理；请先开代理软件"},
                         ensure_ascii=False))
        return 2
    out = preview(url.strip(), proxy)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


def cmd_update():
    log("%s 当前版本：" % MARK)
    subprocess.run(ytdlp_cmd() + ["--version"])
    log("%s 正在升级（pip install -U yt-dlp）…" % MARK)
    rc = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"]).returncode
    log("%s 升级%s，新版本：" % (MARK, "完成" if rc == 0 else "失败（退出码 %d）" % rc))
    subprocess.run(ytdlp_cmd() + ["--version"])
    return rc


def main():
    ap = argparse.ArgumentParser(prog="ytdlp_headless", description="yt-dlp 万能站点下载器")
    sub = ap.add_subparsers(dest="command", required=True)

    dp = sub.add_parser("download", help="下载")
    dp.add_argument("urls", nargs="+", help="一个或多个 http(s) 链接")
    dp.add_argument("--mode", default="all", help="all=列表全量 / 1=仅单条 / 纯数字 N=列表限量前 N")
    dp.add_argument("--quality", default="best", choices=list(QUALITY_FILTER) + ["audio"],
                    help="画质上限（audio 时请改用 --audio-format）")
    dp.add_argument("--audio-format", default="none", help="mp3/m4a/flac/none；非 none 时提取音频")
    dp.add_argument("--subs", default="none", choices=["none", "write", "embed"], help="字幕模式")
    dp.add_argument("--subs-langs", default="zh-CN,zh-Hans,en", help="字幕语言，逗号分隔")
    dp.add_argument("--cookies-browser", default="none", help="edge/chrome/firefox/none")
    dp.add_argument("--cookies-file", default="", help="cookies.txt 路径")
    dp.add_argument("--archive", action="store_true", help="启用下载存档去重")
    dp.add_argument("--sponsorblock", action="store_true", help="启用 SponsorBlock 标记（YouTube）")
    dp.set_defaults(fn=lambda o: cmd_download(vars(o)))

    pp = sub.add_parser("preview", help="解析预览（stdout 输出 JSON）")
    pp.add_argument("url")
    pp.set_defaults(fn=lambda o: cmd_preview(o.url))

    up = sub.add_parser("update", help="升级 yt-dlp")
    up.set_defaults(fn=lambda o: cmd_update())

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
