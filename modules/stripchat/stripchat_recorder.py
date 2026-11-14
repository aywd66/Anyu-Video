#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stripchat / Stripchat-VR 直播流录制器（直播页模块）
====================================================

OneRecord (libs/feiyu-extractor-1.0.1.jar) Stripchat 提取链路的独立复刻，
逆向细节见模块同目录 stripchat-README.md。

为接入工具箱桥接服务而加的参数：
  --out       DIR   录像根目录（默认 ./recordings）
  --stop-file PATH  出现该文件即优雅收尾（补 #EXT-X-ENDLIST、ffmpeg 合并）
  --log       PATH  运行日志（UTF-8，逐行 flush；桥接服务 tail 它给网页日志窗）

行为与逆向结果一致：
  房间 URL（5 种模式，含 vr.stripchat.com 与三个白标镜像）
  → cam API 取 streamName/直播状态
  → edge-hls.doppiocdn{com,org,live,net} 4 路 CDN 播放列表
  → m3u8 的 #EXT-X-MOUFLON:PSCH 行查密钥表得 pkey
  → mouflon 分片解密（反转→Base64 补位→SHA-256(pkey) 循环异或）
  → fMP4 分片拼 init 头落盘 → 本地 playlist.m3u8 → ffmpeg concat 合并

纯标准库；ffmpeg 在 PATH 时自动合并，否则保留分片 + m3u8。
日志全部使用简体中文；urllib/HTTP 的英文底层报错经 friendly_error() 翻译。
"""
import argparse
import base64
import hashlib
import json
import os
import random
import re
import shutil
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")
HOME_URL = "https://stripchat.com/"
CAM_API = "https://stripchat.com/api/front/v2/models/username/%s/cam?uniq=%s"
CDN_TLDS = ["doppiocdn.com", "doppiocdn.org", "doppiocdn.live", "doppiocdn.net"]

PUBLIC_STATUSES = {"public"}
OFFLINE_STATUSES = {"private", "groupShow", "virtualPrivate", "p2pVoice", "idle"}

# 主播状态 → 中文（页面与日志统一口径）
STATUS_ZH = {
    "public": "公开直播",
    "private": "私密秀",
    "groupShow": "团体秀",
    "virtualPrivate": "虚拟私密",
    "p2pVoice": "语音互动",
    "idle": "未开播",
    "unknown": "状态未知",
}

# psch -> pkey（提取自 OneRecord 的 stripchat_mouflon_keys.json）
MOUFLON_KEYS = {
    "Zokee2OhPh9kugh4": "Quean4cai9boJa5a",
    "Zeechoej4aleeshi": "ubahjae7goPoodi6",
    "Ook7quaiNgiyuhai": "EQueeGh2kaewa3ch",
    "Fq6m2TO2ZeBkRPm9": "xb6di1NF9EFXHUwb",
    "GrRncsoByZmsiT6L": "NigHYyOD9l4rvAEb",
    "1Dzcc6OjP73LKbtI": "Y64UVwX5RrIWnOLp",
    "N2oLovTIXb0o28Uj": "ABE7Sj8jh3oPM2ae",
    "NTK9aqcLmNFMWrpQ": "tOcYOap4Ty1l9Jzb",
    "7uUnbD0jMCB9GH32": "lzCQ6QBTnLpB0zMF",
    "Ohi7eTRBpkAuML0l": "kExe29N2sLFrHGqu",
    "OLzu7QlySkG2fVRn": "CsovScFH9VirSJ4Z",
}

URL_PATTERNS = [
    (re.compile(r"https?://vr\.stripchat\.com/cam/([\w.-]+)"), True),
    (re.compile(r"https?://(?:[\w-]+\.)?stripchat\.com/(?!cam/)([\w.-]+)"), False),
    (re.compile(r"https?://(?:[\w-]+\.)?stripol\.com/([\w.-]+)"), False),
    (re.compile(r"https?://(?:[\w-]+\.)?camwhorestv\.info/([\w.-]+)"), False),
    (re.compile(r"https?://(?:www\.)?xhamsterlive\.com/([\w.-]+)"), False),
]

SEGMENT_RE = re.compile(r"_([^_]+)_(\d+(?:_part\d+)?)\.mp4(?:[?#].*)?")
MAX_RETRIES = 3
MAX_PLAYLIST_REFRESH_FAILURES = 5


def friendly_error(e):
    """把 urllib / HTTP 的底层英文报错翻译成中文 + 给出下一步建议。

    只做字符串匹配，不改变异常本身；认不出的原样返回（保持可诊断性）。"""
    text = str(e)
    low = text.lower()
    if "418" in text:
        return ("被网站反爬机制拦截（HTTP 418）——IP 被识别为程序访问；"
                "请开启系统代理（Clash/V2Ray 系统模式）或更换代理节点后重试")
    if "403" in text:
        return "被服务器拒绝（HTTP 403）——可能需要更新浏览器 Cookie 或更换代理节点"
    if "404" in text:
        return "资源不存在（HTTP 404）——检查房间名 / 链接是否正确"
    if "timed out" in low or "timeout" in low:
        return "网络超时——请确认已开启系统代理（Clash/V2Ray 系统模式），或更换节点"
    if "connection refused" in low:
        return "连接被拒绝——目标不可达，检查网络与代理设置"
    if "getaddrinfo failed" in low or "name or service not known" in low:
        return "域名解析失败——检查网络 / DNS / 代理是否正常"
    if "ssl" in low and ("cert" in low or "verify" in low or "handshake" in low):
        return "SSL 连接失败——通常是代理软件拦截了连接，尝试更换节点或关闭 SSL 检查类功能"
    if ("connection reset" in low or "connection aborted" in low
            or "forcibly closed" in low):
        return "连接被重置——网络链路不稳定或被中断，可重试或更换代理节点"
    m = re.search(r"HTTP Error (\d+)", text)
    if m:
        return "服务器返回错误（HTTP %s）" % m.group(1)
    return text


class NeedCookies(Exception):
    """418 被拦：需要真实浏览器 Cookie（由桥接唤起 Chrome 抓取后重试）。"""


class Log:
    """stdout + 可选文件双写，逐行 flush（桥接服务按行 tail）。"""

    def __init__(self, path=None):
        self._f = open(path, "a", encoding="utf-8", buffering=1) if path else None

    def __call__(self, msg):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
        print(line, flush=True)
        if self._f:
            self._f.write(line + "\n")

    def close(self):
        if self._f:
            self._f.close()


# ---------------------------------------------------------------- mouflon（字节码级复刻）

def mouflon_decrypt_segment_url(url, pdkey):
    m = SEGMENT_RE.search(url)
    if not m:
        raise ValueError("无法解析加密分片地址：%s" % url)
    enc = m.group(1)
    rev = enc[::-1]
    while len(rev) % 4 != 0:
        rev += "="
    data = base64.b64decode(rev)
    key = hashlib.sha256(pdkey.encode("utf-8")).digest()
    out = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return url.replace(enc, out.decode("utf-8"))


def extract_sequence(url):
    m = SEGMENT_RE.search(url)
    if not m:
        raise ValueError("分片地址中不含序号：%s" % url)
    return int(re.split(r"_part", m.group(2))[0])


# ---------------------------------------------------------------- HTTP

class Client:
    """带浏览器伪装的 HTTP 客户端。

    cookie_header / ua 来自浏览器抓取（grab_cookies.py）的成果注入；
    没有 CookieProcessor —— 手动 Cookie 头与 cookiejar 同时开会互相覆盖。
    xhr=True 用 XHR 风格请求头（调 /api/ 必需，网页头会被 418）。"""

    def __init__(self, ua=None, cookie_header=None):
        self.ua = ua or USER_AGENT
        self.cookie_header = cookie_header
        self.opener = urllib.request.build_opener()

    def get(self, url, referer=HOME_URL, binary=False, timeout=20, xhr=False):
        h = {"User-Agent": self.ua,
             "Accept-Language": "en,en-US;q=0.9",
             "Accept-Encoding": "identity",
             "Origin": "https://stripchat.com"}
        if xhr:
            h.update({"Accept": "application/json, text/plain, */*",
                      "Sec-Fetch-Dest": "empty",
                      "Sec-Fetch-Mode": "cors",
                      "Sec-Fetch-Site": "same-origin",
                      "X-Requested-With": "XMLHttpRequest"})
        else:
            h.update({"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                      "Sec-Fetch-Dest": "document",
                      "Sec-Fetch-Mode": "navigate",
                      "Sec-Fetch-Site": "none",
                      "Upgrade-Insecure-Requests": "1"})
        if referer:
            h["Referer"] = referer
        if self.cookie_header:
            h["Cookie"] = self.cookie_header
        req = urllib.request.Request(url, headers=h)
        with self.opener.open(req, timeout=timeout) as resp:
            body = resp.read()
            return (resp.status, body if binary else body.decode("utf-8", "replace"))


def load_cookies_file(path):
    """读取浏览器抓取的 Cookie JSON，返回 (ua, cookie头)。失败抛异常。"""
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    cookies = d.get("cookies") or {}
    if not cookies:
        raise ValueError("Cookie 文件里没有 Cookie：%s" % path)
    header = "; ".join("%s=%s" % (k, v) for k, v in cookies.items())
    return d.get("ua"), header


# ---------------------------------------------------------------- 观众模式（浏览器传输）

# 观众模式使用的真实浏览器：用户指定的 Chrome
CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


class BrowserSession:
    """观众模式：用专属持久化 Chrome 配置直接"观看"房间，旁听页面自己的流量。

    背景（2026-10 实测）：stripchat 已部署 TLS 指纹墙——Python/Node 的握手
    一律被断（SSL EOF/超时），API 层另有 418 反爬，无头浏览器也会被识别。
    伪装路线全部失败后采用的根本方案：让真实有头浏览器当观众，页面自己的
    JS 会加载播放器并请求 m3u8；我们只监听网络事件拿播放列表，分片也在
    页面内 fetch —— 全程没有一个"伪造"请求，鉴权由站点自己完成。

    配置目录持久化（browser-profile/）：Cookie、localStorage 与设备指纹
    像真人浏览器一样逐步积累；首次若弹出人机验证，手动点一次即可，
    之后一般不再出现。"""

    def __init__(self, room_url, profile_dir, log):
        self.room_url = room_url
        self.log = log
        self.master = None     # (url, text) 主播放列表
        self.media = None      # (url, text) 最新媒体播放列表（播放器自动刷新）
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        # Chrome 用显式路径（用户指定）；路径不存在时退回 playwright 自动探测
        launch_kw = dict(
            user_data_dir=str(profile_dir), headless=False,
            viewport={"width": 880, "height": 600}, locale="en-US",
            args=["--window-size=900,620", "--no-first-run", "--mute-audio"])
        if os.path.exists(CHROME_EXE):
            launch_kw["executable_path"] = CHROME_EXE
        else:
            launch_kw["channel"] = "chrome"
        self.ctx = self._pw.chromium.launch_persistent_context(**launch_kw)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.on("response", self._on_response)

    def _on_response(self, resp):
        try:
            url = resp.url
            if ".m3u8" not in url:
                return
            text = resp.text()
            if "#EXT-X-STREAM-INF" in text:
                if self.master is None:
                    self.master = (url, text)
                    self.log("已捕获主播放列表（来自页面播放器）")
            else:
                self.media = (url, text)
        except Exception:
            pass

    def open_room(self, timeout=60):
        """打开房间页并等待播放器请求出播放列表（最长 timeout 秒）。"""
        self.page.goto(self.room_url, wait_until="domcontentloaded",
                       timeout=timeout * 1000)
        self.log("房间页已打开；浏览器窗口即录制会话，录制期间请勿关闭")
        self.log("（首次使用如弹出人机验证，手动通过一次即可，之后会记住）")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.media or self.master:
                return True
            time.sleep(1)
        return False

    def fetch_bytes(self, url, timeout=30):
        """在页面内 fetch 二进制（浏览器网络栈，Cookie/指纹全自动）。"""
        b64 = self.page.evaluate(
            """async (u) => {
                const r = await fetch(u);
                if (!r.ok) return null;
                const b = await r.arrayBuffer();
                let s = ''; const c = new Uint8Array(b);
                for (let i = 0; i < c.length; i += 0x8000)
                    s += String.fromCharCode.apply(null, c.subarray(i, i + 0x8000));
                return btoa(s);
            }""", url)
        if b64 is None:
            raise RuntimeError("页内请求失败（HTTP 非 200）")
        return base64.b64decode(b64)

    def close(self):
        for closer in (lambda: self.ctx.close(), lambda: self._pw.stop()):
            try:
                closer()
            except Exception:
                pass


def record_via_browser(username, out_root, keys, log, stop_file, room_url, profile_dir):
    """观众模式录制主循环：消费浏览器旁听到的媒体播放列表快照。"""
    session_dir = out_root / ("%s_%s" % (username, time.strftime("%Y%m%d_%H%M%S")))
    session_dir.mkdir(parents=True, exist_ok=True)
    log("保存目录：%s" % session_dir)

    sess = BrowserSession(room_url, profile_dir, log)
    downloaded = set()
    init_bytes = None
    m3u8 = session_dir / "playlist.m3u8"
    m3u8.write_text("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-MEDIA-SEQUENCE:0\n",
                    encoding="utf-8")
    last_harvest = ""          # 上次消费的播放列表内容（去重）
    idle_since = None          # 播放列表多久没更新（下播探测）
    try:
        if not sess.open_room():
            raise RuntimeError("打开房间页后 60 秒内没等到播放列表——"
                               "主播可能未开播，或弹出的人机验证未通过")
        log("播放列表已就绪，开始录制")
        while not stop_requested(stop_file):
            snap = sess.media
            if snap is None:
                time.sleep(2)
                continue
            url, text = snap
            if text != last_harvest:
                last_harvest = text
                idle_since = None
                init_url, pkey, segments = parse_media_playlist(text, url, keys)
                if init_url and init_bytes is None:
                    try:
                        init_bytes = sess.fetch_bytes(init_url)
                        log("已缓存初始分片（%d 字节）" % len(init_bytes))
                    except Exception as e:
                        log("[警告] 初始分片获取失败：%s" % friendly_error(e))
                fresh = [s for s in segments if s.sequence not in downloaded]
                for seg in fresh:
                    if stop_requested(stop_file):
                        break
                    data = None
                    for attempt in range(MAX_RETRIES):
                        try:
                            data = sess.fetch_bytes(seg.url)
                            break
                        except Exception as e:
                            if attempt == MAX_RETRIES - 1:
                                log("[错误] 分片 %d 获取失败：%s" %
                                    (seg.sequence, friendly_error(e)))
                            else:
                                time.sleep(1)
                    if data is None:
                        continue
                    downloaded.add(seg.sequence)
                    if init_bytes:
                        data = init_bytes + data
                    (session_dir / ("%s_segment%06d.ts" % (username, seg.sequence))
                     ).write_bytes(data)
                    with m3u8.open("a", encoding="utf-8") as f:
                        f.write("#EXTINF:0,\n%s_segment%06d.ts\n" % (username, seg.sequence))
                    log("已保存分片 %06d（%d 字节）" % (seg.sequence, len(data)))
            else:
                # 播放列表没有新内容：累计空闲，超过 10 分钟视为下播
                if idle_since is None:
                    idle_since = time.time()
                elif time.time() - idle_since > 600:
                    log("播放列表超过 10 分钟没有更新，判断直播已结束")
                    break
                time.sleep(3)
    finally:
        if stop_requested(stop_file):
            log("收到停止信号，正在收尾合并…")
        try:
            with m3u8.open("a", encoding="utf-8") as f:
                f.write("#EXT-X-ENDLIST\n")
        except OSError:
            pass
        merged = merge_session(session_dir, log, len(downloaded))
        if downloaded:
            log("录制结束：共 %d 个分片，成品：%s" % (len(downloaded), merged or session_dir))
        else:
            log("会话结束：未录到分片（主播未开播 / 网络被拒 / 人机验证未通过）")
        sess.close()


# ---------------------------------------------------------------- m3u8 解析

class Segment:
    def __init__(self, url, sequence):
        self.url, self.sequence = url, sequence


def parse_best_stream(master_text):
    best_bw, best_url, pending = -1, None, None
    for line in master_text.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            m = re.search(r"BANDWIDTH=(\d+)", line)
            pending = int(m.group(1)) if m else -1
        elif line and not line.startswith("#") and pending is not None:
            if pending > best_bw:
                best_bw, best_url = pending, line
            pending = None
    return best_url


def parse_media_playlist(text, base_url, keys):
    psch = pkey = None
    init_url = None
    segments = []
    prefix = base_url.rsplit("/", 1)[0] + "/" if "://" in base_url else ""
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if line.startswith("#EXT-X-MOUFLON:PSCH:"):
            psch = line[len("#EXT-X-MOUFLON:PSCH:"):]
        elif line.startswith("#EXT-X-MOUFLON:URI:"):
            try:
                tok = line[len("#EXT-X-MOUFLON:URI:"):]
                rev = tok[::-1]
                while len(rev) % 4:
                    rev += "="
                init_url = urljoin(prefix, base64.b64decode(rev).decode("utf-8"))
            except Exception:
                pass
        elif line.startswith("#EXT-X-MAP:URI"):
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                init_url = urljoin(prefix, m.group(1))
        elif line and not line.startswith("#"):
            url = urljoin(prefix, line)
            if psch is not None and pkey is None:
                pkey = keys.get(psch)
            if pkey and SEGMENT_RE.search(url):
                url = mouflon_decrypt_segment_url(url, pkey)
            segments.append(Segment(url, extract_sequence(url)))
    return init_url, pkey, segments


# ---------------------------------------------------------------- API

def uniq():
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choice(chars) for _ in range(36))


def get_stream_info(client, username, is_vr, log):
    referer = ("https://vr.stripchat.com/cam/%s" % username if is_vr
               else "https://stripchat.com/%s" % username)
    try:
        status, body = client.get(CAM_API % (username, uniq()),
                                  referer=referer, xhr=True)
    except urllib.error.HTTPError as e:
        if e.code == 418:
            raise NeedCookies("被反爬拦截（HTTP 418）：需要真实浏览器 Cookie")
        raise RuntimeError("查询主播信息失败：%s" % friendly_error(e))
    except Exception as e:
        raise RuntimeError("查询主播信息失败：%s" % friendly_error(e))
    if status != 200:
        raise RuntimeError("查询主播信息的接口返回了 HTTP %d" % status)
    try:
        js = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError("接口返回的不是有效数据（可能被 Cloudflare 人机验证拦截，"
                           "请开系统代理后重试）")
    user = js.get("user") or {}
    if not user:
        raise RuntimeError("未找到该主播：%s（检查房间名是否正确）" % username)
    st = user.get("status", "unknown")
    recordable = (st in PUBLIC_STATUSES
                  and user.get("isCamAvailable", False)
                  and user.get("isCamActive", False))
    log("主播状态：%s · 可录制：%s · 观众数：%s" %
        (STATUS_ZH.get(st, st), "是" if recordable else "否",
         user.get("viewersCount", 0)))
    return {"stream_name": user.get("username"), "status": st,
            "recordable": recordable, "vr": is_vr}


def build_playlist_urls(stream_name, is_vr):
    vr = "vr" if is_vr else ""
    return ["https://edge-hls.%s/hls/%s%s/master/%s%s_auto.m3u8" %
            (tld, stream_name, vr, stream_name, vr) for tld in CDN_TLDS]


def append_psch_pkey(url, text, keys):
    for line in text.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if line.startswith("#EXT-X-MOUFLON:PSCH:"):
            psch = line[len("#EXT-X-MOUFLON:PSCH:"):]
            pkey = keys.get(psch)
            if pkey:
                sep = "&" if "?" in url else "?"
                return "%s%spsch=%s&pkey=%s" % (url, sep, psch, pkey)
            break
    return url


# ---------------------------------------------------------------- 录制

def stop_requested(stop_file):
    return bool(stop_file) and Path(stop_file).exists()


def record(client, username, is_vr, out_root, keys, log, stop_file):
    info = get_stream_info(client, username, is_vr, log)
    if not info["recordable"]:
        st = info["status"]
        hint = "主播未开播" if st in ("idle", "unknown") else "主播在%s，不对外公开" % STATUS_ZH.get(st, st)
        raise RuntimeError("当前无法录制（%s）——等公开直播后再来" % hint)

    referer = ("https://vr.stripchat.com/cam/%s" % username if is_vr
               else "https://stripchat.com/%s" % username)
    session_dir = out_root / ("%s_%s" % (username, time.strftime("%Y%m%d_%H%M%S")))
    session_dir.mkdir(parents=True, exist_ok=True)
    log("保存目录：%s" % session_dir)

    downloaded = set()
    init_bytes = None
    m3u8 = session_dir / "playlist.m3u8"
    m3u8.write_text("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-MEDIA-SEQUENCE:0\n",
                    encoding="utf-8")

    failures = 0
    try:
        while not stop_requested(stop_file):
            try:
                master_url, master_text = None, None
                for u in build_playlist_urls(info["stream_name"], is_vr):
                    try:
                        st, tx = client.get(u, referer=referer)
                        if st == 200 and "#EXTM3U" in tx:
                            master_url, master_text = u, tx
                            break
                    except Exception:
                        continue
                if not master_url:
                    raise RuntimeError("4 个 CDN 域名全部获取播放列表失败")
                master_url = append_psch_pkey(master_url, master_text, keys)
                variant = parse_best_stream(master_text)
                target = urljoin(master_url, variant) if variant else master_url
                st, text = client.get(target, referer=referer)
                if st != 200:
                    raise RuntimeError("获取清晰度列表失败（HTTP %d）" % st)
                failures = 0
            except Exception as e:
                failures += 1
                log("[警告] 刷新播放列表失败（第 %d/%d 次）：%s" %
                    (failures, MAX_PLAYLIST_REFRESH_FAILURES,
                     friendly_error(e)))
                if failures >= MAX_PLAYLIST_REFRESH_FAILURES:
                    break
                time.sleep(5)
                continue

            init_url, pkey, segments = parse_media_playlist(text, target, keys)
            if init_url and init_bytes is None:
                try:
                    _, init_bytes = client.get(init_url, referer=referer, binary=True)
                    log("已缓存初始分片（%d 字节）" % len(init_bytes))
                except Exception as e:
                    log("[警告] 初始分片下载失败：%s" % friendly_error(e))

            fresh = [s for s in segments if s.sequence not in downloaded]
            if not fresh:
                time.sleep(5)
                try:
                    probe = get_stream_info(client, username, is_vr, log) \
                        if not stop_requested(stop_file) else None
                    if probe and not probe["recordable"]:
                        log("直播已结束或转为不公开（主播状态：%s），停止录制" %
                            STATUS_ZH.get(probe["status"], probe["status"]))
                        break
                except Exception:
                    pass
                continue

            for seg in fresh:
                if stop_requested(stop_file):
                    break
                data = None
                for attempt in range(MAX_RETRIES):
                    try:
                        _, data = client.get(seg.url, referer=referer, binary=True)
                        break
                    except Exception as e:
                        if attempt == MAX_RETRIES - 1:
                            log("[错误] 分片 %d 下载失败：%s" %
                                (seg.sequence, friendly_error(e)))
                        else:
                            time.sleep(1)
                if data is None:
                    continue
                downloaded.add(seg.sequence)
                if init_bytes:
                    data = init_bytes + data
                (session_dir / ("%s_segment%06d.ts" % (username, seg.sequence))
                 ).write_bytes(data)
                with m3u8.open("a", encoding="utf-8") as f:
                    f.write("#EXTINF:0,\n%s_segment%06d.ts\n" % (username, seg.sequence))
                log("已保存分片 %06d（%d 字节）" % (seg.sequence, len(data)))
    finally:
        if stop_requested(stop_file):
            log("收到停止信号，正在收尾合并…")
        with m3u8.open("a", encoding="utf-8") as f:
            f.write("#EXT-X-ENDLIST\n")
        merged = merge_session(session_dir, log, len(downloaded))
        log("录制结束：共 %d 个分片，成品：%s" % (len(downloaded), merged or session_dir))


def merge_session(session_dir, log, downloaded_count=0):
    if downloaded_count == 0:
        log("未录到任何分片，跳过合并")
        return None
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        log("[警告] 未找到 ffmpeg，保留分片与 playlist.m3u8（播放器可直接打开 m3u8 观看）")
        return None
    out = session_dir / (session_dir.name + ".ts")
    try:
        r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat",
                            "-safe", "0", "-i", str(session_dir / "playlist.m3u8"),
                            "-c", "copy", str(out)],
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        log("[警告] ffmpeg 合并失败：%s" % friendly_error(e))
        return None
    if r.returncode != 0:
        log("[警告] ffmpeg 合并失败（退出码 %d），保留分片" % r.returncode)
        return None
    for p in session_dir.glob("*_segment*.ts"):
        p.unlink()
    return out


# ---------------------------------------------------------------- 入口

def main():
    ap = argparse.ArgumentParser(description="Stripchat 直播流录制器（直播页模块）")
    ap.add_argument("url", help="房间 URL（stripchat / vr.stripchat / xhamsterlive / stripol / camwhorestv）")
    ap.add_argument("--out", default="recordings", help="录像根目录")
    ap.add_argument("--stop-file", default=None, help="该文件出现时优雅收尾")
    ap.add_argument("--log", default=None, help="运行日志文件（UTF-8 追加）")
    ap.add_argument("--cookies-file", default=None,
                    help="浏览器抓取的 Cookie JSON（grab_cookies.py 产物）")
    args = ap.parse_args()

    log = Log(args.log)
    url = args.url.rstrip("/")
    match = None
    for pat, is_vr in URL_PATTERNS:
        m = pat.match(url)
        if m:
            match = (m.group(1), is_vr)
            break
    if not match:
        log("[错误] 不支持的房间链接：%s" % url)
        return 2
    username, is_vr = match
    log("房间：%s（%s）" % (username, "VR 直播" if is_vr else "普通直播"))

    # 会话登记：pid + 元数据落在 stop-file 同目录。桥接重启后凭这两个文件
    # 「认领」本进程（否则监护丢失，网页会误显示「未在录制」却停不掉它）。
    state_dir = Path(args.stop_file).parent if args.stop_file else None
    pidfile = state_dir / "recorder.pid" if state_dir else None
    session = state_dir / "session.json" if state_dir else None
    if pidfile:
        try:
            pidfile.write_text(str(os.getpid()), encoding="ascii")
            session.write_text(json.dumps(
                {"url": url, "user": username, "vr": is_vr, "pid": os.getpid(),
                 "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            pass

    rc = 0
    # 传输模式：装了 playwright 就走观众模式（真实浏览器，对抗指纹墙）；
    # 否则降级 urllib（在当前反爬强度下大概率失败，但保留可用性）。
    try:
        import playwright  # noqa: F401
        have_pw = True
    except ImportError:
        have_pw = False
    try:
        if have_pw:
            log("传输模式：观众模式（真实 Chrome 浏览器）")
            profile = (state_dir / "browser-profile") if state_dir else Path("browser-profile")
            record_via_browser(username, Path(args.out), MOUFLON_KEYS, log,
                               args.stop_file, url, profile)
        else:
            log("传输模式：直连（未检测到 playwright，当前反爬下可能失败）")
            client = None
            if args.cookies_file:
                try:
                    ua, cookie_header = load_cookies_file(args.cookies_file)
                    client = Client(ua=ua, cookie_header=cookie_header)
                    log("已加载浏览器 Cookie（%d 条）" % cookie_header.count("="))
                except Exception as e:
                    log("[警告] 读取 Cookie 文件失败，改用普通访问：%s" % friendly_error(e))
            if client is None:
                client = Client()
                try:
                    client.get(HOME_URL)
                    log("主页访问成功，已获得基础 Cookie")
                except urllib.error.HTTPError as e:
                    if e.code == 418:
                        log("[警告] 主页即被反爬拦截（HTTP 418）")
                    else:
                        log("[警告] 访问主页失败（不影响继续尝试）：%s" % friendly_error(e))
                except Exception as e:
                    log("[警告] 访问主页失败（不影响继续尝试）：%s" % friendly_error(e))
            record(client, username, is_vr, Path(args.out), MOUFLON_KEYS, log, args.stop_file)
    except KeyboardInterrupt:
        log("已手动中断（Ctrl+C），正在收尾…")
    except NeedCookies as e:
        # 专用退出码 3：桥接看到它会自动抓浏览器 Cookie 并重试一次
        log("[需要浏览器Cookie] %s —— 桥接将自动唤起 Chrome 获取" % e)
        rc = 3
    except Exception as e:
        log("[错误] %s" % (friendly_error(e) if len(str(e)) < 200 else str(e)))
        rc = 1
    finally:
        for f in (pidfile, session):
            if f:
                try:
                    f.unlink()
                except OSError:
                    pass
        log.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
