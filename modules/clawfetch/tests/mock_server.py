#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mock_server.py —— clawfetch 异常场景测试用的本地模拟直链服务器（仅标准库）

镜像 File To Link Bot 的真实 URL 结构，便于端到端测试：
  GET/HEAD /download/<token>  → 返回 HTML 中转页（带 /stream/ 链接），测试"中转页自动转直链"
  GET/HEAD /stream/<token>    → 返回文件流（确定性伪随机字节，支持 Range/206 断点续传）

注入异常（复现失败场景）：
  --cut-at N       每个文件连接发出 N 字节后强制断开（模拟传输中断）
  --cut-once       仅第一个文件连接断开（配合 --cut-at，验证自动续传后一次下完）
  --stall-after N  发出 N 字节后挂住不再发送（模拟服务器无响应，客户端应超时重试）
  --throttle-ms N  每发出 64KB 暂停 N 毫秒（限速，便于测试大文件中断续传）
  --status CODE    /stream/ 固定返回该 HTTP 状态码（如 404，验证失败提示与退出码）
  --slots N        只允许 N 个文件响应同时进行；超出的请求**当场回 502**
                   （不排队）。用来复现**"单通道/串行源站"**：并发一多，
                   大多数连接拿不到字节（拿到的是 502），少数在滴。
                   配合 --rate-limit 使用最贴近真实（2026-09-26 那次 818.91MB 的
                   trycloudflare 临时隧道：单连接 290 KB/s，8 连接反而只有 168 KB/s，
                   3/8 个分段全程零字节，一次运行刷出 72 次 HTTP 502）。

用法示例：
  python mock_server.py --port 8394
  python mock_server.py --cut-at 1048576 --cut-once     # 断流一次，验证自动续传
  python mock_server.py --stall-after 65536             # 验证超时重试与失败隔离
  python mock_server.py --status 404                    # 验证 404 提示与退出码
  python mock_server.py --slots 2 --rate-limit 600      # 复现"并发饿死"的串行源

启动时会打印文件大小与完整 SHA-256，作为测试的校验基准。
"""

import argparse
import hashlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEED = b"clawfetch-test-payload/v1"
BLOCK = 32  # 每块 32 字节：block_at(i) = sha256(SEED + str(i))

_conn_lock = threading.Lock()
_conn_count = 0
_slot_sem = None          # --slots N：文件响应的并发槽位


class CFG(object):
    token = "83942d892579074f"
    size = 5 * 1024 * 1024
    cut_at = 0
    cut_once = False
    stall_after = 0
    blackhole = False
    throttle_ms = 0
    rate_limit = 0.0
    status = 200
    host = "127.0.0.1"
    port = 8394
    slots = 0
    slot_wait = 0.0       # ⚠️ 必须在这里给默认值：只在 handler 里读而类上没有，
                          #    会 AttributeError 把连接直接掐断（第一版就踩了，
                          #    表现为"客户端零字节超时"而不是预期的 502）


def block_at(i):
    return hashlib.sha256(SEED + str(i).encode()).digest()


def payload_sha256(size):
    h = hashlib.sha256()
    full, rem = divmod(size, BLOCK)
    for i in range(full):
        h.update(block_at(i))
    if rem:
        h.update(block_at(full)[:rem])
    return h.hexdigest()


def gen_from(start, length, chunk_size=65536):
    """从偏移 start 起产出 length 字节的确定性字节流（与 payload_sha256 同一序列）。"""
    remaining = length
    idx, skip = divmod(start, BLOCK)
    while remaining > 0:
        take = min(chunk_size, remaining)
        parts = []
        need = take
        while need > 0:
            b = block_at(idx)
            avail = BLOCK - skip
            piece = b[skip:skip + min(need, avail)]
            parts.append(piece)
            need -= len(piece)
            idx += 1
            skip = 0
        yield b"".join(parts)
        remaining -= take


PAGE_TMPL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="10;url=/stream/{TOKEN}">
<title>File Download</title>
</head>
<body>
<h1>Your file is ready</h1>
<p>Your download will begin shortly. If it does not, use the button below.</p>
<p id="timer">10</p>
<p><a id="dl" href="/stream/{TOKEN}">Download now</a></p>
</body>
</html>
"""


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MockLinkBot/1.0"

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    # ---- 路由 ----
    def _route(self):
        path = self.path.split("?")[0]
        if path.startswith("/stream/"):
            return ("stream", path[len("/stream/"):])
        if path.startswith("/download/"):
            return ("page", path[len("/download/"):])
        return None

    def do_HEAD(self):
        self._handle(head=True)

    def do_GET(self):
        self._handle(head=False)

    def _handle(self, head):
        global _conn_count
        r = self._route()
        if not r:
            self._plain(404, b"not found")
            return
        kind, token = r
        if token != CFG.token:
            self._plain(404, b"bad token")
            return
        if kind == "page":
            body = PAGE_TMPL.replace("{TOKEN}", CFG.token).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)
            return

        # ---- /stream/ 文件流 ----
        if not head:  # 仅对 GET 计数，探测 HEAD 不占“第一个连接”名额
            with _conn_lock:
                _conn_count += 1
                my_no = _conn_count
        else:
            my_no = 0
        if CFG.status != 200:
            body = ("mock error %d\n" % CFG.status).encode()
            self.send_response(CFG.status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)
            return

        size = CFG.size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        is_range = False
        if rng and rng.strip().lower().startswith("bytes="):
            spec = rng.strip()[6:].split(",")[0]
            a, _, b = spec.partition("-")
            if a.strip():
                start = int(a)
                is_range = True
            if b.strip():
                end = min(int(b), size - 1)
                is_range = True
        if is_range and start >= size:
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # --slots：单通道源站。**在发响应头之前**判定槽位 —— 抢不到就当场回 502，
        # 不排队。这一点是刻意的：真实过载源站（例如 trycloudflare 临时隧道）就是
        # "你再来一路我给你 502"，而**不是**"让你排队慢慢等"。
        #
        # 我第一版写成信号量排队（等 300 秒），结果 mock 自己造出一个假象：
        # 被取消/重试的那几十个请求全卡在队列里，新连接排在队尾，先被饿死 ——
        # 于是测出来的"没进度"是 mock 的排队，不是被测代码的问题。
        held_slot = False
        if CFG.slots and not head:
            got = (_slot_sem.acquire(timeout=CFG.slot_wait) if CFG.slot_wait > 0
                   else _slot_sem.acquire(blocking=False))
            if not got:
                self._plain(502, b"origin busy: no free stream\n")
                return
            held_slot = True

        try:
            if is_range:
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            else:
                length = size
                self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition",
                             'attachment; filename*=UTF-8\'\'mock-payload.bin; filename="mock-payload.bin"')
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            if head:
                return

            if CFG.blackhole:
                time.sleep(3600)  # 黑洞：头部正常、正文零字节（模拟半死源）
                return

            sent = 0
            try:
                for chunk in gen_from(start, length):
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    sent += len(chunk)
                    if CFG.throttle_ms:
                        time.sleep(CFG.throttle_ms / 1000.0)
                    if CFG.rate_limit:
                        time.sleep(len(chunk) / 1024.0 / CFG.rate_limit)
                    if CFG.stall_after and sent >= CFG.stall_after:
                        time.sleep(3600)  # 挂住，等待客户端超时
                        self.close_connection = True
                        return
                    if CFG.cut_at and sent >= CFG.cut_at \
                            and ((not CFG.cut_once) or my_no == 1):
                        self.close_connection = True
                        self.connection.close()  # 模拟传输中途断开
                        return
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
        finally:
            if held_slot:
                _slot_sem.release()

    def _plain(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


def main():
    global _slot_sem
    ap = argparse.ArgumentParser(description="clawfetch 异常场景测试模拟服务器")
    ap.add_argument("--host", default=CFG.host)
    ap.add_argument("--port", type=int, default=CFG.port)
    ap.add_argument("--token", default=CFG.token)
    ap.add_argument("--size", type=int, default=CFG.size,
                    help="文件字节数（默认 5242880 = 5 MB）")
    ap.add_argument("--cut-at", type=int, default=0, dest="cut_at",
                    help="每个连接发出 N 字节后强制断开")
    ap.add_argument("--cut-once", action="store_true",
                    help="仅第一个文件连接断开")
    ap.add_argument("--stall-after", type=int, default=0, dest="stall_after",
                    help="发出 N 字节后挂住不发送")
    ap.add_argument("--blackhole", action="store_true",
                    help="真黑洞模式：接受连接后零字节响应（模拟半死源）")
    ap.add_argument("--throttle-ms", type=int, default=0, dest="throttle_ms",
                    help="每发出 64KB 暂停 N 毫秒（限速）")
    ap.add_argument("--rate-limit", type=float, default=0, dest="rate_limit",
                    help="每连接限速 KB/s（模拟慢速服务器，验证多线程提速）")
    ap.add_argument("--status", type=int, default=200,
                    help="/stream/ 固定返回的 HTTP 状态码")
    ap.add_argument("--slots", type=int, default=0,
                    help="文件响应的并发槽位数；0 表示不限。>0 时超出的请求当场回 502"
                         "（模拟单通道/串行源站，配合 --rate-limit 复现'并发饿死'）")
    ap.add_argument("--slot-wait", type=float, default=0.0, dest="slot_wait",
                    help="--slots 抢不到槽位时最多等几秒再回 502（默认 0 = 立即回）")
    args = ap.parse_args()

    CFG.host, CFG.port, CFG.token, CFG.size = args.host, args.port, args.token, args.size
    CFG.cut_at, CFG.cut_once = args.cut_at, args.cut_once
    CFG.stall_after, CFG.status, CFG.throttle_ms = args.stall_after, args.status, args.throttle_ms
    CFG.rate_limit = args.rate_limit
    CFG.blackhole = args.blackhole
    CFG.slots = args.slots
    CFG.slot_wait = args.slot_wait
    if CFG.slots:
        _slot_sem = threading.BoundedSemaphore(CFG.slots)

    sha = payload_sha256(CFG.size)
    print("[MOCK-SERVER] 监听 http://%s:%d" % (CFG.host, CFG.port))
    print("[MOCK-SERVER] token=%s size=%d sha256=%s" % (CFG.token, CFG.size, sha))
    print("[MOCK-SERVER] 模式: cut_at=%d cut_once=%s stall_after=%d throttle_ms=%d rate_limit=%s status=%d blackhole=%s slots=%d"
          % (CFG.cut_at, CFG.cut_once, CFG.stall_after, CFG.throttle_ms,
             CFG.rate_limit or 0, CFG.status, CFG.blackhole, CFG.slots))
    print("[MOCK-SERVER] 页面: /download/%s  文件: /stream/%s" % (CFG.token, CFG.token))
    try:
        srv = ThreadingHTTPServer((CFG.host, CFG.port), MockHandler)
        srv.daemon_threads = True
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[MOCK-SERVER] 已停止")
    finally:
        try:
            srv.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
