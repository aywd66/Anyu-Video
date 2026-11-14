#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TikTok / YouTube 无 GUI 下载器（与 bilibili_headless.py 同一套任务对接约定）。

为什么单独一个模块而不是塞进快手/B站：这两个站点的提取器 yt-dlp 原生就有
（不像快手要自制），但**境内网络直连不通**，必须经代理 —— 本模块的核心增值
就是「自动探测本地代理 + 清晰的失败提示」，这是其它模块没有的逻辑。

用法：python ty_headless.py <url> [mode]
  mode: all（默认，列表类链接全量）/ 1 / 纯数字 N（列表限量前 N 个）
       单视频链接忽略 mode。
环境变量：
  TY_OUTDIR  产物根目录（默认 downloads/ty）；按平台自动分子目录 tiktok/ youtube/
  TY_PROXY   显式代理（如 http://127.0.0.1:7897）；不设则自动探测常见本地端口，
             直连可用时优先直连。

退出码：0 成功 / 2 参数无效或需要代理 / 3 解析不出作品 / 1 下载失败。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLBOX_ROOT = os.path.dirname(os.path.dirname(_HERE))          # 视频下载工具箱/
DEFAULT_OUTDIR = os.path.join(_TOOLBOX_ROOT, "downloads", "ty")

# 与抖音/快手/B站一致的进度标记，任务中心按 `[ty] 完成 i/N` 解析
MARK = "[ty]"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")

# 本地代理常见端口（Clash Verge 默认 7897，老 Clash 7890，v2rayN 10808/10809）
_PROXY_PORTS_HTTP = [7897, 7890, 7891, 10809, 10808]
_PROXY_PORTS_SOCKS = [1080]


def log(msg):
    print(msg, flush=True)


def _ensure_ascii_ca():
    """curl_cffi 在含中文的 venv 路径下加载不了 certifi 的 CA 包（curl 77，
    实测 2026-10-24：TikTok 提取强制走 curl_cffi 伪装，工具箱目录名带中文就必挂）。
    把证书复制到纯 ASCII 路径，用环境变量指过去 —— 对 yt-dlp 子进程全程生效。"""
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
        log("%s ⚠️ CA 证书 ASCII 化失败（不影响 YouTube，可能影响 TikTok）: %s" % (MARK, e))


# ---------------------------------------------------------------- 链接识别

def detect_platform(url):
    u = (url or "").lower()
    if any(k in u for k in ("tiktok.com",)):
        return "tiktok"
    if any(k in u for k in ("youtube.com", "youtu.be", "youtube-nocookie.com")):
        return "youtube"
    return None


def ytdlp_cmd():
    # 本脚本被 kuaishou-env 解释器拉起（那里装了 yt_dlp），sys.executable 即可
    return [sys.executable, "-m", "yt_dlp"]


def find_ffmpeg():
    here_root = _TOOLBOX_ROOT
    cands = []
    try:
        import shutil
        p = shutil.which("ffmpeg")
        if p:
            cands.append(p)
    except Exception:
        pass
    for env in ("kuaishou-env", "douyin-venv", "live-recorder-env"):
        # imageio_ffmpeg 把 ffmpeg 放在 venv 的 bin/Scripts 下
        try:
            import imageio_ffmpeg
            cands.append(imageio_ffmpeg.get_ffmpeg_exe())
            break
        except Exception:
            pass
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


# ---------------------------------------------------------------- 代理探测

def _url_reachable(url, proxy=None, timeout=4):
    """探测 url（经 proxy 或直连）是否可达。HEAD 不被支持时按 GET 处理。"""
    import urllib.request
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    op = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": UA})
    try:
        r = op.open(req, timeout=timeout)
        return r.status < 500
    except Exception:
        return False


def pick_proxy(platform):
    """返回代理 URL 或 None（= 直连可用）。

    顺序：TY_PROXY 显式指定 → 直连探测 → 常见本地端口逐个试。
    ⚠️ 只在「直连不通」时才找代理：能直连的网络环境下强行走本地代理，
       反而会把流量交给一个不存在的出口。
    """
    probe = {"tiktok": "https://www.tiktok.com/robots.txt",
             "youtube": "https://www.youtube.com/robots.txt"}[platform]
    explicit = (os.environ.get("TY_PROXY") or "").strip()
    if explicit:
        if _url_reachable(probe, proxy=explicit, timeout=6):
            return explicit
        log("%s ⚠️ TY_PROXY=%r 经它访问不了目标站，继续自动探测" % (MARK, explicit))
    if _url_reachable(probe, timeout=4):
        log("%s 直连可用，不走代理" % MARK)
        return None
    for port in _PROXY_PORTS_HTTP:
        p = "http://127.0.0.1:%d" % port
        if _url_reachable(probe, proxy=p, timeout=3):
            log("%s 检测到本地代理 %s 可用" % (MARK, p))
            return p
    for port in _PROXY_PORTS_SOCKS:
        p = "socks5://127.0.0.1:%d" % port
        if _url_reachable(probe, proxy=p, timeout=3):
            log("%s 检测到本地 SOCKS 代理 %s 可用" % (MARK, p))
            return p
    return ""   # 空串 = 需要但没有


# ---------------------------------------------------------------- 下载

TMPL = "%(uploader)s/%(title).80s_[%(id)s].%(ext)s"


def ytdlp_common_args(outdir, proxy):
    args = ["--no-overwrites", "--newline", "--sleep-requests", "1",
            "--retries", "5", "--no-keep-video", "--merge-output-format", "mp4",
            "-P", "home:" + outdir,
            "-P", "temp:" + os.path.join(outdir, ".tmp"),
            "-o", TMPL]
    ff = find_ffmpeg()
    if ff:
        args += ["--ffmpeg-location", ff]
    if proxy:
        args += ["--proxy", proxy]
    ck = (os.environ.get("TY_COOKIES") or "").strip()
    if ck and os.path.exists(ck):
        args += ["--cookies", ck]
    return args


def run_ytdlp(args):
    cmd = ytdlp_cmd() + args
    return subprocess.run(cmd).returncode


def enumerate_entries(url, proxy, limit=0):
    """列表类链接（频道/主页/播放列表）先扁平枚举，返回作品 URL 列表。

    单视频链接也走这里 —— flat-playlist 对单视频返回 1 条的列表，
    统一路径省一套分支。枚举失败返回 None（调用方回退为直接下载整链）。
    """
    cmd = ytdlp_cmd() + ["--flat-playlist", "-J", "--no-warnings"]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd += [url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
    except Exception as e:
        log("%s 枚举超时/异常: %s" % (MARK, e))
        return None
    if r.returncode != 0:
        snippet = (r.stderr or r.stdout or "").strip().splitlines()
        snippet = snippet[-3:] if snippet else []
        for ln in snippet:
            log("%s %s" % (MARK, ln[:200]))
        return None
    try:
        d = json.loads(r.stdout)
    except Exception:
        return None
    if d.get("_type") == "playlist":
        entries = d.get("entries") or []
    else:
        entries = [d]
    out = []
    for e in entries:
        u = e.get("url") or e.get("webpage_url") or ""
        if not u and e.get("id"):
            # flat 条目常只有 id：按平台拼回标准作品页
            if detect_platform(url) == "youtube":
                u = "https://www.youtube.com/watch?v=" + str(e["id"])
        if u:
            out.append(u)
    if limit and len(out) > limit:
        out = out[:limit]
    return out


def download_one(url, outdir, proxy):
    return run_ytdlp(ytdlp_common_args(outdir, proxy) + [url])


def main():
    if len(sys.argv) < 2 or not (sys.argv[1] or "").startswith("http"):
        log("[错误] 未提供链接")
        return 2
    url = sys.argv[1].strip()
    mode = (sys.argv[2] if len(sys.argv) > 2 else "all").strip()
    limit = 0
    if mode.isdigit():
        limit = int(mode)

    _ensure_ascii_ca()

    platform = detect_platform(url)
    if not platform:
        log("[错误] 无法识别的链接（支持：tiktok.com / vm.tiktok.com / "
            "youtube.com / youtu.be）")
        return 2

    outdir = os.environ.get("TY_OUTDIR") or DEFAULT_OUTDIR
    outdir = os.path.join(outdir, platform)
    os.makedirs(os.path.join(outdir, ".tmp"), exist_ok=True)

    log("%s 平台: %s | 保存到: %s" % (MARK, platform, outdir))
    proxy = pick_proxy(platform)
    if proxy == "":
        log("%s ❌ 目标站直连不通，也没探测到可用的本地代理。" % MARK)
        log("%s    YouTube/TikTok 需要代理：请先打开你的代理软件（如 Clash Verge），"
            "再重新提交这个链接。也可以设置环境变量 TY_PROXY 指定代理地址。" % MARK)
        return 2

    is_list = bool(re.search(r"(channel/|@[^/]+|/playlist|list=|/video$|space|/videos?$)",
                             url)) and "watch?v=" not in url and "/video/" not in url
    entries = enumerate_entries(url, proxy, limit=limit) if is_list else [url]
    if not entries:
        # 枚举失败：单视频链接直接下；列表链接枚举不出就没法限量和报进度，也直接试
        log("%s 枚举不出清单，按单链接直接下载" % MARK)
        entries = [url]

    total = len(entries)
    log("%s 待下载 %d 个" % (MARK, total))
    ok = 0
    for i, u in enumerate(entries, 1):
        rc = download_one(u, outdir, proxy)
        if rc == 0:
            ok += 1
        else:
            log("%s 第 %d/%d 个失败（退出码 %d）：%s" % (MARK, i, total, rc, u[:90]))
            if platform == "tiktok":
                log("%s ⚠️ TikTok 拒绝了请求（常见原因：当前代理节点的出口 IP 被"
                    " TikTok 风控）。可尝试：① 在代理软件里换一个节点后重新提交；"
                    " ② 稍后再试；③ 有 TikTok 账号的话导出 cookies.txt 并设置"
                    " 环境变量 TY_COOKIES 指向它。" % MARK)
        log("%s 完成 %d/%d" % (MARK, i, total))
    log("[完成] %s" % ("下载成功" if ok == total else
                       ("部分成功 %d/%d" % (ok, total) if ok else "下载失败")))
    return 0 if ok == total else (1 if ok == 0 else 0)


if __name__ == "__main__":
    sys.exit(main())
