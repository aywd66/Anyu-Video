#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_proxy.py —— clawfetch 的 --proxy 开关回归测试

离线、不联网（源站是本目录的 mock_server.py）。

为什么需要这个开关：clawfetch 里原本 5 处裸 urllib.request.urlopen，出网路径
完全由环境变量决定。真实故障（手机端下发的那条 818.91MB 直链）里，
桥接服务把**外壳注入的** HTTP_PROXY/HTTPS_PROXY 原样传给了 clawfetch 子进程 ——
于是"从这台机器直连那个 URL"悄悄变成了"绕到 127.0.0.1:<某个可能是别人的、
随时会消失的端口>"。这类问题最耗时间的地方是：**你看不出它走了哪条路**。

本测试跑两部分：
  1. 纯函数：apply_proxy_setting 三种模式真的改了 urllib 的全局 opener
  2. 端到端：环境里塞一个**死代理**，证明
       auto → 被代理毁掉（复现现场）
       none → 摘掉代理就恢复（这就是修复的意义）
       URL  → 确实按你给的那个地址走（换一个同样死的地址，同样被毁）
     三种模式都拿同一个 mock 源站、同一份数据做对照，只有代理策略在变。

用法：python modules/clawfetch/tests/test_proxy.py
"""

import hashlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import types
import urllib.request

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

import mock_server as mk            # noqa: E402
import clawfetch as cf              # noqa: E402

PASS = [0]
FAIL = [0]

SIZE = 2 * 1024 * 1024             # < MULTI_MIN，走单连接；本测试只关心"出网走了哪条路"
EXPECT_SHA = mk.payload_sha256(SIZE)


def check(name, ok, extra=""):
    if ok:
        PASS[0] += 1
        print("  [OK]   %s%s" % (name, ("  (%s)" % extra) if extra else ""))
    else:
        FAIL[0] += 1
        print("  [FAIL] %s%s" % (name, ("  (%s)" % extra) if extra else ""))
    return ok


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def dead_proxy_url():
    """一个「刚被占过、现在没人听」的端口 —— 连它必然被立刻拒绝，且不会是别的东西。"""
    return "http://127.0.0.1:%d" % free_port()


def wait_port(port, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def opener_proxies():
    """urlopen 当前**实际**会用到的代理表（不猜，直接读全局 opener）。

    返回 dict；没有代理时返回 {}。注意一个 CPython 细节：`ProxyHandler({})`
    的 `*_open` 方法是在 `__init__` 的 for 循环里逐个 setattr 的，代理表为空
    时循环体不执行 —— 于是这个 handler 一个钩子都没有，`OpenerDirector`
    也就**不会把它登记进 handlers**。所以"直连"在自查里表现为
    「opener 上找不到 ProxyHandler」，而不是「找到了但它是空的」。
    """
    op = getattr(urllib.request, "_opener", None)
    for h in getattr(op, "handlers", []) or []:
        if isinstance(h, urllib.request.ProxyHandler):
            return h.proxies or {}
    return {}


# ── 1. 纯函数 ────────────────────────────────────────────────────────────

def test_rules():
    print("── 1. --proxy 三种模式真的改了出网路径（纯函数）")
    saved = opener_proxies()

    # 先塞一个环境变量代理，模拟"被外壳注入"的现场
    os.environ["HTTP_PROXY"] = "http://127.0.0.1:59999"
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:59999"
    # ⚠️ urlopen 的 opener 是**懒构建**的：没人碰过之前 _opener 是 None。
    #    不先造一个出来，"auto 不做任何事"就会被误读成"auto 把代理清掉了"。
    #    这里手动装一个与 urlopen 默认等价（build_opener()）的 opener 当基线。
    urllib.request.install_opener(urllib.request.build_opener())
    try:
        note = cf.apply_proxy_setting(types.SimpleNamespace(proxy="auto"))
        px = opener_proxies()
        check("auto：不干预，沿用环境里的代理（问题根源就在这一步）",
              note.startswith("auto") and "59999" in (px.get("http") or ""), note)

        note = cf.apply_proxy_setting(types.SimpleNamespace(proxy="none"))
        px = opener_proxies()
        check("none：环境里明明有代理，装上来的 opener 却一个代理都不认",
              "none" in note and not px, "%s / 代理表=%s" % (note, px or "空"))

        note = cf.apply_proxy_setting(types.SimpleNamespace(proxy="http://127.0.0.1:7890"))
        px = opener_proxies()
        check("URL：http/https 都指向给定地址",
              px.get("http") == "http://127.0.0.1:7890"
              and px.get("https") == "http://127.0.0.1:7890", note)

        note = cf.apply_proxy_setting(types.SimpleNamespace(proxy="127.0.0.1:7890"))
        px = opener_proxies()
        check("URL：漏写 scheme 时补 http://（用户手打地址不会因此报错）",
              px.get("http") == "http://127.0.0.1:7890", note)

        note = cf.apply_proxy_setting(types.SimpleNamespace(proxy=None))
        check("缺省/未给值时按 auto 处理（不会因为 None 崩）", note.startswith("auto"), note)
    finally:
        # 还原全局 opener，别污染同进程里的其他测试
        urllib.request.install_opener(urllib.request.build_opener(
            urllib.request.ProxyHandler(saved)))
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("HTTPS_PROXY", None)


# ── 2. 端到端：死代理下的三种策略 ────────────────────────────────────────

def child_env(proxy_url):
    """给子进程一个"被注入代理"的环境 —— 这正是桥接服务干过的事。"""
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
              "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        env.pop(k, None)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env[k] = proxy_url
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def run_case(label, proxy_arg, workdir, port, injected_proxy):
    outdir = os.path.join(workdir, "out")
    os.makedirs(outdir, exist_ok=True)
    ledger = os.path.join(outdir, "ledger.jsonl")
    mock_out = open(os.path.join(workdir, "mock.out"), "wb")
    mock = subprocess.Popen(
        [sys.executable, "-u", MOCK, "--port", str(port), "--size", str(SIZE)],
        stdout=mock_out, stderr=subprocess.STDOUT, env=child_env(dead_proxy_url()))
    try:
        if not wait_port(port):
            raise RuntimeError("mock server 没起来")
        url = "http://127.0.0.1:%d/stream/%s" % (port, mk.CFG.token)
        cmd = [sys.executable, "-u", CLAWFETCH, url, "-o", outdir,
               "-n", "0", "-t", "5", "--ledger", ledger]
        if proxy_arg is not None:
            cmd += ["--proxy", proxy_arg]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           env=child_env(injected_proxy), timeout=180)
        log = p.stdout.decode("utf-8", errors="replace")
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=10)
        except Exception:
            mock.kill()
        mock_out.close()

    target = os.path.join(outdir, "mock-payload.bin")
    got = None
    if os.path.exists(target):
        h = hashlib.sha256()
        with open(target, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        got = h.hexdigest()
    return {"label": label, "rc": p.returncode, "log": log, "sha": got,
            "size": os.path.getsize(target) if os.path.exists(target) else -1}


def test_e2e():
    print()
    print("── 2. 端到端：环境里塞一个死代理（mock 源站在 127.0.0.1，本不需要代理）")
    work = tempfile.mkdtemp(prefix="cf_proxy_")
    try:
        injected = dead_proxy_url()
        print("      注入的代理：%s（没人监听）" % injected)
        print("      源站：mock_server 127.0.0.1，2 MiB")

        ra = run_case("auto", "auto", os.path.join(work, "a"), free_port(), injected)
        print("      %-6s 退出码 %d  文件 %s" % (ra["label"], ra["rc"],
              "无" if ra["size"] < 0 else "%d B" % ra["size"]))
        check("auto：被继承来的代理拦死 —— 这就是现场那一半原因",
              ra["rc"] != 0 and ra["sha"] != EXPECT_SHA, "rc=%d" % ra["rc"])

        rb = run_case("none", "none", os.path.join(work, "b"), free_port(), injected)
        print("      %-6s 退出码 %d  文件 %s" % (rb["label"], rb["rc"],
              "无" if rb["size"] < 0 else "%d B" % rb["size"]))
        check("none：同一个死代理环境里，--proxy none 直接连通并下完",
              rb["rc"] == 0 and rb["sha"] == EXPECT_SHA, "rc=%d" % rb["rc"])
        check("none：文件逐字节一致", rb["sha"] == EXPECT_SHA)
        check("none：日志明确交代了走了哪条路",
              "[代理]" in rb["log"] and "强制直连" in rb["log"])

        rc_ = run_case("URL", dead_proxy_url(), os.path.join(work, "c"), free_port(), injected)
        print("      %-6s 退出码 %d  文件 %s" % (rc_["label"], rc_["rc"],
              "无" if rc_["size"] < 0 else "%d B" % rc_["size"]))
        check("URL：你指定的地址确实被采用（指定一个死地址同样会被拦死）",
              rc_["rc"] != 0 and "只走" in rc_["log"])
        check("URL：日志里能看到被指定的那个地址",
              "http://127.0.0.1:" in rc_["log"].split("[代理]", 1)[-1].splitlines()[0])

        # 反向断言：不传 --proxy 时行为等同 auto（默认值不能改变既有用户的体验）
        rd = run_case("默认", None, os.path.join(work, "d"), free_port(), injected)
        check("不传 --proxy = auto（默认不改变既有行为）",
              rd["rc"] != 0, "rc=%d" % rd["rc"])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    print("=" * 68)
    print("clawfetch --proxy 回归（离线，不联网）")
    print("=" * 68)
    test_rules()
    test_e2e()
    print()
    print("=" * 68)
    print("通过 %d 项，失败 %d 项" % (PASS[0], FAIL[0]))
    print("=" * 68)
    return 1 if FAIL[0] else 0


if __name__ == "__main__":
    sys.exit(main())
