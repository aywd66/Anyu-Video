#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""哔哩哔哩无 GUI 下载器（与 kuaishou_headless.py 同一套任务对接约定）。

支持三类需求，URL 自动识别（无需用户选类型）：
  1. 单作品    bilibili.com/video/BVxxxx / av123 / b23.tv 短链
                → yt-dlp 下载最高画质 mp4；mode=audio 时只提取音频 mp3
  2. 主页批量  space.bilibili.com/<uid>（可带 /uploads/video）
                → BilibiliSpaceVideo 播放列表翻页下载；mode 为纯数字时限量前 N 个
  3. 直播录制  live.bilibili.com/<房间号>
                → 公开 API 拿流地址 + ffmpeg -c copy 录制，直到直播结束或任务被取消

用法：python bilibili_headless.py <url> [mode]
  mode: all（默认）/ 1 / 纯数字 N / audio / live
环境变量：
  BILI_OUTDIR  产物根目录（默认 data/bilibili）；中间流固定落在 <outdir>/.tmp，
               不会出现在成品目录里。
  BILI_COOKIE  可选，登录 Cookie 头（SESSDATA=...; buvid3=...）。
               1080P 以上清晰度、大会员画质需要对应登录态；不带则按游客清晰度。
               设置页「扫码获取」登录后会写入 modules/bilibili/.cookies.json。
  BILI_FFMPEG  可选，ffmpeg 路径（合并视频轨+音频轨、抽音轨、直播录制都要它）。
               缺省依次找 PATH、工具箱各 venv 里的 imageio_ffmpeg。
               ⚠️ 缺 ffmpeg 时 B 站的 DASH 流合不了，只会留下「无声视频 + 音频」
               两个文件 —— 这就是「有的重复、有的没声音」的根源，脚本会明确提示
               并退回单文件格式。

退出码：0 成功 / 2 未登录或参数无效 / 3 解析不出作品 / 1 下载失败。
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLBOX_ROOT = os.path.dirname(os.path.dirname(_HERE))          # 视频下载工具箱/
DEFAULT_OUTDIR = os.path.join(_TOOLBOX_ROOT, "data", "bilibili")

# 与抖音/快手一致的进度标记，任务中心 task_progress 按 `[bili] 完成 i/N` 解析
MARK = "[bili]"


def log(msg):
    print(msg, flush=True)


def cookie_header():
    env = (os.environ.get("BILI_COOKIE") or "").strip()
    if env:
        return env
    # 设置页保存的 Cookie（modules/bilibili/.cookies.json）
    try:
        with open(os.path.join(_HERE, ".cookies.json"), "r", encoding="utf-8") as f:
            return str((json.load(f) or {}).get("cookie") or "").strip()
    except Exception:
        return ""


def http_headers():
    h = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
    ck = cookie_header()
    if ck:
        h["Cookie"] = ck
    return h


# ---------------------------------------------------------------- 链接识别

def resolve_short_link(url):
    """b23.tv 短链跟随跳转，返回落地 URL（失败原样返回）。"""
    if "b23.tv" not in url:
        return url
    try:
        r = requests.get(url, headers=http_headers(), timeout=20, allow_redirects=True)
        return r.url or url
    except Exception as e:
        log("[!] 短链解析失败（按原链接继续）: %s" % e)
        return url


def classify(url):
    """返回 (kind, key)。kind ∈ video/space/live/unknown。"""
    u = url or ""
    m = re.search(r"live\.bilibili\.com/(?:h5/)?(\d+)", u)
    if m:
        return "live", m.group(1)
    m = re.search(r"space\.bilibili\.com/(\d+)", u)
    if m:
        return "space", m.group(1)
    if re.search(r"bilibili\.com/video/(BV[0-9A-Za-z]+)", u) or \
            re.search(r"bilibili\.com/(?:s/)?video/av(\d+)", u) or \
            re.search(r"\bav(\d+)\b", u) or \
            re.search(r"(BV[0-9A-Za-z]{8,12})\b", u):
        return "video", u
    return "unknown", ""


# ---------------------------------------------------------------- 外部工具（ffmpeg）

_FFMPEG = {"done": False, "path": ""}


def find_ffmpeg():
    """找 ffmpeg：BILI_FFMPEG > PATH > 工具箱自带 venv 里 imageio_ffmpeg 的二进制。

    为什么必须显式找：B 站给的是 DASH —— 视频轨和音频轨是**两个文件**，只有
    ffmpeg 能把它们合成一个带声音的 mp4。找不到 ffmpeg 时 yt-dlp 不会报错退出，
    而是把「无声视频」和「音频」分别留在目录里 —— 用户看到的就是「重复 + 没声音」。
    """
    if _FFMPEG["done"]:
        return _FFMPEG["path"]
    cands = []
    env = (os.environ.get("BILI_FFMPEG") or "").strip()
    if env:
        cands.append(env)
    found = shutil.which("ffmpeg")
    if found:
        cands.append(found)
    for venv in ("douyin-venv", "kuaishou-env", "live-recorder-env"):
        py = os.path.join(_TOOLBOX_ROOT, "runtime", venv, "Scripts", "python.exe")
        if not os.path.exists(py):
            continue
        try:
            out = subprocess.run(
                [py, "-c", "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"],
                capture_output=True, text=True, timeout=30)
            lines = (out.stdout or "").strip().splitlines()
            if out.returncode == 0 and lines:
                cands.append(lines[-1].strip())
        except Exception:
            pass
    for c in cands:
        if c and os.path.exists(c):
            _FFMPEG.update(done=True, path=c)
            return c
    _FFMPEG.update(done=True, path="")
    return ""


def find_ffprobe():
    """ffprobe 与 ffmpeg 一般同目录；imageio_ffmpeg 只带 ffmpeg，所以允许为空。"""
    ff = find_ffmpeg()
    if ff:
        exe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
        cand = os.path.join(os.path.dirname(ff), exe)
        if os.path.exists(cand):
            return cand
    return shutil.which("ffprobe") or ""


def has_audio(path):
    """成品里是否真的有音轨。查不出来时返回 True（宁可漏报，不要误报）。"""
    try:
        probe = find_ffprobe()
        if probe:
            out = subprocess.run(
                [probe, "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
                capture_output=True, text=True, timeout=60)
            return "audio" in (out.stdout or "")
        ff = find_ffmpeg()
        if ff:
            # 没有 ffprobe：读 ffmpeg 的流信息（不带输出文件必然返回码 1，只看文本）
            out = subprocess.run([ff, "-hide_banner", "-i", path],
                                 capture_output=True, text=True, timeout=60,
                                 errors="replace")
            return "Audio:" in (out.stderr or "")
    except Exception:
        pass
    return True


# ---------------------------------------------------------------- yt-dlp

# 输出命名：**不要随手改**。改模板 = 已下载的文件不再被 --no-overwrites 认出，
# 整个主页会被重新下一遍，看起来就像「下载重复」。
TMPL_SINGLE = "%(uploader)s/%(title)s_[%(id)s].%(ext)s"
TMPL_SPACE = "%(uploader)s/%(uploader)s 的投稿_[%(id)s].%(ext)s"
TMPL_AUDIO = "音频/%(uploader)s/%(title)s_[%(id)s].%(ext)s"


def ytdlp_cmd():
    # 本脚本被 kuaishou-env 解释器拉起（那里装了 yt_dlp），sys.executable 即可
    return [sys.executable, "-m", "yt_dlp"]


def tmp_dir(outdir):
    """中间流暂存目录（在输出根下，同一分区，合并后 rename 进成品目录很快）。"""
    d = os.path.join(outdir, ".tmp")
    os.makedirs(d, exist_ok=True)
    return d


def ytdlp_common_args(outdir=None):
    args = ["--no-overwrites", "--no-playlist", "--newline",
            "--sleep-requests", "1", "--retries", "5", "--no-keep-video"]
    ff = find_ffmpeg()
    if ff:
        # 显式指定，不靠 PATH 撞运气：子进程的 PATH 未必和交互式 shell 一样。
        args += ["--ffmpeg-location", ff]
    if outdir:
        # ⚠️ 中间流（视频-only 的 *.f30080.mp4、音频 *.f30280.m4a）必须落在临时
        #    目录。放进输出目录时，只要「合并后删除中间文件」这一步失败 ——
        #    被安全删除钩子拦下、进程被杀、文件被占用、磁盘只读 —— 用户就会
        #    同时看到 `xxx.mp4` 和 `xxx.f30080.mp4`：前者正常，后者没有音轨。
        #    于是「有的重复、有的没声音」两个症状一起出现（2026-10-18 事故）。
        #    ⚠️ -o 必须用**相对模板**，绝对路径会让 yt-dlp 忽略 --paths。
        args += ["-P", "home:" + outdir, "-P", "temp:" + tmp_dir(outdir)]
    ck = cookie_header()
    if ck:
        args += ["--add-headers", "Cookie: " + ck]
    return args


def run_ytdlp(args, outdir=None):
    cmd = ytdlp_cmd() + ytdlp_common_args(outdir) + args
    log("[bili] 运行 yt-dlp ...")
    return subprocess.run(cmd).returncode


def _report_path(outdir):
    return os.path.join(tmp_dir(outdir), "final-%d.txt" % time.time_ns())


def _read_report(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return [ln.strip() for ln in f if ln.strip()]
    except Exception:
        return []


def download_one(url, outdir, audio=False, tmpl=None):
    """下载单个作品，返回 (ok, [成品绝对路径])。

    audio=True 只抽音频 mp3；否则 mp4（视频轨 + 音频轨合并）。
    合并出来没有音轨时自动退回「单文件渐进式格式」重下一次（有声音，画质低一档）。
    """
    os.makedirs(outdir, exist_ok=True)
    tmpl = tmpl or (TMPL_AUDIO if audio else TMPL_SINGLE)
    ff = find_ffmpeg()
    if audio:
        base = ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    elif ff:
        base = ["-f", "bv*+ba/b", "--merge-output-format", "mp4"]
    else:
        # 没有 ffmpeg 就别选「视频轨+音频轨」：合不了，只会留下无声视频。
        base = ["-f", "b[ext=mp4]/b"]
        log("[!] 未找到 ffmpeg，改用单文件格式（有声音，画质可能低一档）")
        log("[!] 想要更高画质：装 ffmpeg 并放进 PATH，或用 BILI_FFMPEG 指向它")

    def _run(extra=None):
        report = _report_path(outdir)
        args = list(base) + list(extra or []) + [
            "-o", tmpl, "--print-to-file", "after_move:%(filepath)s", report, url]
        rc = run_ytdlp(args, outdir)
        files = _read_report(report)
        try:
            os.remove(report)
        except Exception:
            pass
        return rc, files

    rc, files = _run()
    if rc == 0 and not audio and ff:
        silent = [f for f in files if not has_audio(f)]
        if silent:
            log("[!] 成品没有音轨：%s" % os.path.basename(silent[0]))
            log("[!] 改用单文件格式重下一次（画质低一档，但保证有声音）")
            for f in silent:
                try:
                    os.remove(f)
                except Exception:
                    pass
            rc, files = _run(["-f", "b[ext=mp4]/b", "--force-overwrites"])
    return rc == 0, files


# 中间流文件名：<成品名>.f<format_id>.<ext>（如 xxx_[BV1xx].f30080.mp4）
_INTERMEDIATE_RE = re.compile(r"^(.*)\.f[0-9A-Za-z_-]+\.[A-Za-z0-9]{2,5}$")


def cleanup_intermediates(outdir, dry_run=False):
    """清掉「已有成品」的中间流文件（*.f30080.mp4 / *.f30280.m4a / *.part）。

    只动这些明确是 yt-dlp 中间产物的文件，且必须旁边有同名成品；成品永不删。
    历史遗留（旧版本把中间流写进了输出目录）也靠它收拾。返回移除个数。
    """
    if not os.path.isdir(outdir):
        return 0
    removed = 0
    for root, _dirs, files in os.walk(outdir):
        if os.path.basename(root) == ".tmp":
            continue
        for f in files:
            p = os.path.join(root, f)
            target = None
            if f.endswith(".part"):
                # 断点续传的临时文件。两种情况下它是死文件：
                #   1) 同名成品已经下完（xxx.mp4.part 旁边有 xxx.mp4）；
                #   2) 它是某个中间流的残片（xxx.f30077.mp4.part），
                #      而合成好的 xxx.mp4 已经存在 —— 这条流再也用不上了。
                inner = p[:-5]
                if os.path.exists(inner):
                    target = p
                else:
                    m = _INTERMEDIATE_RE.match(os.path.basename(inner))
                    if m:
                        stem = os.path.join(root, m.group(1))
                        if any(os.path.exists(stem + ext) for ext in
                               (".mp4", ".mkv", ".flv", ".webm", ".mp3", ".m4a")):
                            target = p
            else:
                m = _INTERMEDIATE_RE.match(f)
                if m:
                    stem = os.path.join(root, m.group(1))
                    if any(os.path.exists(stem + ext) for ext in
                           (".mp4", ".mkv", ".flv", ".webm", ".mp3", ".m4a")):
                        target = p
            if not target:
                continue
            if dry_run:
                log("[清理] 待移除: %s" % f)
                removed += 1
                continue
            try:
                os.remove(target)
                removed += 1
            except Exception as e:
                log("[!] 清理失败 %s: %s" % (f, str(e)[:60]))
    # .tmp 里只剩空目录时顺手收掉（中间流本来就该是临时的）
    tmp_root = os.path.join(outdir, ".tmp")
    if os.path.isdir(tmp_root) and not dry_run:
        for root, _dirs, _files in os.walk(tmp_root, topdown=False):
            if os.path.abspath(root) == os.path.abspath(tmp_root):
                continue
            try:
                if not os.listdir(root):
                    os.rmdir(root)
            except Exception:
                pass
    return removed


def download_single(url, outdir, audio=False):
    """单作品：视频 mp4（最高画质）或音频 mp3。"""
    ok, _files = download_one(url, outdir, audio=audio)
    n = cleanup_intermediates(outdir)
    if n:
        log("[清理] 顺手清掉 %d 个历史中间流文件（无音轨，不是成品）" % n)
    log("[完成] %s" % ("下载成功" if ok else "下载失败"))
    return 0 if ok else 1


def download_space(uid, outdir, limit):
    """主页批量：浏览器通道收集投稿列表 → 逐条交 yt-dlp。

    为什么不用 yt-dlp 的 BilibiliSpaceVideo 播放列表：B 站对匿名 API 风控升级后，
    空间接口匿名直连会 412/`-352 风控校验失败`（实测 2026-10-18），而**浏览器里
    页面自己发的签名请求不受影响**。所以与抖音/快手同一姿势：playwright 打开
    空间页，滚动加载时拦截页面自己的 `/x/space/wbi/arc/search` 响应收集 BV 号，
    再逐条走「单视频页」通道下载——单视频页 yt-dlp 实测可用。
    """
    bvids = collect_space_bvids(uid, limit)
    if not bvids:
        log("[!] 未能从主页收集到投稿（页面加载失败或该 UP 主没有作品）")
        return 3
    total = len(bvids)
    log("[bili] 主页批量: uid=%s 收集到 %d 个投稿%s"
        % (uid, total, ("（限量前 %d）" % limit) if limit else ""))
    outtmpl = TMPL_SPACE
    os.makedirs(outdir, exist_ok=True)
    done, failed = 0, []
    for i, bvid in enumerate(bvids, start=1):
        url = "https://www.bilibili.com/video/" + bvid
        ok, _files = download_one(url, outdir, tmpl=outtmpl)
        if ok:
            done += 1
        else:
            failed.append(bvid)
        log("[bili] 完成 %d/%d%s" % (i, total, "" if ok else "（失败）"))
        time.sleep(1.5)  # 轻微节流，别把风控招来
    # B 站 CDN 偶发读超时（实测一次 40 条里能挂十几个），重试一轮大多能过。
    if failed:
        log("[bili] %d 个失败，重试一轮 ..." % len(failed))
        retry = list(failed)
        failed = []
        for j, bvid in enumerate(retry, start=1):
            ok, _files = download_one("https://www.bilibili.com/video/" + bvid,
                                      outdir, tmpl=outtmpl)
            if ok:
                done += 1
            else:
                failed.append(bvid)
            log("[bili] 重试 %d/%d%s" % (j, len(retry), "" if ok else "（仍失败）"))
            time.sleep(1.5)
    n = cleanup_intermediates(outdir)
    if n:
        log("[清理] 顺手清掉 %d 个历史中间流文件（无音轨，不是成品）" % n)
    log("[完成] 批量结束：成功 %d / 失败 %d / 共 %d" % (done, len(failed), total))
    return 0 if not failed else 1


def collect_space_bvids(uid, limit=0, max_pages_clicks=120):
    """打开空间投稿页滚动收集 BV 号（拦截页面自己的签名接口响应）。

    ⚠️ 必须**有头**（headless=False）：2026-10-18 实测，无头浏览器的空间列表
    接口会被风控拦（列表不渲染/412），有头放行 —— 与抖音浏览器兜底同一结论。
    """
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        log("[!] playwright 不可用: %s" % e)
        return []
    bvids, seen = [], set()

    async def _run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False, args=["--disable-blink-features=AutomationControlled"])
            ctx = await browser.new_context(
                user_agent=UA, locale="zh-CN", viewport={"width": 1400, "height": 900})
            ck = cookie_header()
            if ck:
                cookies = []
                for pair in ck.split(";"):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        cookies.append({"name": k.strip(), "value": v.strip(),
                                        "domain": ".bilibili.com", "path": "/"})
                if cookies:
                    await ctx.add_cookies(cookies)
            page = await ctx.new_page()

            async def on_resp(r):
                if "/x/space/wbi/arc/search" not in (r.url or ""):
                    return
                try:
                    d = await r.json()
                    for v in (((d.get("data") or {}).get("list") or {}).get("vlist") or []):
                        b = v.get("bvid")
                        if b and b not in seen:
                            seen.add(b)
                            bvids.append(b)
                except Exception:
                    pass

            page.on("response", lambda r: asyncio.create_task(on_resp(r)))
            try:
                await page.goto("https://space.bilibili.com/%s/video" % uid,
                                wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(5000)
                # 风控门禁：列表接口迟迟不来 + 页面出现验证字样时，保持有头窗口
                # 请用户人工完成验证（滑块/点选），完成风控即放行 —— 与抖音
                # 浏览器兜底的 _wait_for_manual_verification 同一策略。
                if not bvids:
                    try:
                        gate = await page.evaluate(
                            "() => /验证|安全|滑动|captcha/i.test(document.title + document.body.innerText.slice(0, 600))"
                        )
                    except Exception:
                        gate = False
                    if gate:
                        log("[!] 检测到哔哩哔哩安全验证：请在**弹出的浏览器窗口**里完成验证，"
                            "完成后程序会自动继续（最多等 180 秒）")
                        for _ in range(60):
                            if bvids or page.is_closed():
                                break
                            await page.wait_for_timeout(3000)
                # ── 分页：空间投稿页是「下一页」按钮分页，不是无限滚动 ──
                # 2026-10-18 实测：单页固定 40 条，滚动/scrollIntoView 都不会触发
                # 下一页（某作者 54 个视频只扫到 40 就是这个原因），必须循环点
                # `button.vui_pagenation--btn-side`（下一页）直到禁用（末页）。
                # 点击无新增时先检查是否撞上验证门禁（ headed 窗口里人工完成后自动续）。
                async def _gate_wait():
                    """返回 True=出现了门禁并等待处理（含 180s 人工验证窗口）。"""
                    try:
                        gate = await page.evaluate(
                            "() => /验证|安全|滑动|captcha|检测中/i.test(document.title + document.body.innerText.slice(0, 600))"
                        )
                    except Exception:
                        gate = False
                    if not gate:
                        return False
                    log("[!] 翻页时撞上哔哩哔哩安全验证：请在**弹出的浏览器窗口**里完成验证，"
                        "完成后自动继续（最多等 180 秒）")
                    for _ in range(60):
                        if page.is_closed():
                            break
                        await page.wait_for_timeout(3000)
                        # 页面恢复的标志：BJ 号继续增长或按钮恢复可点
                        try:
                            if await page.locator(
                                "button.vui_pagenation--btn-side", has_text="下一页"
                            ).count():
                                return True
                        except Exception:
                            pass
                    return True

                stale = 0
                for _ in range(max_pages_clicks):
                    if limit and len(bvids) >= limit:
                        break
                    if page.is_closed():
                        break
                    btn = page.locator("button.vui_pagenation--btn-side", has_text="下一页")
                    try:
                        if await btn.count() == 0:
                            log("[i] 没有下一页按钮（单页足够）")
                            break
                        if await btn.is_disabled():
                            log("[i] 已到末页")
                            break
                        before = len(bvids)
                        await btn.click()
                        await page.wait_for_timeout(2500)
                        if len(bvids) > before:
                            stale = 0
                            continue
                        # 点击无新增：可能是门禁，也可能真到底了
                        if await _gate_wait():
                            stale = 0
                            continue
                        stale += 1
                        if stale >= 3:
                            log("[i] 连续 %d 次点击无新增且无验证，停止翻页" % stale)
                            break
                    except Exception as e:
                        log("[i] 翻页中断: %s" % str(e)[:60])
                        break
            finally:
                await browser.close()

    asyncio.run(_run())
    if not bvids:
        # 首次空手：等 6s 重试一轮（风控是间歇性的，首次撞上别直接放弃）
        log("[i] 首轮未收集到投稿，6s 后重试一次 ...")
        time.sleep(6)
        try:
            asyncio.run(_run())
        except Exception as e:
            log("[!] 重试失败: %s" % e)
    if limit and len(bvids) > limit:
        bvids = bvids[:limit]
    return bvids


# ---------------------------------------------------------------- 图文（动态图片集）

def collect_space_draws(uid, limit=0, max_loads=80):
    """收集 UP 主的图文动态（opus/draw 类型条目）。

    图文发在**动态流**里：打开 space.bilibili.com/<uid>/dynamic，拦截页面自己的
    `web-dynamic/v1/feed/space` 响应收集条目，点「加载更多」翻页。
    门禁处理与视频批量一致：检测到安全验证就保持有头窗口等人工完成。
    返回 [(id_str, 标题), ...]
    """
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        log("[!] playwright 不可用: %s" % e)
        return []
    items, seen = [], set()

    async def _run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False, args=["--disable-blink-features=AutomationControlled"])
            ctx = await browser.new_context(
                user_agent=UA, locale="zh-CN", viewport={"width": 1400, "height": 900})
            ck = cookie_header()
            if ck:
                cookies = []
                for pair in ck.split(";"):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        cookies.append({"name": k.strip(), "value": v.strip(),
                                        "domain": ".bilibili.com", "path": "/"})
                if cookies:
                    await ctx.add_cookies(cookies)
            page = await ctx.new_page()

            async def on_resp(r):
                if "/web-dynamic/v1/feed/space" not in (r.url or ""):
                    return
                try:
                    d = await r.json()
                    for it in ((d.get("data") or {}).get("items") or []):
                        iid = str(it.get("id_str") or "")
                        if not iid or iid in seen:
                            continue
                        md = (it.get("modules") or {}).get("module_dynamic") or {}
                        major = md.get("major") or {}
                        if not (major.get("draw") or major.get("opus")):
                            continue  # 只要图文/图片类
                        seen.add(iid)
                        # 图片 URL 直接从条目 JSON 里全量提取（draw.items[].src /
                        # opus.pictures 等形态兼容），排除头像 —— feed 响应自带
                        # 图片直链，无需再访问会被风控的 opus 内容页。
                        blob = json.dumps(it, ensure_ascii=False)
                        pics = []
                        for u in re.findall(
                            r'https://\w+\.hdslb\.com/bfs/[^\s"\'\\<>]+?\.(?:jpg|jpeg|png|webp|gif)',
                            blob.replace("\\u002F", "/").replace("\\/", "/"),
                        ):
                            base = u.split("@")[0]
                            if "/bfs/face/" in base or "emoji" in base:
                                continue
                            if base not in pics:
                                pics.append(base)
                        title = ((md.get("desc") or {}).get("text") or "").strip()
                        items.append({"id": iid, "title": title[:40], "pics": pics})
                except Exception:
                    pass

            page.on("response", lambda r: asyncio.create_task(on_resp(r)))
            try:
                await page.goto("https://space.bilibili.com/%s/dynamic" % uid,
                                wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(6000)
                if not items:
                    try:
                        gate = await page.evaluate(
                            "() => /验证|安全|滑动|captcha|检测中/i.test(document.title + document.body.innerText.slice(0, 600))"
                        )
                    except Exception:
                        gate = False
                    if gate:
                        log("[!] 检测到哔哩哔哩安全验证：请在**弹出的浏览器窗口**里完成验证，"
                            "完成后程序会自动继续（最多等 180 秒）")
                        for _ in range(60):
                            if items or page.is_closed():
                                break
                            await page.wait_for_timeout(3000)
                for _ in range(max_loads):
                    if limit and len(items) >= limit:
                        break
                    if page.is_closed():
                        break
                    btn = page.locator("button", has_text="加载更多")
                    try:
                        if await btn.count() == 0:
                            break
                        before = len(items)
                        await btn.first.click()
                        await page.wait_for_timeout(2500)
                        if len(items) == before:
                            break  # 没有新条目 = 到底了
                    except Exception:
                        break
            finally:
                await browser.close()

    asyncio.run(_run())
    if not items:
        log("[i] 首轮未收集到图文，6s 后重试一次 ...")
        time.sleep(6)
        try:
            asyncio.run(_run())
        except Exception as e:
            log("[!] 重试失败: %s" % e)
    if limit and len(items) > limit:
        items = items[:limit]
    return items


def download_draws(uid, outdir, limit):
    """主页图文批量：动态 feed 收集（自带图片直链）→ 直接下载图片。"""
    items = collect_space_draws(uid, limit)
    if not items:
        log("[!] 未能从动态收集到图文（该 UP 主可能没有图文，或被风控拦截）")
        return 3
    total = len(items)
    log("[bili] 图文批量: uid=%s 收集到 %d 条" % (uid, total))
    done = failed = 0
    for i, item in enumerate(items, start=1):
        iid, title = item["id"], item["title"]
        try:
            ok, n = download_pics(item, outdir)
            if ok:
                done += 1
                log("[bili] 完成 %d/%d（%s，%d/%d 图）" % (i, total, title or iid, ok, n))
            else:
                failed += 1
                log("[bili] 完成 %d/%d（%s，未提取到图片）" % (i, total, title or iid))
        except Exception as e:
            failed += 1
            log("[bili] 完成 %d/%d（失败：%s）" % (i, total, str(e)[:60]))
        time.sleep(1.0)
    log("[完成] 图文批量结束：成功 %d / 失败 %d / 共 %d" % (done, failed, total))
    return 0 if failed == 0 else 1


def download_pics(item, outdir):
    """把一条图文的图片列表下载到 图文/<标题>_[<id>]/，附带正文文本。"""
    iid, title, pics = item["id"], item["title"], item["pics"]
    item_dir = os.path.join(outdir, "图文", "%s_[%s]" % (sanitize(title)[:50] or "图文", iid))
    os.makedirs(item_dir, exist_ok=True)
    text = item.get("text") or ""
    if text:
        try:
            with open(os.path.join(item_dir, "正文.txt"), "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass
    ok = 0
    for i, u in enumerate(pics, start=1):
        try:
            ir = requests.get(u, headers=http_headers(), timeout=30)
            ir.raise_for_status()
            ext = os.path.splitext(u.split("?")[0])[1] or ".jpg"
            with open(os.path.join(item_dir, "图%02d%s" % (i, ext)), "wb") as f:
                f.write(ir.content)
            ok += 1
        except Exception as e:
            log("[!] 图 %d 下载失败: %s" % (i, str(e)[:50]))
    return ok, len(pics)


# ---------------------------------------------------------------- 直播录制
# （ffmpeg 的查找统一走文件上方的 find_ffmpeg：合并、抽音轨、录制共用一份逻辑）

def get_live_info(room_id):
    """房间信息：live_status（1=直播中）、标题、画质流地址。"""
    info = requests.get(
        "https://api.live.bilibili.com/room/v1/Room/get_info?room_id=" + room_id,
        headers=http_headers(), timeout=20).json()
    if info.get("code") != 0:
        raise RuntimeError("房间信息获取失败: %s" % info.get("message"))
    title = (info.get("title") or ("room" + room_id)).strip()
    status = int(info.get("live_status") or 0)
    play = requests.get(
        "https://api.live.bilibili.com/room/v1/Room/playUrl?cid=%s&qn=10000&platform=web"
        % room_id, headers=http_headers(), timeout=20).json()
    stream_url = ""
    if play.get("code") == 0:
        durl = ((play.get("data") or {}).get("durl") or [])
        if durl:
            stream_url = durl[0].get("url") or ""
    return status, title, stream_url


def sanitize(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name or "").strip() or "untitled"


def record_live(room_id, outdir):
    """直播录制：ffmpeg -c copy 写 FLV，直到直播结束 / ffmpeg 退出。"""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log("[!] 找不到 ffmpeg（直播录制必需）。可设置 BILI_FFMPEG 指定路径。")
        return 2
    status, title, stream_url = get_live_info(room_id)
    if status != 1:
        log("[!] 房间 %s 当前未开播（live_status=%s），退出" % (room_id, status))
        return 3
    if not stream_url:
        # 公开 API 拿不到流（加密/改版）→ 回落 yt-dlp 的 BiliLive 提取器
        log("[i] playUrl 接口未返回流地址，回落 yt-dlp 直播提取器")
        outtmpl = os.path.join(outdir, "直播", "%(title)s_%%(id)s.%%(ext)s")
        rc = run_ytdlp(["--yes-playlist", "-o", outtmpl,
                        "https://live.bilibili.com/" + room_id])
        log("[完成] %s" % ("录制结束" if rc == 0 else "录制失败(码 %d)" % rc))
        return 0 if rc == 0 else 1

    live_dir = os.path.join(outdir, "直播")
    os.makedirs(live_dir, exist_ok=True)
    out_path = os.path.join(live_dir, "%s_[%s].flv" % (
        sanitize(title)[:60], time.strftime("%Y%m%d_%H%M%S")))
    log("[bili-live] 开始录制: %s" % out_path)
    log("[bili-live] 录制中，直播结束或任务取消时自动收尾 ...")
    try:
        rc = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-stats",
                             "-i", stream_url, "-c", "copy", out_path]).returncode
    except KeyboardInterrupt:
        rc = 0
    size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    log("[bili] 完成 1/1（%d 字节）" % size)
    log("[完成] %s" % ("录制结束，文件 %s" % out_path if rc == 0 else "录制异常退出(码 %d)" % rc))
    return 0 if rc == 0 else 1


# ---------------------------------------------------------------- 入口

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    url = sys.argv[1].strip()
    mode = (sys.argv[2].strip() if len(sys.argv) > 2 else "all") or "all"
    outdir = (os.environ.get("BILI_OUTDIR") or DEFAULT_OUTDIR).strip()
    os.makedirs(outdir, exist_ok=True)

    # 维护入口：清掉历史遗留的中间流（视频-only 的 *.f30080.mp4 等）。
    # 加 --dry-run 只列出不删。
    if url.lower() in ("--cleanup", "--clean"):
        dry = "--dry-run" in sys.argv
        n = cleanup_intermediates(outdir, dry_run=dry)
        log("[完成] %s %d 个中间流文件（目录：%s）"
            % ("待清理" if dry else "已清理", n, outdir))
        return 0

    landed = resolve_short_link(url)
    kind, key = classify(landed)
    log("[bili] 落地页: %s" % landed[:100])
    log("[bili] 类型: %s" % kind)

    if kind == "video":
        # 音频模式只在单作品语义下生效；主页链接 + audio 不做批量提取
        return download_single(landed, outdir, audio=(mode.lower() == "audio"))

    if kind == "space":
        limit = 0
        if mode.isdigit():
            limit = max(int(mode), 0)
        elif mode == "1":
            limit = 1
        if mode.lower() == "posts":
            return download_draws(key, outdir, limit)
        return download_space(key, outdir, limit)

    if kind == "live" or mode.lower() == "live":
        if kind != "live":
            log("[!] mode=live 但链接不是直播间地址")
            return 2
        return record_live(key, outdir)

    log("[!] 无法识别的链接（支持：视频页 / av·BV 号 / space.bilibili.com/<uid> / live.bilibili.com/<房间号> / b23.tv 短链）")
    return 3


if __name__ == "__main__":
    sys.exit(main())
