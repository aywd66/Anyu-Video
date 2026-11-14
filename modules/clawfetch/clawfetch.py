#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clawfetch -- 一条命令的直链下载器(Python 3.8+,仅标准库)

特性
  - 下载前先探测链接可用性(HEAD,被拒时自动降级 GET Range)
  - HTML 中转页自动转直链(发现返回网页而非文件时,自动尝试同域 /stream/ 端点)
  - 断点续传(HTTP Range),网络中断/超时按指数退避自动重试
  - 实时进度:百分比、速度、剩余时间;服务器无响应时有等待提示
  - SHA-256 完整性校验 + 大小与服务器报告值核对
  - 每次下载尝试(含失败)追加写入 download_ledger.jsonl 台账,可事后核查
  - 失败清理采用"隔离而非删除"设计:未完成文件一律改名移入 .clawfetch_trash/,
    程序全程不删除任何用户文件;仅程序自身的 .part.meta 元数据在收尾后移入隔离区
  - 续传带"同文件"校验:ETag/Last-Modified 绑定(.part.meta)+ Content-Range 起点核对,
    服务器文件被更换时自动丢弃旧进度重下,绝不拼接出"大小正确但内容错位"的文件
  - 绝不覆盖已存在的完好文件;同名自动另存编号副本
  - 跨平台:Windows / macOS / Linux,无需安装任何第三方依赖

用法
  python clawfetch.py <下载链接> [-o 保存路径] [-n 重试次数] [-t 超时秒数]
  python clawfetch.py --help     查看全部参数
"""

import argparse
import copy
import errno
import hashlib
import http.client
import json
import os
import random
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from datetime import datetime
from urllib.parse import unquote, urlparse

VERSION = "1.4.2"
PROGRAM = "clawfetch"
LEDGER_NAME = "download_ledger.jsonl"
TRASH_DIR = ".clawfetch_trash"
CHUNK = 64 * 1024
MULTI_MIN = 16 * 1024 * 1024  # 超过此大小且服务器支持 Range 时启用多线程分段
STALL_ABORT_SEC = float(os.environ.get("CLAWFETCH_STALL_ABORT", "120"))  # 尝试内全局无进度多久后提前中止
# 自适应并发的两个判据窗口。取名是因为它们是**实测结论**而不是拍脑袋的常数：
# 2026-09-26 一次真实故障（818.91MB 走 trycloudflare 临时隧道）里，8 个连接
# 有 3 个整整 30 秒一个字节都没收到，而同一次运行里有 72 次 HTTP 502。
STARVE_SEC = float(os.environ.get("CLAWFETCH_STARVE_SEC", "30"))       # 新并发布局给多久来自证；也是"零进度"的判定时长
SCALE_WINDOW_SEC = float(os.environ.get("CLAWFETCH_SCALE_WINDOW", "15"))  # 吞吐采样窗口
# 预检探测的超时上限。探测只是"这链接活着吗"，不该跟着 --timeout 一起放大：
# probe() 最多试 2 次，所以 -t 60 会让**还没开始下载**就先等 2 分钟。
# （2026-09-26 实测：-t 60 时探测阶段耗掉约 100 秒。批量模式早有 10s 上限，
#   单链接模式漏了，这里补齐。）
PROBE_TIMEOUT_MAX = float(os.environ.get("CLAWFETCH_PROBE_TIMEOUT", "10"))

# 并发上限。与 plan_segments 里的 32 是同一个数，提到这里是为了让"升档阶梯"
# 和"分段规划"用同一个上限，不再各写一个字面量。
MAX_CONN = int(os.environ.get("CLAWFETCH_MAX_CONN", "32"))
# 5xx 熔断阈值：单次尝试内 HTTP 5xx 累计到这个数，就判定"源站扛不住这么多连接"，
# 当场锁死为单连接并不再升档。取值依据：健康源站偶发 5xx 是零星的，
# 而 2026-09-27 那次真实故障在 83 秒里刷了 122 次（32 连接），中间毫无过渡。
HTTP5XX_TRIP = int(os.environ.get("CLAWFETCH_5XX_TRIP", "5"))

# ---- 退出码 ----
EXIT_OK = 0
EXIT_ARGS = 2          # 参数错误
EXIT_URL = 3           # 无效链接
EXIT_NETWORK = 4       # 连接失败 / 隧道不可达 / DNS 失败
EXIT_HTTP = 5          # HTTP 错误(404/403/5xx 等)
EXIT_TIMEOUT = 6       # 超时 / 服务器无响应(重试耗尽)
EXIT_INTEGRITY = 7     # 完整性校验失败
EXIT_IO = 8            # 磁盘 / 本地读写错误
EXIT_ABORT = 130       # 用户中断(Ctrl+C)

UA = "%s/%s (Python-urllib; cross-platform CLI downloader)" % (PROGRAM, VERSION)
BASE_HEADERS = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}

RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


class FetchError(Exception):
    """带退出码/建议/重试标记的下载错误。"""

    def __init__(self, code, message, hint=None, retryable=False, http_status=None):
        super().__init__(message)
        self.code = code
        self.hint = hint
        self.retryable = retryable
        self.http_status = http_status
        self.quarantined = []
        self.attempts_used = 0
        self.last_part_size = 0


# ---------------------------------------------------------------- 控制台输出

_out_lock = threading.RLock()
_console_len = 0


def _is_tty():
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def cwrite(text, newline=True):
    """写一行;newline=False 时用 \\r 原地刷新进度(终端模式)。"""
    global _console_len
    with _out_lock:
        tty = _is_tty()
        if newline:
            if tty and _console_len:
                sys.stdout.write("\r" + " " * _console_len + "\r")
            sys.stdout.write(text + "\n")
            _console_len = 0
        else:
            if not tty:
                sys.stdout.write(text + "\n")
            else:
                pad = " " * max(0, _console_len - len(text))
                sys.stdout.write("\r" + text + pad)
                _console_len = len(text)
        sys.stdout.flush()


def clear_progress_line():
    global _console_len
    with _out_lock:
        if _is_tty() and _console_len:
            sys.stdout.write("\r" + " " * _console_len + "\r")
        _console_len = 0
        sys.stdout.flush()


def human_bytes(n):
    if n is None:
        return "未知大小"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.2f %s" % (n, unit))
        n /= 1024.0
    return "%.2f TB" % n


def human_secs(s):
    if s is None or s < 0 or s != s:  # NaN 防御
        return "--:--"
    s = int(s)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    if h:
        return "%02d:%02d:%02d" % (h, m, sec)
    return "%02d:%02d" % (m, sec)


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 进度显示

class Progress(object):
    """流式进度:百分比 + 速度(4 秒滑动窗口)+ 剩余时间。"""

    def __init__(self, base, total, is_tty, quiet, tag=""):
        self.base = base            # 续传起点字节数
        self.total = total          # 最终文件总大小(未知为 None)
        self.n = 0                  # 本次尝试新增字节
        self.is_tty = is_tty
        self.quiet = quiet
        self.tag = tag or ""        # 显示标签(如 "8 线程")
        self.samples = deque()      # (monotonic_t, 当前总字节)
        self.t0 = time.monotonic()
        self.last_paint = 0.0
        self._lock = threading.RLock()

    def cur(self):
        return self.base + self.n

    def speed(self):
        if len(self.samples) < 2:
            return 0.0
        dt = self.samples[-1][0] - self.samples[0][0]
        db = self.samples[-1][1] - self.samples[0][1]
        if dt <= 0:
            return 0.0
        return max(0.0, db / dt)

    def tick(self, got):
        with self._lock:
            self.n += got
            now = time.monotonic()
            self.samples.append((now, self.cur()))
            while len(self.samples) > 2 and now - self.samples[0][0] > 4.0:
                self.samples.popleft()
        self.paint()

    def paint(self, force=False):
        if self.quiet:
            return
        now = time.monotonic()
        interval = 0.15 if self.is_tty else 5.0
        if not force and now - self.last_paint < interval:
            return
        self.last_paint = now
        cur, sp = self.cur(), self.speed()
        if self.total:
            pct = min(cur / float(self.total) * 100.0, 100.0)
            filled = int(24 * min(cur / float(self.total), 1.0))
            bar = "#" * filled + "-" * (24 - filled)
            eta = human_secs((self.total - cur) / sp) if sp > 1 else "--:--"
            line = "[%s] %6.2f%%  %s/%s  %s/s  剩余 %s" % (
                bar, pct, human_bytes(cur), human_bytes(self.total),
                human_bytes(sp), eta)
        else:
            line = "已下载 %s  %s/s  已用时 %s" % (
                human_bytes(cur), human_bytes(sp), human_secs(now - self.t0))
        if self.tag:
            line += "  (%s)" % self.tag
        if self.base:
            line += "  (续传自 %s)" % human_bytes(self.base)
        cwrite(line, newline=not self.is_tty)


class StallWatchdog(threading.Thread):
    """长时间无数据时给出等待提示(主线程阻塞在 read 时也能提示)。"""

    def __init__(self, timeout, is_tty, quiet):
        threading.Thread.__init__(self)
        self.daemon = True
        self.timeout = timeout
        self.is_tty = is_tty
        self.quiet = quiet
        self.stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._silent_since = time.monotonic()

    def poke(self):
        with self._lock:
            self._silent_since = time.monotonic()

    def run(self):
        while not self.stop_evt.wait(1.0):
            if self.quiet:
                continue
            with self._lock:
                silent = time.monotonic() - self._silent_since
            if silent < 6.0:
                continue
            msg = "... 服务器已 %ds 无数据,等待中(超过 %ds 将判定超时并自动断点续传重试)" % (
                int(silent), self.timeout)
            if self.is_tty:
                cwrite(msg, newline=False)
            else:
                if int(silent) % 30 == 0:
                    cwrite(msg, newline=True)


# ---------------------------------------------------------------- 工具函数

def normalize_url(raw):
    url = (raw or "").strip()
    notice = None
    if url and "://" not in url:
        url = "https://" + url
        notice = "链接缺少协议前缀,已按 https:// 处理"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise FetchError(EXIT_URL, "不是有效的 http(s) 链接:%r" % raw,
                         hint="链接应以 http:// 或 https:// 开头")
    return url, notice


def header_int(hdrs, key):
    try:
        v = hdrs.get(key)
        return int(str(v).strip()) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_content_range_total(value):
    if not value:
        return None
    m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", value.strip(), re.I)
    if not m:
        return None
    return None if m.group(3) == "*" else int(m.group(3))


def fix_mojibake(raw):
    """HTTP 头按 latin-1 解码导致的中文乱码修复。"""
    if not raw:
        return raw
    try:
        return raw.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return raw


def filename_from_cd(cd):
    """从 Content-Disposition 解析文件名,优先 filename*(UTF-8),兼容 filename。"""
    if not cd:
        return None
    m = re.search(r"filename\*\s*=\s*([^;]+)", cd, re.I)
    if m:
        v = m.group(1).strip().strip('"')
        try:
            if "''" in v:
                charset, _, enc_part = v.partition("''")
                return fix_mojibake(unquote(enc_part, encoding=(charset or "utf-8"),
                                            errors="replace"))
            return fix_mojibake(unquote(v))
        except Exception:
            pass
    m = re.search(r'filename\s*=\s*"((?:[^"\\]|\\.)*)"', cd, re.I)
    if m:
        return fix_mojibake(re.sub(r"\\(.)", r"\1", m.group(1)))
    m = re.search(r"filename\s*=\s*([^;]+)", cd, re.I)
    if m:
        return fix_mojibake(m.group(1).strip().strip('"'))
    return None


def sanitize_filename(name):
    """生成跨平台安全的文件名(防路径穿越、Windows 保留名/非法字符)。"""
    if not name:
        return None
    name = name.strip().strip('"').strip("'")
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', "_", name)
    name = name.strip(" .")
    if not name:
        return None
    stem = name.split(".")[0].rstrip(" .").upper()
    if stem in RESERVED_NAMES:
        name = "_" + name
    if len(name) > 150:
        root, ext = os.path.splitext(name)
        if len(ext) > 40:
            ext = ext[:40]
        name = root[: max(1, 150 - len(ext))] + ext
    return name


def url_last_segment(url):
    try:
        path = unquote(urlparse(url).path or "")
        seg = path.rstrip("/").rsplit("/", 1)[-1]
        return seg or None
    except Exception:
        return None


def parse_output_arg(output):
    """-o 参数 → (目录, 指定文件名 or None)。
    规则:已存在的目录或分隔符结尾 → 目录;不存在且无扩展名 → 按新目录处理;
    其余 → 完整文件路径。"""
    if output is None or output == "":
        return os.getcwd(), None
    o = os.path.expanduser(output)
    if o.endswith(("\\", "/")) or os.path.isdir(o):
        return o, None
    a = os.path.abspath(o)
    if not os.path.exists(a) and not os.path.splitext(a)[1]:
        return a, None  # 新目录(无扩展名),由 run() 创建
    d, b = os.path.split(a)
    return (d or os.getcwd()), (b or None)


def pick_target(outdir, override, cd_name, url_name, overwrite):
    """确定最终保存路径;默认不覆盖同名文件(自动编号副本);--overwrite 时沿用原名、
    完成下载收尾时把旧文件移入隔离区(不删除)。"""
    if override:
        override = sanitize_filename(override)
    name = override or sanitize_filename(cd_name) or sanitize_filename(url_name) \
        or ("download-%s.bin" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    target = os.path.join(outdir, name)
    note = None
    if os.path.exists(target):
        if overwrite:
            note = "已存在同名文件:--overwrite 生效,下载完成收尾时旧文件将移入 %s/(不删除)" % TRASH_DIR
        else:
            stem, ext = os.path.splitext(name)
            k = 1
            while os.path.exists(os.path.join(outdir, "%s (%d)%s" % (stem, k, ext))):
                k += 1
            new_name = "%s (%d)%s" % (stem, k, ext)
            note = "目标文件已存在,为不覆盖已有文件,改存为 %s" % new_name
            target = os.path.join(outdir, new_name)
    return target, note


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def backoff_delay(base, failed_attempt):
    """指数退避 + 抖动:base*2^(n-1),封顶 60s。failed_attempt 从 1 计。"""
    return min(base * (2 ** (failed_attempt - 1)), 60.0) \
        + random.uniform(0, min(0.5 * base, 2.0))


# ---------------------------------------------------------------- 错误归一化

def normalize_error(e):
    """把任意异常翻译成 FetchError(退出码 + 建议 + 是否可重试)。"""
    if isinstance(e, FetchError):
        return e
    if isinstance(e, urllib.error.HTTPError):
        c = e.code
        if c in (408, 429) or c >= 500:
            return FetchError(EXIT_HTTP, "服务器错误(HTTP %d)" % c,
                              hint="服务器暂时不可用,将自动重试",
                              retryable=True, http_status=c)
        if c == 416:
            return FetchError(EXIT_HTTP, "断点区间无效(HTTP 416)",
                              hint="本地断点与服务器文件不匹配(文件可能已被更换);本次进度将移入隔离区,重跑将重新下载",
                              retryable=False, http_status=c)
        if c in (401, 403):
            return FetchError(EXIT_HTTP, "服务器拒绝访问(HTTP %d)" % c,
                              hint="链接可能需要登录或访问权限;如是他人分享的链接,请向分享者确认",
                              retryable=False, http_status=c)
        if c in (404, 410):
            return FetchError(EXIT_HTTP, "链接不存在(HTTP %d)" % c,
                              hint="文件可能已被删除,或临时隧道已下线/过期--请向分享者确认链接是否仍有效,必要时索取新链接",
                              retryable=False, http_status=c)
        return FetchError(EXIT_HTTP, "HTTP %d 错误" % c,
                          hint="服务器返回异常状态", retryable=False, http_status=c)
    if isinstance(e, urllib.error.URLError):
        reason = e.reason
        if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason).lower():
            return FetchError(EXIT_TIMEOUT, "连接超时",
                              hint="网络不通或服务器无响应;可尝试增大 --timeout",
                              retryable=True)
        s = str(reason).lower()
        if ("getaddrinfo" in s or "name or service not known" in s
                or "nodename nor servname" in s or "no address associated" in s
                or "temporary failure in name resolution" in s):
            host = ""
            try:
                host = urlparse(e.url or "").hostname or ""
            except Exception:
                pass
            return FetchError(EXIT_NETWORK, "无法解析域名%s" % ("(%s)" % host if host else ""),
                              hint="域名不存在,或 trycloudflare 临时隧道已失效/过期--请确认链接,必要时向分享者索取新链接",
                              retryable=True)
        if "refused" in s:
            return FetchError(EXIT_NETWORK, "连接被拒绝",
                              hint="目标服务可能已下线(临时隧道可能已过期),请确认链接或索取新链接",
                              retryable=True)
        if "cert" in s or "ssl" in s:
            return FetchError(EXIT_NETWORK, "TLS/SSL 证书错误:%s" % reason,
                              hint="请检查本机系统时间是否正确,或网络中是否有代理/防火墙拦截",
                              retryable=False)
        return FetchError(EXIT_NETWORK, "网络错误:%s" % reason,
                          hint="请检查本机网络连接后重跑", retryable=True)
    if isinstance(e, (socket.timeout, TimeoutError)):
        return FetchError(EXIT_TIMEOUT, "连接/读取超时",
                          hint="可尝试增大 --timeout 后重跑", retryable=True)
    if isinstance(e, (ConnectionError, BrokenPipeError)):
        return FetchError(EXIT_NETWORK, "连接中断:%s" % e,
                          hint="网络波动,将自动断点续传重试", retryable=True)
    if isinstance(e, ssl.SSLError):
        return FetchError(EXIT_NETWORK, "TLS 连接中断:%s" % e,
                          hint="网络/代理波动或 TLS 被拦截,将自动断点续传重试", retryable=True)
    if isinstance(e, ValueError):
        return FetchError(EXIT_URL, "链接或响应解析失败:%s" % e,
                          hint="请检查链接格式是否完整", retryable=False)
    if isinstance(e, http.client.IncompleteRead):
        return FetchError(EXIT_NETWORK, "传输提前中断:%s" % e,
                          hint="将自动断点续传重试", retryable=True)
    if isinstance(e, http.client.HTTPException):
        return FetchError(EXIT_NETWORK, "HTTP 协议错误:%s" % e, retryable=True)
    if isinstance(e, OSError):
        if getattr(e, "errno", None) == errno.ENOSPC:
            return FetchError(EXIT_IO, "磁盘空间不足",
                              hint="请清理目标磁盘空间后重跑", retryable=False)
        return FetchError(EXIT_IO, "本地读写错误:%s" % e,
                          hint="检查保存路径权限与剩余空间", retryable=False)
    return FetchError(EXIT_IO, "未知错误:%r" % e, retryable=False)


# ---------------------------------------------------------------- 台账

_ledger_warned = False


def ledger_write(path, **fields):
    """追加一条台账记录(JSONL,UTF-8,LF 行尾)。每次尝试(含失败)都记录。"""
    global _ledger_warned
    entry = {"schema": "clawfetch-ledger/1", "ts": now_iso()}
    entry.update(fields)
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        if not _ledger_warned:  # 台账写失败不阻断下载,但明确提示一次
            _ledger_warned = True
            try:
                sys.stderr.write("[clawfetch] 警告:无法写入台账 %s(%s);下载继续,本次记录缺失\n"
                                 % (path, e))
            except Exception:
                pass
    return entry


# ---------------------------------------------------------------- 探测

def probe(url, timeout):
    """下载前探测:先 HEAD,被拒则降级 GET Range 0-0。返回 dict。"""
    last = None
    for i in range(2):
        try:
            req = urllib.request.Request(url, headers=BASE_HEADERS, method="HEAD")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _probe_from(resp.getcode(), resp.headers, resp.geturl(), via="HEAD")
        except urllib.error.HTTPError as e:
            if e.code in (400, 403, 405, 501):
                try:  # 降级 GET(只取 1 字节)
                    req = urllib.request.Request(
                        url, headers=dict(BASE_HEADERS, **{"Range": "bytes=0-0"}))
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        _ = resp.read(1)
                        return _probe_from(resp.getcode(), resp.headers,
                                           resp.geturl(), via="GET")
                except Exception as e2:
                    last = normalize_error(e2)
                    if last.retryable and i == 0:
                        time.sleep(1.5)
                        continue
                    raise last
            last = normalize_error(e)
            if last.retryable and i == 0:
                time.sleep(1.5)
                continue
            raise last
        except Exception as e:
            last = normalize_error(e)
            if last.retryable and i == 0:
                time.sleep(1.5)
                continue
            raise last
    raise last


def _probe_from(status, hdrs, final_url, via):
    total = None
    if status == 206:
        total = parse_content_range_total(hdrs.get("Content-Range"))
    if total is None:
        total = header_int(hdrs, "Content-Length")
    accept = (hdrs.get("Accept-Ranges") or "").lower()
    return {
        "status": status,
        "via": via,
        "final_url": final_url,
        "total": total,
        "accept_ranges": "bytes" in accept,
        "filename": filename_from_cd(hdrs.get("Content-Disposition")),
        "content_type": (hdrs.get("Content-Type") or "").lower(),
        "server": hdrs.get("Server"),
        "etag": hdrs.get("ETag"),
        "last_modified": hdrs.get("Last-Modified"),
    }


# ---------------------------------------------------------------- 隔离与收尾

def move_to_trash(paths, outdir, tag="failed"):
    """隔离而非删除:把文件改名移入 .clawfetch_trash/,可随时人工恢复。
    程序全程不执行任何删除操作。返回实际隔离到的路径列表。"""
    moved = []
    trash = os.path.join(outdir, TRASH_DIR)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for p in paths or []:
        try:
            if not p or not os.path.exists(p):
                continue
            os.makedirs(trash, exist_ok=True)
            base = os.path.basename(p)
            dest = os.path.join(trash, "%s.%s.%s" % (base, stamp, tag))
            k = 1
            while os.path.exists(dest):
                dest = os.path.join(trash, "%s.%s.%s.%d" % (base, stamp, tag, k))
                k += 1
            os.rename(p, dest)
            moved.append(dest)
        except OSError:
            continue
    return moved


def finalize(part, target, expected, outdir, overwrite=False):
    """收尾:大小核对 → (仅 --overwrite)旧文件进隔离区 → 改名 → 计算 SHA-256。
    返回 (size, sha256, did_rename)。非 overwrite 模式下目标意外存在则拒绝收尾,
    绝不覆盖/隔离他人文件。"""
    size = os.path.getsize(part)
    if expected is not None and size != expected:
        raise FetchError(
            EXIT_INTEGRITY,
            "大小校验失败:本地 %d 字节,服务器报告 %d 字节" % (size, expected),
            hint="服务器文件可能已变化或传输异常;本次未完成文件将被移入隔离区,重跑将重新完整下载",
            retryable=False)
    if os.path.exists(target):
        if not overwrite:
            raise FetchError(
                EXIT_IO,
                "保存目标已存在其他文件(%s),为避免覆盖已停止收尾" % os.path.basename(target),
                hint="可能是并发实例写入或文件刚被创建;重跑程序会自动另存编号副本",
                retryable=False)
        move_to_trash([target], outdir)  # --overwrite:旧文件先隔离,绝不直接覆盖
    os.rename(part, target)
    if size > 8 * 1024 * 1024:
        cwrite("正在计算 SHA-256 校验值...")
    return size, sha256_file(target), True


# ---------------------------------------------------------------- 下载主体

def _read_part_meta(meta_path):
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_part_meta(meta_path, url, etag, last_modified, total):
    try:
        with open(meta_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"url": url, "etag": etag, "last_modified": last_modified,
                       "total": total, "ts": now_iso()}, f, ensure_ascii=False)
    except OSError:
        pass


def _meta_matches(meta, etag, last_modified, url):
    """part 元数据与当前服务器标识的一致性:任一可比字段不一致即判 False;
    双方都无标识(无 ETag 且无 Last-Modified)时只能放行。"""
    if not meta or meta.get("url") != url:
        return False
    if (etag or "") != (meta.get("etag") or ""):
        return False
    if (last_modified or "") != (meta.get("last_modified") or ""):
        return False
    return True


def redact_url(u):
    """台账/控制台展示用:隐去链接中的凭据(user:pass@)。"""
    try:
        p = urlparse(u)
        if p.username:
            netloc = p.netloc
            if p.password:
                netloc = netloc.replace("%s:%s@" % (p.username, p.password), "***@", 1)
            else:
                netloc = netloc.replace("%s@" % p.username, "***@", 1)
            return p._replace(netloc=netloc).geturl()
    except Exception:
        pass
    return u


def run_attempts(args, url, target, part, total_hint, run_id, ledger, is_tty, outdir,
                 request_url=None, probe_etag=None, probe_last_modified=None):
    """带重试的下载主循环。返回 (file_size, sha256, server_size, attempts_used)。"""
    request_url = request_url or url
    ledger_url = redact_url(url)
    meta_path = part + ".meta"
    attempts = max(args.retries, 0) + 1
    attempt = 0
    renamed = False
    while attempt < attempts:
        attempt += 1
        t0 = time.monotonic()
        offset = 0
        if not args.no_resume and os.path.exists(part):
            offset = os.path.getsize(part)
            if total_hint is not None and offset > total_hint:
                offset = 0  # 本地残留比服务器文件还大:丢弃重下
        _mp = part + ".meta"
        if os.path.exists(part) and os.path.exists(_mp):
            _pm = _read_part_meta(_mp)
            if _pm and isinstance(_pm.get("segments"), list):
                # 多线程分段格式的旧进度与单线程模式不兼容,必须隔离重下。
                # (多线程路径是 open("wb")+truncate(total) 预分配到全长的;实测在 NTFS 上
                #  未标稀疏属性的预分配会**真占满**磁盘 —— 一个只下到 2MB 的 818.91MB
                #  .part,在资源管理器里就是 818.91MB。别被"空洞文件"这个说法骗了。)
                move_to_trash([part, _mp], outdir)
                offset = 0
                cwrite("提示:检测到多线程分段格式的旧进度,与单线程模式不兼容,已隔离并重新下载")
        if (not args.no_resume and total_hint is not None and offset > 0
                and offset == total_hint and os.path.exists(part)):
            part_meta = _read_part_meta(meta_path)
            if _meta_matches(part_meta, probe_etag, probe_last_modified, request_url):
                try:  # 断点恰好等于总大小且文件标识一致:直接校验收尾
                    size, sha, _ = finalize(part, target, total_hint, outdir,
                                            getattr(args, "overwrite", False))
                    move_to_trash([meta_path], outdir, tag="done")
                    ledger_write(ledger, run_id=run_id, attempt=attempt, url=ledger_url,
                                 status="success", http_status=None, server_size=total_hint,
                                 file_path=os.path.abspath(target), file_size=size,
                                 sha256=sha, resumed_from=offset,
                                 duration_s=round(time.monotonic() - t0, 3),
                                 note="断点已等于文件总大小且 ETag/时间戳与预检一致,直接校验收尾")
                    return size, sha, total_hint, attempt
                except FetchError as fe:
                    cwrite("提示:快捷收尾未通过(%s),将重新完整下载" % fe)
                    offset = 0
                except OSError as oe:
                    fe = normalize_error(oe)
                    ledger_write(ledger, run_id=run_id, attempt=attempt, url=ledger_url,
                                 status="failed", http_status=None, server_size=None,
                                 file_path=os.path.abspath(target), file_size=None,
                                 sha256=None, resumed_from=None, duration_s=None,
                                 error=str(fe), hint=fe.hint, note="快捷收尾阶段失败")
                    fe.attempts_used = attempt
                    raise
            else:
                move_to_trash([part], outdir)
                move_to_trash([meta_path], outdir)
                offset = 0
                cwrite("提示:本地断点与服务器文件标识不一致(文件可能已更换),已隔离旧进度并重新下载")
        resumed_from = offset if offset > 0 else None
        try:
            headers = dict(BASE_HEADERS)
            if offset > 0:
                headers["Range"] = "bytes=%d-" % offset
                cwrite("[尝试 %d/%d] 断点续传:从第 %s 字节继续" % (
                    attempt, attempts, format(offset, ",")))
            else:
                cwrite("[尝试 %d/%d] 开始下载%s" % (
                    attempt, attempts, "(未启用续传)" if args.no_resume else ""))
            req = urllib.request.Request(request_url, headers=headers)
            with urllib.request.urlopen(req, timeout=args.timeout) as resp:
                status = resp.getcode()
                hdrs = resp.headers
                enc = (hdrs.get("Content-Encoding") or "identity").lower()
                if enc not in ("identity", ""):
                    raise FetchError(
                        EXIT_HTTP,
                        "服务器返回了压缩编码的响应体(Content-Encoding: %s),无法安全保存" % enc,
                        hint="已请求 identity 被无视,请检查代理/网关设置",
                        retryable=False, http_status=status)
                if offset > 0 and status == 206:
                    ct = (hdrs.get("Content-Type") or "").lower()
                    cr = hdrs.get("Content-Range")
                    if "multipart" in ct:
                        move_to_trash([part], outdir)
                        move_to_trash([meta_path], outdir)
                        offset = 0
                        raise FetchError(
                            EXIT_NETWORK,
                            "服务器返回 multipart/byteranges 响应,无法安全续传",
                            hint="已隔离本地进度,将重新完整下载", retryable=True)
                    m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", cr.strip(), re.I) if cr else None
                    if m and int(m.group(1)) != offset:
                        move_to_trash([part], outdir)
                        move_to_trash([meta_path], outdir)
                        offset = 0
                        raise FetchError(
                            EXIT_NETWORK,
                            "服务器返回的续传起点(%s 字节)与本地断点(%s 字节)不一致"
                            % (format(int(m.group(1)), ","), format(offset, ",")),
                            hint="已隔离旧进度,将重新完整下载", retryable=True)
                    total = int(m.group(3)) if (m and m.group(3) != "*") else None
                    if total is None:
                        # 少数服务器 206 但无 Content-Range:与预检大小交叉验证
                        cl = header_int(hdrs, "Content-Length")
                        if total_hint is not None:
                            total = total_hint
                            if cl is not None and cl + offset != total_hint:
                                cwrite("警告:服务器 206 未带 Content-Range 且大小口径与预检不一致,按预检值 %s 处理"
                                       % human_bytes(total_hint))
                        else:
                            move_to_trash([part], outdir)
                            move_to_trash([meta_path], outdir)
                            offset = 0
                            raise FetchError(
                                EXIT_NETWORK,
                                "服务器 206 响应未带 Content-Range 且预检未获知文件大小,无法安全续传",
                                hint="已隔离本地进度,将重新完整下载", retryable=True)
                    if offset > total:
                        move_to_trash([part], outdir)
                        move_to_trash([meta_path], outdir)
                        offset = 0
                        raise FetchError(
                            EXIT_NETWORK,
                            "本地已下载部分(%d 字节)大于服务器文件(%s 字节),内容可能已变化"
                            % (offset, total),
                            hint="过期进度已移入隔离区,将从头重新下载", retryable=True)
                    if not _meta_matches(_read_part_meta(meta_path), hdrs.get("ETag"),
                                         hdrs.get("Last-Modified"), request_url):
                        move_to_trash([part], outdir)
                        move_to_trash([meta_path], outdir)
                        offset = 0
                        raise FetchError(
                            EXIT_NETWORK,
                            "服务器文件标识已变化(ETag/Last-Modified 与首次下载会话不一致),本地进度不可信",
                            hint="已隔离旧进度,将重新完整下载", retryable=True)
                    resumed_from = offset
                    f = open(part, "ab")   # total 此时已是最终文件总大小
                else:
                    if offset > 0 and status == 200:
                        cwrite("提示:服务器不支持断点续传(HTTP 200),改为从头下载")
                    offset = 0
                    resumed_from = None
                    total = header_int(hdrs, "Content-Length")
                    f = open(part, "wb")
                    _write_part_meta(meta_path, request_url, hdrs.get("ETag"),
                                     hdrs.get("Last-Modified"), total)
                try:
                    prog_total = total
                    prog = Progress(base=offset, total=prog_total, is_tty=is_tty,
                                    quiet=args.quiet)
                    wd = StallWatchdog(args.timeout, is_tty, args.quiet)
                    wd.start()
                    got = 0
                    try:
                        prog.paint(force=True)
                        while True:
                            try:
                                chunk = resp.read(CHUNK)
                            except socket.timeout:
                                raise FetchError(
                                    EXIT_TIMEOUT,
                                    "连接 %ds 无数据,读取超时" % args.timeout,
                                    hint="可尝试增大 --timeout 后重跑;重跑会自动断点续传",
                                    retryable=True)
                            if not chunk:
                                break
                            f.write(chunk)
                            got += len(chunk)
                            prog.tick(len(chunk))
                            wd.poke()
                        f.flush()
                        try:
                            os.fsync(f.fileno())
                        except OSError:
                            pass
                    finally:
                        wd.stop_evt.set()
                        clear_progress_line()
                finally:
                    f.close()
                expected = total
                if expected is not None and offset + got != expected:
                    raise FetchError(
                        EXIT_NETWORK,
                        "传输不完整:预期 %s 字节,实际收到 %s 字节"
                        % (format(expected, ","), format(offset + got, ",")),
                        hint="连接提前断开,将自动断点续传重试", retryable=True)
                size, sha, _ = finalize(part, target, expected, outdir,
                                        getattr(args, "overwrite", False))
                renamed = True
                move_to_trash([meta_path], outdir, tag="done")
                dur = time.monotonic() - t0
                ledger_write(ledger, run_id=run_id, attempt=attempt, url=ledger_url,
                             status="success", http_status=status, server_size=expected,
                             file_path=os.path.abspath(target), file_size=size,
                             sha256=sha, resumed_from=resumed_from,
                             duration_s=round(dur, 3), note=None)
                return size, sha, expected, attempt
        except KeyboardInterrupt:
            cur = os.path.getsize(part) if os.path.exists(part) else 0
            if os.path.exists(part):
                note = "用户中断;进度已保留在 %s,重跑同一命令可断点续传" % os.path.basename(part)
            else:
                note = "用户中断于收尾阶段(进度文件已合并),重跑同一命令可完成校验收尾"
            ledger_write(ledger, run_id=run_id, attempt=attempt, url=ledger_url,
                         status="interrupted", http_status=None, server_size=None,
                         file_path=os.path.abspath(target), file_size=cur,
                         sha256=None, resumed_from=resumed_from, duration_s=None,
                         note=note)
            raise
        except Exception as e:
            fe = normalize_error(e)
            cur = os.path.getsize(part) if os.path.exists(part) else 0
            dur = time.monotonic() - t0
            ledger_write(ledger, run_id=run_id, attempt=attempt, url=ledger_url,
                         status="failed", http_status=fe.http_status,
                         server_size=None, file_path=os.path.abspath(target),
                         file_size=cur, sha256=None, resumed_from=resumed_from,
                         duration_s=round(dur, 3), error=str(fe), hint=fe.hint,
                         note=None)
            if not fe.retryable or attempt >= attempts:
                fe.attempts_used = attempt
                fe.last_part_size = cur
                fe.quarantined = ([] if args.keep_part
                                  else move_to_trash([part, meta_path]
                                                     + ([target] if renamed else []),
                                                     outdir))
                if fe.quarantined:
                    ledger_write(ledger, run_id=run_id, attempt=attempt, url=ledger_url,
                                 status="failed", http_status=fe.http_status,
                                 server_size=None, file_path=None, file_size=None,
                                 sha256=None, resumed_from=None, duration_s=None,
                                 error=None, hint=None,
                                 note="最终失败收尾:未完成文件已隔离:%s"
                                      % "; ".join(fe.quarantined))
                raise
            delay = backoff_delay(args.backoff, attempt)
            cwrite("第 %d/%d 次尝试失败:%s" % (attempt, attempts, fe))
            if fe.hint:
                cwrite("  提示:%s" % fe.hint)
            cwrite("  %.1f 秒后自动重试(已下载进度保留,Ctrl+C 可中断)..." % delay)
            try:
                time.sleep(delay)
            except KeyboardInterrupt:
                ledger_write(ledger, run_id=run_id, attempt=attempt, url=ledger_url,
                             status="interrupted", http_status=None, server_size=None,
                             file_path=os.path.abspath(target), file_size=cur,
                             sha256=None, resumed_from=resumed_from, duration_s=None,
                             note="重试等待期被用户中断;进度保留在 %s"
                                  % os.path.basename(part))
                raise
    raise FetchError(EXIT_TIMEOUT, "重试次数已用尽", retryable=False)


# ---------------------------------------------------------------- 多线程分段
class MultiUnsupported(Exception):
    """服务器不支持多线程分段(无 Range/206),需回退单线程。"""


def concurrency_starved(seg_done):
    """并发把源站喂哑了吗？判据：**部分**分段一个字节都没收到，而另一部分在动。

    返回 `(是否饿死, 零进度分段数)`。

    为什么要求"有人吃到了"：全都零字节 = 源站整体不通，那是 stalled/错误
    路径该管的事，不该怪到并发数头上（降了连接也一样不通）。
    为什么这是"实测"而不是"猜服务器串行"：真实串行源站的形态就是这个 ——
    2026-09-26 那次 818.91MB 的 trycloudflare 隧道，8 连接里 3 个整整
    30 秒零字节，而剩下的在滴。
    """
    try:
        n = len(seg_done)
        fed = sum(1 for d in seg_done if d > 0)
        return (0 < fed < n, n - fed)
    except Exception:
        return (False, 0)


def retry_storm(win_retries, n_seg):
    """窗口内重试是否密集到"源站被压垮"的程度（5xx/超时刷屏）。

    阈值取 `max(2, N/2)`：单次重试是网络抖动，半个窗口的段都在重试才是过载。
    """
    try:
        return int(win_retries) >= max(2, int(n_seg) // 2)
    except Exception:
        return False


def http5xx_tripped(n_5xx, n_seg, already_weak=False, trip=HTTP5XX_TRIP):
    """5xx 熔断判据（纯函数，便于离线把数字钉住）。

    与 `retry_storm` 分开的**理由**：重试风暴要等观测窗口、而且被降档分支的
    `not errors` 前置条件挡着 —— 502 刷屏的时候恰恰降不了档。5xx 是比"重试多"
    更硬的证据（超时可能是自己网络抖，502 是源站明确拒服务），所以它单独成一条
    判据：**不看窗口、不看 errors，累计到阈值当场熔断**。

    返回 True 时调用方应把并发锁到 1，并把 `weak_source` 置位（本次运行内不再升档）。
    """
    try:
        return bool((not already_weak) and int(n_seg) > 1 and int(n_5xx) >= int(trip))
    except Exception:
        return False


def plan_segments(total, n):
    """把 [0,total) 均分为 n 段(每段至少 1MB,自动减段)。返回 [[start,end,done=0]]。"""
    n = max(1, min(int(n or 1), MAX_CONN))
    while n > 1 and total // n < 1024 * 1024:
        n -= 1
    seg_len = total // n
    segs = []
    start = 0
    for i in range(n):
        end = total - 1 if i == n - 1 else start + seg_len - 1
        segs.append([start, end, 0])
        start = end + 1
    return segs


_meta_write_lock = threading.Lock()


def _write_part_meta_v2(meta_path, url, segments, total):
    """v2 分段元数据落盘(多 worker 并发调用,加锁串行化)。"""
    with _meta_write_lock:
        try:
            with open(meta_path, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"schema": "clawfetch-meta/2", "url": url,
                           "segments": [[s["start"], s["end"], s["done"]] for s in segments],
                           "total": total, "ts": now_iso()},
                          f, ensure_ascii=False)
        except OSError:
            pass


def coalesce_ranges(segs, n):
    """把碎段合并到至多 n 段 —— **代价是中间被夹着的已完成区间会被重下**。

    为什么必须有它：一个连接只能顺序读**一段连续区间**。洞太碎的时候
    （现实例子：E:\\zl 那份 23 段的断点，洞被已下完的区间切成 20 来片），
    `_rescale_segments` 只能切出一片洞一段，于是"降到 1 个连接"永远做不到 ——
    降档会变成死循环：每轮都算出同样多的段，连接数一个都降不下来。

    宁可重下那点被夹住的字节，也要把连接数真正降下来：源站扛不住并发时，
    重下几 MB 远比继续 502 风暴划算。

    返回 `(新段列表, 被重下的字节数估计)`。
    """
    try:
        n = max(1, int(n))
        if len(segs) <= n:
            return segs, 0
        groups = []
        for i in range(n):
            a = (len(segs) * i) // n
            b = (len(segs) * (i + 1)) // n
            groups.append(segs[a:b])
        out, wasted = [], 0
        for g in groups:
            if not g:
                continue
            start, end = g[0]["start"], g[-1]["end"]
            covered = sum(x["end"] - x["start"] + 1 for x in g)
            wasted += (end - start + 1) - covered
            out.append({"start": start, "end": end, "done": 0})
        return out, max(0, wasted)
    except Exception:
        return segs, 0


def _rescale_segments(segments, new_n):
    """按已完成的分段状态重切未完成区间(洞):
    已完成字节原样保留(绝对偏移不变),未完成洞合并后均分为 new_n 段。

    洞比 new_n 还碎时走 `coalesce_ranges` 兜底（见那里的说明）。
    """
    holes = []
    for s in segments:
        seg_len = s["end"] - s["start"] + 1
        if s["done"] < seg_len:
            holes.append((s["start"] + s["done"], s["end"]))
    merged = []
    for s, e in holes:
        if merged and s == merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    if not merged:
        return []  # 全部完成
    total_hole = sum(e - s + 1 for s, e in merged)
    n = max(1, min(int(new_n), MAX_CONN))
    while n > 1 and total_hole // n < 512 * 1024:
        n -= 1
    out = []
    hi = 0
    for i in range(n):
        target = total_hole - (total_hole // n) * (n - 1) if i == n - 1 else total_hole // n
        while target > 0 and hi < len(merged):
            s, e = merged[hi]
            avail = e - s + 1
            take = min(avail, target)
            out.append({"start": s, "end": s + take - 1, "done": 0})
            s += take
            target -= take
            if s > e:
                hi += 1
            else:
                merged[hi] = (s, e)
    if len(out) > n:
        out, _ = coalesce_ranges(out, n)
    return out


def _seg_retry(idx, fe, r, retries, cancel, errors, stats=None):
    """分段级错误处理:可重试且未耗尽 → 退避后继续(True);否则记录并取消(False)。

    `stats` 是给自适应并发用的**故障计数器**：分段重试（尤其是超时、5xx）
    本身就是"源站扛不住这么多连接"的证据。旧代码只看 `errors`（重试耗尽的
    终局错误），于是 502 刷了 72 次也拦不住扩容 —— 这是本轮真实故障的直接原因。
    """
    if stats is not None:
        try:
            stats["retries"] = int(stats.get("retries") or 0) + 1
            if getattr(fe, "http_status", None) and fe.http_status >= 500:
                stats["http5xx"] = int(stats.get("http5xx") or 0) + 1
        except Exception:
            pass
    if fe.retryable and r < retries - 1 and not cancel.is_set():
        cwrite("分段 %d 第 %d 次失败:%s(%.1f 秒后本段自动重试)"
               % (idx, r + 1, fe, backoff_delay(1.0, r + 1)))
        time.sleep(backoff_delay(1.0, r + 1))
        return True
    errors.append(fe)
    cancel.set()
    return False


def _seg_worker(idx, seg, total, fh, wlock, cancel, prog, args, request_url,
                meta_path, segments, errors, stats=None):
    """单段下载线程:从 seg['start']+seg['done'] 续传到 seg['end']。"""
    seg_len = seg["end"] - seg["start"] + 1
    retries = max(args.retries, 0) + 1
    last_flush = [time.monotonic()]
    for r in range(retries):
        if cancel.is_set():
            return
        if seg["done"] >= seg_len:
            return  # 本段已收齐
        pos = seg["start"] + seg["done"]
        try:
            headers = dict(BASE_HEADERS)
            headers["Range"] = "bytes=%d-%d" % (pos, seg["end"])
            req = urllib.request.Request(request_url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=args.timeout)
            try:
                status = resp.getcode()
                hdrs = resp.headers
                if status != 206:
                    raise MultiUnsupported(
                        "分段 %d 请求返回 HTTP %s(非 206)" % (idx, status))
                m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)",
                             (hdrs.get("Content-Range") or "").strip(), re.I)
                if not m or int(m.group(1)) != pos:
                    raise FetchError(
                        EXIT_NETWORK,
                        "分段 %d 续传起点不匹配(服务器返回起点 %s,期望 %d)"
                        % (idx, m.group(1) if m else "无", pos),
                        hint="服务器对分段请求响应异常", retryable=True)
                cr_total = int(m.group(3)) if m.group(3) != "*" else None
                if cr_total is not None and cr_total != total:
                    raise FetchError(
                        EXIT_HTTP,
                        "分段 %d 返回的文件总大小(%d)与预检(%d)不一致"
                        % (idx, cr_total, total),
                        hint="服务器文件可能已变化,请重跑", retryable=False,
                        http_status=status)
                while True:
                    if cancel.is_set():
                        return
                    try:
                        chunk = resp.read(CHUNK)
                    except socket.timeout:
                        raise FetchError(
                            EXIT_TIMEOUT,
                            "分段 %d 连接 %ds 无数据,读取超时" % (idx, args.timeout),
                            hint="可尝试增大 --timeout", retryable=True)
                    if not chunk:
                        break
                    if seg["done"] + len(chunk) > seg_len:
                        chunk = chunk[: seg_len - seg["done"]]
                    with wlock:
                        fh.seek(seg["start"] + seg["done"])
                        fh.write(chunk)
                    seg["done"] += len(chunk)
                    prog.tick(len(chunk))
                    if seg["done"] >= seg_len:
                        break  # 字节已收齐,立即结束本段,不再等待多余 EOF(避免读超时后重试触发 416)
                    if time.monotonic() - last_flush[0] > 2.0:
                        last_flush[0] = time.monotonic()
                        _write_part_meta_v2(meta_path, request_url, segments, total)
                if seg["done"] >= seg_len:
                    _write_part_meta_v2(meta_path, request_url, segments, total)
                    return  # 本段完成
                raise FetchError(
                    EXIT_NETWORK,
                    "分段 %d 传输不完整:预期 %d 字节,实际 %d 字节"
                    % (idx, seg_len, seg["done"]),
                    hint="连接提前断开,本段将自动续传重试", retryable=True)
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
        except MultiUnsupported:
            errors.append(MultiUnsupported(
                "分段 %d 请求返回非 206 响应" % idx))
            cancel.set()
            return
        except FetchError as fe:
            if not _seg_retry(idx, fe, r, retries, cancel, errors, stats):
                return
        except urllib.error.HTTPError as he:
            if he.code == 416 and seg["done"] >= seg_len:
                _write_part_meta_v2(meta_path, request_url, segments, total)
                return  # 本就收齐(防御):区间无效但无需再拉
            if not _seg_retry(idx, normalize_error(he), r, retries, cancel, errors, stats):
                return
        except Exception as e:
            if not _seg_retry(idx, normalize_error(e), r, retries, cancel, errors, stats):
                return
    if seg["done"] < seg_len and not cancel.is_set():
        errors.append(FetchError(EXIT_TIMEOUT, "分段 %d 重试耗尽" % idx,
                                 retryable=True))
        cancel.set()


def download_multi(args, url, target, part, total, run_id, ledger, is_tty, outdir,
                   request_url, probe_etag, probe_last_modified):
    """多线程分段下载。服务器不支持时抛 MultiUnsupported(调用方回退单线程)。"""
    ledger_url = redact_url(url)
    meta_path = part + ".meta"
    prog = None
    attempts = max(args.retries, 0) + 1
    attempt = 0
    stalled_attempts = 0
    auto_mode = (args.segments == 0)
    # 本文件内"当前认为合适的并发"。auto 下会随实测升降，并在重试时沿用 ——
    # 否则第 2 次尝试又从 8 连接开局，把刚刚花 30 秒学到的教训重学一遍。
    preferred_n = 8
    # 本次运行里"已经确认这个源站扛不住并发"。
    # 为什么需要它、以及为什么不能只靠 preferred_n：降档只影响**下一轮**的起点，
    # 而升档阶梯是每轮重新算的。2026-09-27 真实故障里，前几轮已经降到了 1 连接，
    # 第 4 轮却又一路升到 32 连接，83 秒刷出 123 次分段重试（122 次 HTTP 5xx）。
    # 一旦置位：后续每一轮都不再升档，只按学到的并发数跑。
    weak_source = False
    while attempt < attempts:
        attempt += 1
        t0 = time.monotonic()
        meta = _read_part_meta(meta_path)
        meta_ok = bool(meta) and meta.get("url") == request_url \
            and (meta.get("etag") or "") == (probe_etag or "") \
            and (meta.get("last_modified") or "") == (probe_last_modified or "") \
            and meta.get("total") == total \
            and isinstance(meta.get("segments"), list) \
            and not args.no_resume
        if meta_ok and os.path.exists(part) and os.path.getsize(part) == total:
            segments = [{"start": int(s[0]), "end": int(s[1]), "done": int(s[2])}
                        for s in meta["segments"]]
            # 洞覆盖校验:按 start 排序后互不重叠、都落在 [0,total) 内;
            # 未列出的区间视为已完成(自适应扩线程重切后首位洞可能不在 0)
            prev_end = -1
            for s in sorted(segments, key=lambda x: x["start"]):
                if s["start"] < prev_end or s["end"] < s["start"] or s["end"] >= total:
                    meta_ok = False
                    break
                prev_end = s["end"]
            # 显式 --segments N（或 auto 在本文件内学到的合适并发）必须压过
            # 盘上的旧分段布局。否则「照提示加 --segments 1 用单连接重跑」在
            # 续传时是句空话 —— 盘上那份 23 段的 meta 会照旧开 23 个连接，
            # 用户会以为提示骗人。（2026-09-26 真实故障就是这样：失败后重跑
            # 仍以 23 连接开局。）只收不放：比目标多的段才重切。
            want_n = args.segments if args.segments > 0 else preferred_n
            if meta_ok and len(segments) > want_n:
                old_n = len(segments)
                new_segs = _rescale_segments(segments, want_n)
                if new_segs:
                    segments = new_segs
                    _write_part_meta_v2(meta_path, request_url, segments, total)
                    cwrite("[模式] 重切未完成区间：%d 段 → %d 段%s"
                           % (old_n, len(segments),
                              "（--segments %d）" % args.segments if args.segments > 0
                              else "（auto：并发多为无益）"))
        else:
            meta_ok = False
        fh = None
        try:
            if meta_ok:
                resumed = total - sum((s["end"] - s["start"] + 1) - s["done"]
                                      for s in segments)
                fh = open(part, "r+b")
            else:
                if os.path.exists(part) or os.path.exists(meta_path):
                    move_to_trash([part, meta_path], outdir)
                    cwrite("提示:旧进度与当前文件不匹配(或无有效分段信息),已隔离并重新下载")
                segments = [{"start": s[0], "end": s[1], "done": 0}
                            for s in plan_segments(
                                total, args.segments if args.segments > 0 else preferred_n)]
                fh = open(part, "wb")
                fh.truncate(total)
                fh.close()
                _write_part_meta_v2(meta_path, request_url, segments, total)
                fh = open(part, "r+b")
                resumed = 0
            cwrite("[尝试 %d/%d] 多线程分段下载:%d 个连接%s" % (
                attempt, attempts, len(segments),
                ",续传自 %s" % human_bytes(resumed) if resumed else ""))
            if weak_source:
                cwrite("[模式] 已判定源站扛不住并发：本轮沿用 %d 个连接，不再尝试升档"
                       % len(segments))
            prog = Progress(base=resumed, total=total, is_tty=is_tty,
                            quiet=args.quiet, tag="%d 线程" % len(segments))
            wlock = threading.Lock()
            cancel = threading.Event()
            errors = []
            threads = []
            seg_stats = {"retries": 0, "http5xx": 0}
            for i, seg in enumerate(segments):
                if seg["done"] > seg["end"] - seg["start"] + 1:
                    seg["done"] = seg["end"] - seg["start"] + 1  # 防御:done 超界
                t = threading.Thread(
                    target=_seg_worker,
                    args=(i, seg, total, fh, wlock, cancel, prog, args,
                          request_url, meta_path, segments, errors, seg_stats),
                    daemon=True)
                threads.append(t)
                t.start()
            starve_deadline = time.monotonic() + STARVE_SEC
            auto = (args.segments == 0)
            # 升档阶梯**从当前连接数出发算**，不再写死绝对值。
            # 旧写法 ramp=[8,16,32] + 每轮 scale_idx 归零：假设上一轮已降档到 4
            # 连接，本轮起点是 4，可第一次"升档"会直接跳到 ramp[1]=16 —— 等于把
            # 上一轮降档学到的结论当成没发生。weak_source 置位后干脆不给阶梯。
            ramp = None
            if auto and not weak_source:
                ladder, n = [len(segments)], len(segments)
                while n < MAX_CONN and len(ladder) <= 3:
                    n = min(n * 2, MAX_CONN)
                    ladder.append(n)
                ramp = ladder if len(ladder) > 1 else None
            scale_idx = 0
            prev_clean = 0.0         # 上一"干净窗口"的聚合吞吐(用于判断扩容有没有收益)
            stopped_up = False        # 已判定"再增连接不会更快"→ 不再上行
            layout_base = resumed     # 当前并发布局创建时的进度(用于判"完全空转")
            last_window = (time.monotonic(), resumed)
            last_retry_total = 0
            last_progress_ts = time.monotonic()
            last_progress_val = prog.cur() if prog else resumed
            stalled = False

            def _swap_workers(new_segs):
                """把未完成的洞重切成另一套分段并重启工作线程。**进度全部保留**。

                旧代码在"串行源"这条路径上是 move_to_trash + 回退单线程 ——
                也就是说它靠**把已下的几十 MB 丢掉**来换单线程。
                这里改成原地重切：已完成字节绝对偏移不变，只重排"洞"。
                """
                nonlocal segments, wlock, cancel, prog, errors, threads
                nonlocal starve_deadline, last_window, last_retry_total, layout_base
                cancel.set()
                for t in threads:
                    t.join()
                cur = prog.cur() if prog else 0
                segments = new_segs
                _write_part_meta_v2(meta_path, request_url, segments, total)
                wlock = threading.Lock()
                cancel = threading.Event()
                prog = Progress(base=cur, total=total, is_tty=is_tty,
                                quiet=args.quiet, tag="%d 线程" % len(segments))
                errors = []
                threads = []
                for i, sg in enumerate(segments):
                    t = threading.Thread(
                        target=_seg_worker,
                        args=(i, sg, total, fh, wlock, cancel, prog, args,
                              request_url, meta_path, segments, errors, seg_stats),
                        daemon=True)
                    threads.append(t)
                    t.start()
                starve_deadline = time.monotonic() + STARVE_SEC
                layout_base = cur
                last_window = (time.monotonic(), cur)
                last_retry_total = seg_stats["retries"]
                return cur

            try:
                while True:
                    alive = [t for t in threads if t.is_alive()]
                    if not alive:
                        break
                    now = time.monotonic()
                    cur_val = prog.cur() if prog else resumed
                    if cur_val > last_progress_val:
                        last_progress_val = cur_val
                        last_progress_ts = now
                    if not errors and now - last_progress_ts > STALL_ABORT_SEC:
                        # 全尝试无任何进度：提前中止，避免对半死源头无效等待
                        cancel.set()
                        _join_all(threads, 10.0)
                        stalled = True
                        break
                    # ── (1) 降档：并发把源站打哑了 ──────────────────────────
                    # 判据全部是**实测**，不是"猜服务器串行"：
                    #   · 饿死：新布局给了 STARVE_SEC 之后，部分分段一个字节都没
                    #     收到，而另一部分在动 —— 不是源站死，是它喂不过来这么多嘴。
                    #   · 空转：整体一点进度都没有，还在不停重试 —— 连接开太多，
                    #     每一路都拿不到可用带宽（本轮 attempt 2 就是这个样子：
                    #     8 连接跑 42 秒、零增长、HTTP 500）。
                    # 两种情况都砍半，直到单连接；全程保留进度。
                    #
                    # 刻意**不**用"重试多 → 降档"这一条单判据：健康快源遇到整段
                    # 网络闪断也会刷重试，那不是源站的错，降它的连接只会白白变慢
                    # （2026-09-11 那次 1.68GB 的实测就是靠分段级重试自愈的）。
                    # ── (0) 熔断：HTTP 5xx 风暴 ─────────────────────────────
                    # 5xx 密集是"源站扛不住这么多连接"最硬的证据，而且它**不需要**
                    # 等 STARVE_SEC 判据窗口、也**不该**被 errors 挡住 —— 下面那条
                    # 降档分支的前置条件是 not errors，于是 502 刷屏的时候反而一整轮
                    # 都降不了档，只能看着它撞到重试耗尽（2026-09-27 真实故障形态）。
                    # 一旦命中：当场降到单连接，并把 weak_source 记到本次运行结束。
                    if http5xx_tripped(seg_stats.get("http5xx"), len(segments), weak_source):
                        cwrite("[熔断] 本轮已出现 %d 次 HTTP 5xx —— 源站扛不住并发，"
                               "降到 1 个连接（进度保留，本次运行内不再升档）"
                               % int(seg_stats["http5xx"]))
                        weak_source = True
                        stopped_up = True
                        new_segs = _rescale_segments(segments, 1)
                        if new_segs and len(new_segs) < len(segments):
                            preferred_n = 1
                            _swap_workers(new_segs)
                            continue
                        cwrite("[熔断] 未完成区间太碎，切不出单连接（已降到极限 %d）"
                               % len(segments))
                    if (not errors and len(segments) > 1 and now > starve_deadline):
                        seg_done = [s["done"] for s in segments]
                        win_r = seg_stats["retries"] - last_retry_total
                        starved_now, n_starved = concurrency_starved(seg_done)
                        storm = retry_storm(win_r, len(segments))
                        no_progress = (prog.cur() if prog else 0) <= layout_base
                        if starved_now or (storm and no_progress):
                            new_n = max(1, len(segments) // 2)
                            if starved_now:
                                why = "%d/%d 个分段 %d 秒零进度" % (
                                    n_starved, len(seg_done), int(STARVE_SEC))
                            else:
                                why = "%d 秒零进度却有 %d 次分段重试" % (
                                    int(STARVE_SEC), win_r)
                            cwrite("[自动] 检测到%s —— 源站并发能力有限，"
                                   "降到 %d 个连接（进度保留，不是重下）"
                                   % (why, new_n))
                            stopped_up = True
                            # 降档是"源站弱"的直接证据，记到本次运行结束：
                            # 只改 preferred_n 不够 —— 那只能决定下一轮的起点，
                            # 下一轮照样会沿升档阶梯一路往上试。
                            weak_source = True
                            new_segs = _rescale_segments(segments, new_n)
                            # 防呆：洞碎到切不出更少的段时**不要**换布局，
                            # 否则会每轮算出同样多的段、原地打转（真踩过的坑）。
                            if len(new_segs) >= len(segments):
                                cwrite("[自动] 未完成区间太碎，切不出更少的连接"
                                       "（已降到极限 %d）" % len(segments))
                            else:
                                preferred_n = max(1, len(new_segs))
                                _swap_workers(new_segs)
                                continue
                    # ── (2) 升档：只有"这一窗干净且确有吞吐收益"才加连接 ──────
                    if (auto and ramp is not None and not stopped_up
                            and scale_idx < len(ramp) - 1
                            and not errors and now - last_window[0] >= SCALE_WINDOW_SEC):
                        done_now = prog.cur()
                        w_speed = (done_now - last_window[1]) / max(now - last_window[0], 0.1)
                        win_r = seg_stats["retries"] - last_retry_total
                        last_window = (now, done_now)
                        last_retry_total = seg_stats["retries"]
                        # 旧代码这里判健康用的是 `all(s["done"] > 0)`，即"每段都
                        # 滴到过一滴"就算健康 —— 818MB 的源站上滴速 2KB/s 也算健康，
                        # 于是 8→16→23 一路加连接，把本来能跑的单连接拖成 502 风暴。
                        # 现在：挨饿或有重试风暴 = 这一窗不干净 → 既不扩容也不下结论，
                        # 下一窗再看（快源偶发闪断不该被判死刑）。
                        storm = retry_storm(win_r, len(segments))
                        clean = (not any(s["done"] == 0 for s in segments)) and not storm
                        do_scale = False
                        if clean:
                            if scale_idx == 0:
                                do_scale = w_speed > 0
                            else:
                                # 后续窗口:聚合吞吐比上一档干净窗口提升 >=15% 才继续扩
                                do_scale = w_speed >= prev_clean * 1.15
                            if not do_scale:
                                stopped_up = True   # 干净的一窗却没提速 → 扩容无收益
                            prev_clean = w_speed
                        if do_scale:
                            scale_idx += 1
                            new_n = ramp[scale_idx]
                            cwrite("[自动] 当前聚合 %s/s,扩到 %d 线程..." % (
                                human_bytes(w_speed), new_n))
                            new_segs = _rescale_segments(segments, new_n)
                            if not new_segs:
                                break  # 已全部完成
                            _swap_workers(new_segs)
                            continue
                    time.sleep(2)
            finally:
                cancel.set()
                if fh is not None:
                    # 任何退出路径都先关共享句柄,否则 Windows 下隔离/改名会 WinError 32
                    try:
                        fh.close()
                    except OSError:
                        pass
                    fh = None
            if stalled:
                cur = int(prog.cur()) if prog else 0
                if cur > resumed:
                    stalled_attempts = 0
                else:
                    stalled_attempts += 1
                if stalled_attempts >= 2:
                    # 快速失败：直接以“源半死”终结，避免分段级错误覆盖诊断
                    _fe = FetchError(
                        EXIT_TIMEOUT,
                        "源服务器连续 %d 次尝试无任何下载进度（连接正常但不发送数据）"
                        % stalled_attempts,
                        hint="源服务器/隧道可能处于半死状态：请稍后重试同一链接，或向分享者索取新链接；已下载进度已保留，重跑同一命令可续传",
                        retryable=False)
                    _fe.keep_part = True  # 源半死属临时故障，保留断点便于后续续传
                    errors = [_fe]
                else:
                    errors.insert(0, FetchError(
                        EXIT_TIMEOUT,
                        "本次尝试 %d 秒无下载进度，已提前中止（避免无效等待）"
                        % int(STALL_ABORT_SEC),
                        hint="将自动重试；若持续无进度，源服务器可能已失效",
                        retryable=True))
            if errors:
                multi_us = [e for e in errors if isinstance(e, MultiUnsupported)]
                if multi_us:
                    move_to_trash([part, meta_path], outdir)
                    raise multi_us[0]
                fe = errors[0]
                # 失败提示要说清真正的出路。旧版一律「可尝试增大 --timeout」，
                # 但这类故障里 timeout 不是病根（单连接 3.5 秒就拿到 1MiB）——
                # 病根是连接开太多。分段重试/5xx 密集就是那个证据。
                if (int(seg_stats.get("retries") or 0) >= 5
                        and int(seg_stats.get("http5xx") or 0) >= 2):
                    # auto 模式现在会自己熔断（见上面的 HTTP5XX_TRIP），所以这里
                    # 不再把责任推给用户手动加参数 —— 只说明现象与可选的固定姿势。
                    # 显式 --segments 仍然有意义：它跳过"先开 8 个再往下试"的过程。
                    fe.hint = ("本次共 %d 次分段重试（其中 %d 次 HTTP 5xx）——"
                               "这是源站扛不住多连接的信号，auto 模式已锁为单连接。"
                               "想从第一秒就用单连接，可加 --segments 1，"
                               "或在网页直链下载处勾选「兼容弱源站」（已下进度会续传）。"
                               % (int(seg_stats["retries"]), int(seg_stats["http5xx"])))
                cur = int(prog.cur()) if prog else sum(s["done"] for s in segments)
                dur = time.monotonic() - t0
                ledger_write(ledger, run_id=run_id, attempt=attempt, url=ledger_url,
                             status="failed", http_status=getattr(fe, "http_status", None),
                             server_size=total, file_path=os.path.abspath(target),
                             file_size=cur, sha256=None,
                             resumed_from=resumed or None,
                             duration_s=round(dur, 3), error=str(fe),
                             hint=getattr(fe, "hint", None),
                             note="%d 线程分段下载" % len(segments))
                if not getattr(fe, "retryable", False) or attempt >= attempts:
                    fe.attempts_used = attempt
                    fe.quarantined = ([] if (args.keep_part
                                             or getattr(fe, "keep_part", False))
                                      else move_to_trash([part, meta_path], outdir))
                    if fe.quarantined:
                        ledger_write(ledger, run_id=run_id, attempt=attempt,
                                     url=ledger_url, status="failed",
                                     http_status=None, server_size=None,
                                     file_path=None, file_size=None, sha256=None,
                                     resumed_from=None, duration_s=None, error=None,
                                     hint=None,
                                     note="最终失败收尾:未完成文件已隔离:%s"
                                          % "; ".join(fe.quarantined))
                    raise fe
                delay = backoff_delay(args.backoff, attempt)
                cwrite("第 %d/%d 次尝试失败:%s" % (attempt, attempts, fe))
                if getattr(fe, "hint", None):
                    cwrite("  提示:%s" % fe.hint)
                cwrite("  %.1f 秒后自动重试(已下载进度保留,Ctrl+C 可中断)..." % delay)
                try:
                    time.sleep(delay)
                except KeyboardInterrupt:
                    cur = int(prog.cur()) if prog else sum(s["done"] for s in segments)
                    _write_part_meta_v2(meta_path, request_url, segments, total)
                    ledger_write(ledger, run_id=run_id, attempt=attempt, url=ledger_url,
                                 status="interrupted", http_status=None,
                                 server_size=None, file_path=os.path.abspath(target),
                                 file_size=cur, sha256=None,
                                 resumed_from=cur or None, duration_s=None,
                                 note="重试等待期被用户中断;%d 线程分段进度保留"
                                      % len(segments))
                    raise
                continue
            incomplete = [s for s in segments
                          if s["done"] < s["end"] - s["start"] + 1]
            if fh is not None:  # 收尾前必须关闭共享句柄,否则 Windows 下 rename 报 WinError 32
                try:
                    fh.close()
                except OSError:
                    pass
                fh = None
            if incomplete:
                raise FetchError(EXIT_NETWORK, "存在未完成的分段", retryable=True)
            clear_progress_line()
            size, sha, _ = finalize(part, target, total, outdir,
                                    getattr(args, "overwrite", False))
            move_to_trash([meta_path], outdir, tag="done")
            dur = time.monotonic() - t0
            ledger_write(ledger, run_id=run_id, attempt=attempt, url=ledger_url,
                         status="success", http_status=206, server_size=total,
                         file_path=os.path.abspath(target), file_size=size,
                         sha256=sha, resumed_from=resumed or None,
                         duration_s=round(dur, 3),
                         note="%d 线程分段下载" % len(segments))
            return size, sha, total, attempt
        except KeyboardInterrupt:
            if cancel is not None:
                cancel.set()
            cur = int(prog.cur()) if prog else (sum(s["done"] for s in segments) if segments else 0)
            _write_part_meta_v2(meta_path, request_url, segments, total)
            ledger_write(ledger, run_id=run_id, attempt=attempt, url=ledger_url,
                         status="interrupted", http_status=None, server_size=None,
                         file_path=os.path.abspath(target), file_size=cur,
                         sha256=None, resumed_from=cur or None, duration_s=None,
                         note="用户中断;%s 线程分段进度已保留,重跑同一命令可续传"
                              % (len(segments) if segments else "?"))
            raise
        finally:
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
    raise FetchError(EXIT_TIMEOUT, "重试次数已用尽", retryable=False)


# ---------------------------------------------------------------- HTML 中转页

PAGE_LINK_RE = re.compile(r'''href\s*=\s*["']([^"']+)["']''', re.I)


def resolve_html_page(url, timeout, all_links=False):
    """/download/ 类链接返回 HTML 中转页时:抓取页面,寻找同域直链(/stream/ 优先)。
    返回 (直链 or None, 失败原因 or None);all_links=True 时返回 (全部候选列表, 失败原因)。"""
    try:
        req = urllib.request.Request(url, headers=BASE_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = "utf-8"
            ct = resp.headers.get("Content-Type") or ""
            m = re.search(r"charset=([\w\-]+)", ct, re.I)
            if m:
                charset = m.group(1)
            raw = resp.read(1024 * 1024)
        html = raw.decode(charset, errors="replace")
    except Exception as e:
        return None, "二次抓取失败:%s" % normalize_error(e)
    host = (urlparse(url).netloc or "").lower()
    candidates = []
    for href in PAGE_LINK_RE.findall(html):
        if href.startswith("//"):
            full = (urlparse(url).scheme or "https") + ":" + href
        elif href.startswith("http://") or href.startswith("https://"):
            full = href
        elif href.startswith("/"):
            full = "%s://%s%s" % (urlparse(url).scheme or "https",
                                  urlparse(url).netloc, href)
        else:
            continue
        try:
            nl = (urlparse(full).netloc or "").lower()
        except Exception:
            continue
        if not nl or nl != host:
            continue
        low = full.lower()
        if "/stream/" in low or "/file/" in low or "/d/" in low:
            candidates.insert(0, full)
        elif "/download/" not in low:
            candidates.append(full)
    if all_links:
        return candidates, (None if candidates else "页面中未发现同域文件直链")
    if candidates:
        return candidates[0], None
    return None, "页面中未发现同域文件直链"


# ---------------------------------------------------------------- 主流程

def print_failure_block(fe, ledger_path):
    quarantined = getattr(fe, "quarantined", None) or []
    lines = ["=" * 62, "[失败] %s" % fe]
    if fe.hint:
        lines.append("  建议:%s" % fe.hint)
    lines.append("  退出码:%d(尝试次数:%d)" % (fe.code, getattr(fe, "attempts_used", 0)))
    lines.append("  台账:%s" % ledger_path)
    for q in quarantined:
        lines.append("  未完成文件已隔离到:%s(未删除,可人工检查后处理)" % q)
    if fe.code in (EXIT_NETWORK, EXIT_HTTP):
        lines.append("  (trycloudflare 临时隧道链接可能已过期:请向分享者索取新链接后重跑)")
    lines.append("=" * 62)
    for ln in lines:
        cwrite(ln)


def run_resolve(args):
    """--resolve 模式:只把链接解析成直链并显示信息,不下载。"""
    _raw = args.url[0] if isinstance(args.url, (list, tuple)) else args.url
    url, notice = normalize_url(_raw)
    if notice:
        cwrite("提示:%s" % notice)
    cwrite("[解析] 正在检查链接可用性...")
    p = probe(url, min(float(args.timeout), PROBE_TIMEOUT_MAX))
    info = {"input": url, "direct": p["final_url"] or url, "kind": "direct",
            "filename": p["filename"], "size": p["total"],
            "accept_ranges": bool(p["accept_ranges"]),
            "http_status": p["status"], "candidates": []}
    if "html" in p["content_type"]:
        cwrite("[解析] 该链接是网页中转页,正在解析直链...")
        cands, why = resolve_html_page(p["final_url"] or url, args.timeout,
                                       all_links=True)
        if not cands:
            raise FetchError(
                EXIT_URL,
                "链接返回的是网页而非文件,且未能从中解析出直链(%s)" % (why or "未知原因"),
                hint="请确认链接是否为文件直链;若是分享页,请提供页面指向的真实下载地址",
                retryable=False)
        p2 = probe(cands[0], min(float(args.timeout), PROBE_TIMEOUT_MAX))
        info["direct"] = cands[0]
        info["kind"] = "html-intermediate"
        info["candidates"] = cands
        info["filename"] = p2["filename"]
        info["size"] = p2["total"]
        info["accept_ranges"] = bool(p2["accept_ranges"])
        info["http_status"] = p2["status"]
    if getattr(args, "json", False):
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return EXIT_OK
    cwrite("=" * 62)
    cwrite("[直链] %s" % info["direct"])
    if len(info["candidates"]) > 1:
        cwrite("  候选直链:")
        for c in info["candidates"]:
            cwrite("    - %s" % c)
    cwrite("  文件名:%s" % (info["filename"] or "服务器未提供"))
    cwrite("  大小:%s" % (human_bytes(info["size"]) if info["size"] is not None
                          else "服务器未报告"))
    cwrite("  断点续传:%s" % ("支持" if info["accept_ranges"] else "未知/不支持"))
    cwrite("  提示:直链可交给任意下载器使用;clawfetch 下载时也会自动完成此解析")
    cwrite("=" * 62)
    return EXIT_OK


def _print_success(target, size, server_size, sha, used, ledger):
    ok_line = "与服务器报告一致" if server_size is not None else "服务器未报告大小,无法比对"
    cwrite("=" * 62)
    cwrite("[成功] 文件已完整下载")
    cwrite("  文件:%s" % os.path.abspath(target))
    cwrite("  大小:%s 字节(%s)- %s" % (format(size, ","), human_bytes(size), ok_line))
    cwrite("  SHA-256:%s" % sha)
    cwrite("  尝试次数:%d" % used)
    cwrite("  台账:%s" % ledger)
    cwrite("=" * 62)


def run(args):
    _raw = args.url[0] if isinstance(args.url, (list, tuple)) else args.url
    url, url_notice = normalize_url(_raw)
    outdir, override = parse_output_arg(args.output)
    try:
        os.makedirs(outdir, exist_ok=True)
    except OSError as e:
        raise FetchError(EXIT_IO, "无法创建保存目录 %s:%s" % (outdir, e))
    outdir = os.path.abspath(outdir)
    ledger = os.path.abspath(args.ledger) if args.ledger \
        else os.path.join(outdir, LEDGER_NAME)
    run_id = uuid.uuid4().hex[:12]
    is_tty = _is_tty()

    cwrite("%s v%s -- 一条命令的直链下载器" % (PROGRAM, VERSION))
    cwrite("目标:%s" % url)
    if url_notice:
        cwrite("提示:%s" % url_notice)
    cwrite("台账:%s" % ledger)

    # 1. 预检:链接可用性探测
    cwrite("[探测] 正在检查链接可用性...")
    try:
        p = probe(url, min(float(args.timeout), PROBE_TIMEOUT_MAX))
    except FetchError as fe:
        fe.attempts_used = 0
        ledger_write(ledger, run_id=run_id, attempt=0, url=redact_url(url), status="failed",
                     http_status=fe.http_status, server_size=None, file_path=None,
                     file_size=None, sha256=None, resumed_from=None, duration_s=None,
                     error=str(fe), hint=fe.hint,
                     note="preflight 探测失败,未开始下载")
        raise
    total = p["total"]
    cwrite("[探测] HTTP %s(%s)| 文件名:%s | 大小:%s | 断点续传:%s" % (
        p["status"], p["via"],
        p["filename"] or "(服务器未提供,将取 URL 末段)",
        human_bytes(total) if total is not None else "服务器未报告",
        "支持" if p["accept_ranges"] else "未知/不支持(不影响续传尝试)"))

    # 2. HTML 中转页自动转直链
    if "html" in p["content_type"]:
        cwrite("[探测] 该链接返回的是网页而非文件,正在解析中转页...")
        direct, why = resolve_html_page(p["final_url"] or url, args.timeout)
        if direct:
            cwrite("[探测] 已从中转页解析到直链:%s" % direct)
            return run(_swap_url(args, direct))
        raise FetchError(
            EXIT_URL,
            "链接返回的是网页而非文件,且未能从中解析出直链(%s)" % (why or "未知原因"),
            hint="请确认链接是否为文件直链;若是分享页,请提供页面指向的真实下载地址",
            retryable=False)

    # 3. 确定保存路径
    target, note = pick_target(outdir, override, p["filename"],
                               url_last_segment(p["final_url"] or url),
                               args.overwrite)
    part = target + ".part"
    if note:
        cwrite("[保存] %s" % note)
    cwrite("[保存] %s" % os.path.abspath(target))

    # 4. 下载(含重试/续传/校验;请求用预检解析出的最终地址,避免每次重走重定向)
    request_url = p["final_url"] or url
    segs_eff = 8 if args.segments == 0 else args.segments
    if segs_eff > 1 and total is not None and total >= MULTI_MIN:
        try:
            cwrite("[模式] 文件 ≥ 16MB 且服务器支持 Range:启用多线程分段下载(--segments 1 可切回单线程)")
            size, sha, server_size, used = download_multi(
                args, url, target, part, total, run_id, ledger, is_tty, outdir,
                request_url=request_url, probe_etag=p.get("etag"),
                probe_last_modified=p.get("last_modified"))
            _print_success(target, size, server_size, sha, used, ledger)
            return EXIT_OK
        except MultiUnsupported as e:
            cwrite("[模式] 服务器不支持多线程分段(%s),回退单线程" % e)
    size, sha, server_size, used = run_attempts(args, url, target, part, total,
                                                run_id, ledger, is_tty, outdir,
                                                request_url=request_url,
                                                probe_etag=p.get("etag"),
                                                probe_last_modified=p.get("last_modified"))

    # 5. 成功总结
    _print_success(target, size, server_size, sha, used, ledger)
    return EXIT_OK


def _swap_url(args, new_url):
    """递归下载解析出的直链:复制 args 并替换 url(防护:仅允许换一次)。"""
    args2 = copy.copy(args)
    args2.url = new_url
    args2._retried_direct = getattr(args, "_retried_direct", 0) + 1
    if args2._retried_direct > 1:
        raise FetchError(EXIT_URL, "中转页解析链路过长", retryable=False)
    return args2


def _positive_float(s):
    try:
        v = float(s)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("必须是数字:%r" % s)
    if v <= 0:
        raise argparse.ArgumentTypeError("必须大于 0:%r" % s)
    return v


def _nonneg_float(s):
    try:
        v = float(s)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("必须是数字:%r" % s)
    if v < 0:
        raise argparse.ArgumentTypeError("不能为负数:%r" % s)
    return v


def _nonneg_int(s):
    try:
        v = int(s)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("必须是整数:%r" % s)
    if v < 0:
        raise argparse.ArgumentTypeError("不能为负数:%r" % s)
    return v


def _jobs_arg(s):
    """--jobs 接受 1..4 或 auto（0=默认策略）。"""
    if str(s).strip().lower() == "auto":
        return 0
    try:
        v = int(s)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("必须是整数或 auto：%r" % s)
    if v < 1 or v > 4:
        raise argparse.ArgumentTypeError("必须在 1-4 之间：%r" % s)
    return v


def _segments_arg(s):
    """--segments 接受整数或 auto(0 表示自适应 8→16→32)。"""
    if str(s).strip().lower() == "auto":
        return 0
    try:
        v = int(s)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("必须是整数或 auto:%r" % s)
    if v < 1 or v > 64:
        raise argparse.ArgumentTypeError("必须在 1-64 之间:%r" % s)
    return v


def build_parser():
    ap = argparse.ArgumentParser(
        prog=PROGRAM,
        description="一条命令下载直链文件:自动探测、HTML 中转页转直链、重试、断点续传、SHA-256 校验、下载台账。",
        epilog="示例:\n"
               "  python clawfetch.py https://example.com/file.zip\n"
               "  python clawfetch.py https://example.com/file.zip -o D:\\downloads\n"
               "  python clawfetch.py <链接> -n 5 -t 60\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="*", help="下载链接（可多个：空格分隔；或配合 --batch 指定链接清单文件）")
    ap.add_argument("-o", "--output", metavar="PATH",
                    help="保存路径:目录(文件名自动确定)或完整文件路径;默认保存到当前目录")
    ap.add_argument("-n", "--retries", type=_nonneg_int, default=3, metavar="N",
                    help="失败自动重试次数(默认 3;总尝试 = N+1)")
    ap.add_argument("-t", "--timeout", type=_positive_float, default=30.0, metavar="SEC",
                    help="连接/读取超时秒数(默认 30;超过即判定无响应并重试)")
    ap.add_argument("--backoff", type=_nonneg_float, default=2.0, metavar="SEC",
                    help="重试退避基数,指数增长(默认 2,封顶 60)")
    ap.add_argument("--proxy", default="auto", metavar="auto|none|URL",
                    help="代理策略:auto=沿用系统/环境设置(默认);none=强制直连,忽略所有代理;"
                         "URL=只走这个代理(如 http://127.0.0.1:7890)。"
                         "下载卡住又说不清是链路还是源站时,拿 none/URL 各跑一次即可分辨")
    ap.add_argument("--no-resume", action="store_true",
                    help="禁用断点续传(默认启用)")
    ap.add_argument("-s", "--segments", type=_segments_arg, default="auto", metavar="N|auto",
                    help="多线程分段数:auto=自适应(会按实测吞吐升降,遇并发拖后腿的源自动降到单连接);"
                         "N=固定段数(1=单线程);续传时 N 会覆盖盘上的旧分段布局")
    ap.add_argument("--overwrite", action="store_true",
                    help="允许替换同名已存在文件(旧文件移入 .clawfetch_trash/ 隔离区,不删除;"
                         "默认不覆盖,自动另存编号副本)")
    ap.add_argument("--keep-part", action="store_true",
                    help="失败后保留 .part 断点文件(默认隔离到 .clawfetch_trash/)")
    ap.add_argument("--ledger", metavar="PATH",
                    help="台账文件路径(默认:保存目录下 download_ledger.jsonl)")
    ap.add_argument("--batch", metavar="FILE",
                    help="从文本文件读取链接清单（每行一个，# 开头为注释）")
    ap.add_argument("-j", "--jobs", type=_jobs_arg, default="auto", metavar="N|auto",
                    help="多链接下载模式：1=顺序逐个下载（最稳）；N≥2=同时下载 N 个；auto=默认策略（见文档）")
    ap.add_argument("--resolve", action="store_true",
                    help="只把链接解析成直链并显示信息,不下载")
    ap.add_argument("--json", action="store_true",
                    help="配合 --resolve:以 JSON 输出解析结果")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="静默模式:不输出进度,仅输出结果与错误")
    ap.add_argument("-V", "--version", action="version",
                    version="%s %s" % (PROGRAM, VERSION))
    return ap


def apply_proxy_setting(args):
    """按 --proxy 决定**整进程**的出网路径，返回一句人类可读的说明。

    为什么是「整进程装一次 opener」而不是改每个调用点：本文件有 5 处裸
    urllib.request.urlopen。install_opener() 换掉的是 urlopen 内部用的那个
    全局 opener，所以所有调用点自动生效，不会漏。
    """
    mode = (getattr(args, "proxy", None) or "auto").strip()
    if mode == "auto":
        # 把"现在到底走了哪条路"写出来 —— 这类问题最耗时间的就是不知道走了什么代理
        try:
            eff = urllib.request.getproxies() or {}
        except Exception:
            eff = {}
        return "auto(沿用系统/环境设置:%s)" % (
            ",".join("%s=%s" % kv for kv in sorted(eff.items())) or "无代理,直连")
    if mode.lower() in ("none", "no", "off", "direct"):
        urllib.request.install_opener(
            urllib.request.build_opener(urllib.request.ProxyHandler({})))
        return "none(强制直连,忽略一切代理设置)"
    url = mode if "://" in mode else "http://" + mode
    urllib.request.install_opener(urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": url, "https": url})))
    return "URL(只走 %s)" % url


def console_init():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:
            pass


def _read_batch_links(path):
    """从文本文件读取链接清单（每行一个，# 开头为注释；兼容 BOM）。"""
    links = []
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    links.append(line)
    except OSError as e:
        raise FetchError(EXIT_IO, "无法读取链接清单 %s：%s" % (path, e))
    return links


def _join_all(threads, timeout):
    """带总时限的线程收尾：整批最多等 timeout 秒，避免逐个 join 的时间被 ×N 放大。"""
    deadline = time.monotonic() + timeout
    for t in threads:
        remain = deadline - time.monotonic()
        if remain <= 0:
            break
        t.join(timeout=remain)


def _preassign_targets(args, urls):
    """并行批量:预检链接文件名并为每个链接分配互不冲突的目标路径。"""
    outdir, _ = parse_output_arg(args.output)
    try:
        os.makedirs(outdir, exist_ok=True)
    except OSError:
        pass
    used = set()
    targets = {}
    for i, u in enumerate(urls):
        name = None
        try:
            p = probe(u, min(float(args.timeout), PROBE_TIMEOUT_MAX))
            name = p.get("filename")
            # HTML 中转页:**先解析成直链,再取名**。
            # 不解析的话这里只能拿到 URL 末段(/download/<id> 这种页面地址),
            # 而真实文件名就写在直链的 Content-Disposition 里 —— 白白丢掉,
            # 于是文件被存成 `<id>.bin`。单链接模式没这个问题(它在 run() 里
            # 解析完会递归重跑一遍,取名自然用的是直链),漏的只有并行批量这条路。
            if not name and "html" in (p.get("content_type") or ""):
                tmo = min(float(args.timeout), PROBE_TIMEOUT_MAX)
                direct, why = resolve_html_page(p.get("final_url") or u, tmo)
                if direct:
                    name = probe(direct, tmo).get("filename")
                    cwrite("[批量] 链接 %d 是网页中转页 → 已解析直链后取名:%s"
                           % (i + 1, sanitize_filename(name) or "(直链未提供文件名)"))
                else:
                    cwrite("[批量] 链接 %d 是网页中转页,但没解析出直链(%s)"
                           % (i + 1, why or "未知原因"))
        except Exception:
            name = None
        name = sanitize_filename(name) or sanitize_filename(url_last_segment(u)) \
            or ("download-%02d.bin" % (i + 1))
        if not os.path.splitext(name)[1]:
            name += ".bin"  # 保证有扩展名，避免被当作目录
        base, ext = os.path.splitext(name)
        cand, k = name, 2
        while cand.lower() in used or os.path.exists(os.path.join(outdir, cand)):
            cand = "%s (%d)%s" % (base, k, ext)
            k += 1
        used.add(cand.lower())
        targets[i] = os.path.join(outdir, cand)
    return targets


def run_batch(args, urls):
    """多链接下载：-j 1 顺序逐个；-j N≥2 同时下载 N 个；auto 默认=同时 2 个。"""
    jobs = args.jobs
    if jobs == 0:
        jobs = 2 if len(urls) > 1 else 1
    jobs = max(1, min(jobs, len(urls), 4))
    t0 = time.monotonic()
    results = []
    rlock = threading.RLock()
    mode_name = "顺序逐个" if jobs == 1 else ("同时 %d 个" % jobs)
    cwrite("#" * 62)
    cwrite("[批量] 共 %d 个链接，模式：%s" % (len(urls), mode_name))
    for i, u in enumerate(urls, 1):
        cwrite("  %d. %s" % (i, redact_url(u)))
    cwrite("#" * 62)

    def _one(u, target=None):
        a2 = copy.copy(args)
        a2.url = u
        if jobs > 1:
            a2.quiet = True  # 并行时关闭逐文件进度刷新，避免多文件进度互相覆盖
            cur_segs = getattr(a2, "segments", 0)
            a2.segments = min(cur_segs if cur_segs and cur_segs > 0 else 8, 8)
        if target:
            a2.output = target  # 预分配的独立目标路径，避免同名文件并行互撞
        code = None
        err_msg = ""
        try:
            code = run(a2)
        except FetchError as fe:
            err_msg = str(fe)
            cwrite("[批量] 失败：%s" % redact_url(u))
            cwrite("       原因：%s" % fe)
            if fe.hint:
                cwrite("       建议：%s" % fe.hint)
            code = fe.code
        except Exception as e:
            fe = normalize_error(e)
            err_msg = str(fe)
            cwrite("[批量] 失败：%s" % redact_url(u))
            cwrite("       原因：%s" % fe)
            code = fe.code
        with rlock:
            results.append((u, code, err_msg))
            ndone = len(results)
        # 每完成一个就报一次总进度：并行时 quiet=True 关掉了逐文件进度条，
        # 没有这一行的话调用方（网页任务卡）在整个批量过程中看不到任何进展，
        # 只能等最后那句总结 —— 5 个链接的长任务看起来就像卡死了。
        cwrite("[批量] 已完成 %d/%d" % (ndone, len(urls)))
        return code

    targets = {}
    if jobs > 1 and len(urls) > 1:
        cwrite("[批量] 预检链接并分配独立目标文件名…")
        targets = _preassign_targets(args, urls)

    if jobs == 1:
        for i, u in enumerate(urls):
            _one(u, targets.get(i))
    else:
        # 同域链接串成一条链（同一源头互挤无益），不同域之间才并行
        groups = {}
        for i, u in enumerate(urls):
            try:
                key = (urlparse(u).netloc or "").lower()
            except Exception:
                key = ""
            groups.setdefault(key, []).append((i, u))
        chains = list(groups.values())
        if len(chains) < len(urls):
            cwrite("[批量] 检测到同域链接：同域将顺序下载、不同域并行（避免同一源头互相挤占）")
        q = list(chains)

        def _worker():
            while True:
                with rlock:
                    if not q:
                        return
                    chain = q.pop(0)
                for i, u in chain:
                    _one(u, targets.get(i))

        workers = [threading.Thread(target=_worker, daemon=True)
                   for _ in range(min(jobs, len(chains)))]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
    dur = time.monotonic() - t0
    ok = [r for r in results if r[1] == 0]
    fail = [r for r in results if r[1] != 0]
    cwrite("=" * 62)
    cwrite("[批量总结] 成功 %d/%d，总耗时 %s" % (len(ok), len(results), human_secs(dur)))
    for u, code, msg in results:
        if code == 0:
            cwrite("  [OK] %s" % redact_url(u))
        else:
            cwrite("  [失败] %s —— %s" % (redact_url(u), msg or ("退出码 %d" % code)))
    cwrite("=" * 62)
    if fail:
        return fail[0][1]
    return EXIT_OK


def main(argv=None):
    console_init()
    args = build_parser().parse_args(argv)
    try:
        proxy_note = apply_proxy_setting(args)
        if not getattr(args, "quiet", False):
            cwrite("[代理] %s" % proxy_note)
        urls = list(args.url or [])
        if getattr(args, "batch", None):
            urls += _read_batch_links(args.batch)
        if not urls:
            raise FetchError(EXIT_ARGS, "未提供下载链接",
                             hint="用法：clawfetch.py <链接> [更多链接...] 或 --batch 链接清单文件")
        if getattr(args, "resolve", False):
            args.url = urls[0]
            return run_resolve(args)
        if len(urls) == 1:
            args.url = urls[0]
            return run(args)
        return run_batch(args, urls)
    except KeyboardInterrupt:
        cwrite("[中断] 用户中断。未完成的进度已保留(*.part),重跑同一命令可断点续传。")
        return EXIT_ABORT
    except FetchError as fe:
        outdir, _ = parse_output_arg(getattr(args, "output", None))
        ledger = os.path.abspath(args.ledger) if getattr(args, "ledger", None) \
            else os.path.join(os.path.abspath(outdir), LEDGER_NAME)
        print_failure_block(fe, ledger)
        return fe.code
    except Exception as e:
        fe = normalize_error(e)
        cwrite("[失败] %s" % fe)
        if fe.hint:
            cwrite("  建议:%s" % fe.hint)
        cwrite("  退出码:%d" % fe.code)
        return fe.code


if __name__ == "__main__":
    sys.exit(main())
