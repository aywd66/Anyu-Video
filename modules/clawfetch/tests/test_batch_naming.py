#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_batch_naming.py —— clawfetch 批量下载「先解析、再取名」回归测试

离线、不联网。测试对象是本模块 2026-09-29 那轮修复。

背景（真实故障，用户那次 8 条链接）：
  用户在网页端粘了 8 条 `…/download/<id>`（File To Link Bot 分享站）。这个路径
  返回的是 **HTML 落地页**，真实文件在 `/stream/<id>`。下完之后文件被存成
  `89dd378182be8970.bin` 这种哈希名，而站点页面上明明写着 `9.mp4`。

  解析本身是好的（run() 里遇到过 HTML 会 resolve_html_page() 后再递归跑一遍）；
  漏的是**并行批量的取名时机**：
    · 单条链接 —— 取名发生在解析之后的第二遍 run() 里 → 一直是正确的
    · 并行批量 —— run_batch() 先调 _preassign_targets() 预分配目标路径（避免
      同名互撞），此刻还没解析；HTML 页没有 Content-Disposition，于是回落到
      「URL 末段 + .bin」。而 pick_target() 里 override 优先于真实文件名，
      后面解析到真名也用不上。

本测试跑三部分：
  1. 端到端：用 mock 的 `/download/<token>` 中转页路由，断言**批量**下拿到的是
     直链广告的文件名（mock 是 mock-payload.bin），且日志有「已解析直链后取名」
  2. 对照：直链 × 批量、中转页 × 单条 两条老路径行为不变
  3. 反证：把新增分支 patch 成 `if False:` 再跑一遍，必须退回 `<token>.bin`
     —— 证明"是这段代码在起作用"，而不是碰巧

用法：python modules/clawfetch/tests/test_batch_naming.py
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.dirname(HERE)                      # modules/clawfetch
ROOT = os.path.dirname(os.path.dirname(MODULE))     # 工具箱根
CLAWFETCH = os.path.join(MODULE, "clawfetch.py")
MOCK = os.path.join(HERE, "mock_server.py")

sys.path.insert(0, HERE)
sys.path.insert(0, MODULE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

import clawfetch as cf                              # noqa: E402

PASS = [0]
FAIL = [0]
TOKEN = "nametest"
EXPECT = "mock-payload.bin"     # 与 mock_server.py 的 Content-Disposition 保持一致


def check(name, ok, extra=""):
    if ok:
        PASS[0] += 1
        print("  [OK]   %s%s" % (name, ("  (%s)" % extra) if extra else ""))
    else:
        FAIL[0] += 1
        print("  [FAIL] %s%s" % (name, ("  (%s)" % extra) if extra else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = socket.socket()
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            s.close()
            return True
        s.close()
        time.sleep(0.15)
    return False


def run_batch(script, urls, outdir):
    p = subprocess.run([sys.executable, script] + urls +
                       ["-o", outdir, "--proxy", "none"],
                       cwd=MODULE, capture_output=True, timeout=300)
    log = p.stdout.decode("utf-8", "replace")
    files = sorted(f for f in os.listdir(outdir)
                   if os.path.isfile(os.path.join(outdir, f))
                   and f != "download_ledger.jsonl")
    return p.returncode, files, log


def case(label, script, tmpl, n, port):
    d = tempfile.mkdtemp(prefix="batchname_")
    try:
        urls = [tmpl % (port, TOKEN) for _ in range(n)]
        rc, files, log = run_batch(script, urls, d)
        print("  -- %s: rc=%s files=%s" % (label, rc, files))
        return rc, files, log
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_e2e(port):
    print()
    print("[1] 端到端：批量提交中转页链接，文件名须来自直链")
    page = "http://127.0.0.1:%d/download/%s"
    direct = "http://127.0.0.1:%d/stream/%s"

    rc, files, log = case("A 中转页×2（批量）", CLAWFETCH, page, 2, port)
    check("A 退出码为 0", rc == 0, "rc=%s" % rc)
    check("A 得到直链声明的文件名", EXPECT in files, "files=%s" % files)
    check("A 第二个自动编号不覆盖",
          any(f.startswith(os.path.splitext(EXPECT)[0] + " (") for f in files), "files=%s" % files)
    check("A 不再用 URL 末段（<token>.bin）命名",
          not any(f.startswith(TOKEN) for f in files), "files=%s" % files)
    check("A 日志说明「已解析直链后取名」", "已解析直链后取名" in log)

    rc, files, log = case("B 直链×2（批量，对照）", CLAWFETCH, direct, 2, port)
    check("B 退出码为 0", rc == 0, "rc=%s" % rc)
    check("B 行为不变（仍用直链文件名）", EXPECT in files, "files=%s" % files)
    check("B 不该出现解析日志", "已解析直链后取名" not in log)

    rc, files, log = case("C 中转页×1（单条，对照）", CLAWFETCH, page, 1, port)
    check("C 退出码为 0", rc == 0, "rc=%s" % rc)
    check("C 单条路径行为不变", files == [EXPECT], "files=%s" % files)


def test_negative_control(port):
    """反证：拆掉新增分支，必须复现修复前的 <token>.bin 命名。"""
    print()
    print("[2] 反证：把新增分支 patch 成 if False: 再跑一次")
    src = open(CLAWFETCH, encoding="utf-8").read()
    marked = 'if not name and "html" in (p.get("content_type") or ""):'
    if marked not in src:
        check("反证：找到被 patch 的分支", False, "源码结构与测试预期不符，请同步本测试")
        return
    patched = src.replace(marked, "if False:  # 反证：拆掉解析分支")
    tmpd = tempfile.mkdtemp(prefix="batchname_broken_")
    try:
        broken = os.path.join(tmpd, "clawfetch_before.py")
        with open(broken, "w", encoding="utf-8") as f:
            f.write(patched)
        rc, files, _log = case("D 中转页×2（拆掉分支）", broken,
                               "http://127.0.0.1:%d/download/%s", 2, port)
        check("D 确实退回 URL 末段命名（反证成立）",
              any(f.startswith(TOKEN) for f in files), "files=%s" % files)
        check("D 不再出现直链文件名", EXPECT not in files, "files=%s" % files)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def main():
    print("=" * 68)
    print("clawfetch 批量取名回归（离线，不联网）")
    print("=" * 68)

    # mock 的文件名是写死在 Content-Disposition 里的，先确认没被改掉
    mock_src = open(MOCK, encoding="utf-8").read()
    check("mock_server 仍以 %s 作为直链文件名" % EXPECT, EXPECT in mock_src)
    check("clawfetch 具备中转页解析能力", hasattr(cf, "resolve_html_page"))
    check("预分配取名函数存在", hasattr(cf, "_preassign_targets"))

    port = free_port()
    srv = subprocess.Popen([sys.executable, MOCK, "--port", str(port),
                            "--token", TOKEN, "--size", "1048576"],
                           cwd=HERE, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    try:
        if not wait_port(port):
            check("mock 源站就绪", False, "port=%d" % port)
            return 1
        check("mock 源站就绪", True, "127.0.0.1:%d" % port)
        time.sleep(0.3)
        test_e2e(port)
        test_negative_control(port)
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except Exception:
            srv.kill()

    print()
    print("=" * 68)
    print("通过 %d 项，失败 %d 项" % (PASS[0], FAIL[0]))
    print("=" * 68)
    return 1 if FAIL[0] else 0


if __name__ == "__main__":
    sys.exit(main())
