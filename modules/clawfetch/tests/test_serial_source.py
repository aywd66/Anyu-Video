#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_serial_source.py —— clawfetch 并发自适应（"并发饿死串行源"）回归测试

离线、不联网。测试对象是本模块 2026-09-26 那轮修复。

背景（真实故障，E:\\zl 那次 818.91MB）：
  源站是一条 trycloudflare 临时隧道，只能同时喂很少几路。clawfetch 默认开 8 个
  分段连接，于是：
    · 单连接实测 290 KB/s；8 连接反而只有 168 KB/s，且 3/8 个分段整整 30 秒零字节
    · 一次运行刷出 72 次 HTTP 502、33 次"30s 无数据"读超时
    · 更要命的是旧代码的"自适应"只会**加**连接（8→16→32，实际 23 个），
      把本来能跑完的单连接拖成了失败
  旧代码唯一的退路是「检测到串行响应 → 丢掉已下进度 → 回退单线程」，而且它
  只在第 1 次尝试有效（`attempt == 1 and resumed == 0`）—— 第 2 次尝试起，
  那个逃生门永远打不开，于是在 8 连接上一直撞到重试耗尽。

本测试跑三部分：
  1. 判定规则（纯函数）钉在真实日志的数字上
  2. 端到端：用 mock 的 --slots 模式复现"单通道源站"，断言新逻辑能**收敛到
     少数连接并下完**，且**不丢进度**、**不走回退单线程那条丢进度的老路**
  3. 同一场景跑修复前的代码做对照（只报告，不断言 —— 旧代码的具体失败方式
     不该被写进断言里）

用法：python modules/clawfetch/tests/test_serial_source.py
"""

import hashlib
import json
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

import mock_server as mk            # noqa: E402
import clawfetch as cf              # noqa: E402

PASS = [0]
FAIL = [0]


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


def child_env(extra=None):
    """子进程环境：**必须**去掉本机代理。

    本机 shell 里有 HTTP_PROXY/HTTPS_PROXY 指向 127.0.0.1:12890，
    urllib 会老老实实把 127.0.0.1 的 mock 请求也塞给代理 → 连不上。
    """
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if extra:
        env.update(extra)
    return env


def wait_port(port, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


# ── 1. 判定规则：钉在真实日志的数字上 ────────────────────────────────────

def test_rules():
    print("── 1. 并发判据（纯函数）")
    # 真实现场：8 个分段，3 个 30 秒零字节，5 个在滴
    starved, n = cf.concurrency_starved([0, 0, 0, 4096, 8192, 1024, 64, 2048])
    check("部分分段零进度 → 判为'饿死'", starved and n == 3, "零进度 %d 个" % n)

    # 全都零字节 = 源站整体不通，不该怪并发数（降连接也没用）
    starved, n = cf.concurrency_starved([0] * 8)
    check("全部零进度 → **不**判为饿死（那是源站不通，不是并发太多）",
          starved is False and n == 8)

    # 全都吃到了 → 健康，哪怕慢也不该降档
    starved, n = cf.concurrency_starved([1024] * 8)
    check("全部有进度 → 不判为饿死", starved is False and n == 0)

    check("空列表不炸", cf.concurrency_starved([]) == (False, 0))

    # 重试风暴阈值 max(2, N/2)
    check("单次重试不算风暴", cf.retry_storm(1, 8) is False)
    check("8 连接里 2 次重试还不算风暴", cf.retry_storm(2, 8) is False)
    check("8 连接里 4 次重试 = 风暴", cf.retry_storm(4, 8) is True)
    check("2 连接里 2 次重试 = 风暴", cf.retry_storm(2, 2) is True)

    # 常量必须留得住"用观测窗口自治"的语义
    check("STARVE_SEC / SCALE_WINDOW_SEC 可用环境变量调（便于测试与现场排查）",
          isinstance(cf.STARVE_SEC, float) and isinstance(cf.SCALE_WINDOW_SEC, float))

    # 5xx 熔断（2026-09-28 加）：5xx 是比"重试多"更硬的证据 —— 超时可能是自己
    # 网络抖，502 是源站明确拒服务。所以它不看观测窗口、也不该被 errors 挡住：
    # 旧降档分支的前置条件是 not errors，502 刷屏时反而一整轮降不了档。
    check("5xx 未达阈值不熔断", cf.http5xx_tripped(4, 8) is False)
    check("5xx 达阈值 → 熔断", cf.http5xx_tripped(5, 8) is True)
    check("已经是单连接则不重复熔断", cf.http5xx_tripped(9, 1) is False)
    check("已判定弱源站后不再触发（避免和升档绕圈）",
          cf.http5xx_tripped(9, 8, True) is False)
    check("HTTP5XX_TRIP 是正整数且可调", isinstance(cf.HTTP5XX_TRIP, int)
          and cf.HTTP5XX_TRIP > 0)


# ── 2. 端到端：单通道源站 ────────────────────────────────────────────────

SIZE = 18 * 1024 * 1024      # 必须 ≥ MULTI_MIN(16MB) 才会走多线程分段路径
EXPECT_SHA = mk.payload_sha256(SIZE)


def run_case(label, script, workdir, port, size=SIZE,
             slots=2, rate=700, retries=2, timeout=8,
             starve=2, window=2, env_extra=None):
    """起 mock（--slots = 单通道源站）→ 跑一份 clawfetch → 返回结果字典。"""
    outdir = os.path.join(workdir, "out")
    os.makedirs(outdir, exist_ok=True)
    ledger = os.path.join(outdir, "ledger.jsonl")
    mock_out = open(os.path.join(workdir, "mock.out"), "wb")
    mock = subprocess.Popen(
        [sys.executable, "-u", MOCK, "--port", str(port), "--size", str(size),
         "--slots", str(slots), "--rate-limit", str(rate)],
        stdout=mock_out, stderr=subprocess.STDOUT, env=child_env())
    try:
        if not wait_port(port):
            raise RuntimeError("mock server 没起来")
        url = "http://127.0.0.1:%d/stream/%s" % (port, mk.CFG.token)
        t0 = time.time()
        p = subprocess.run(
            [sys.executable, "-u", script, url, "-o", outdir,
             "-n", str(retries), "-t", str(timeout), "--ledger", ledger],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=child_env({"CLAWFETCH_STARVE_SEC": str(starve),
                           "CLAWFETCH_SCALE_WINDOW": str(window),
                           # 缩短"全局无进度"的判死时限，失败时别让测试干等两分钟
                           "CLAWFETCH_STALL_ABORT": "20",
                           **(env_extra or {})}),
            timeout=420)
        dt = time.time() - t0
        log = p.stdout.decode("utf-8", errors="replace")
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=10)
        except Exception:
            mock.kill()
        mock_out.close()

    target = os.path.join(outdir, "mock-payload.bin")
    got_sha = None
    if os.path.exists(target):
        h = hashlib.sha256()
        with open(target, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        got_sha = h.hexdigest()
    trash = os.path.join(outdir, ".clawfetch_trash")
    trashed = os.listdir(trash) if os.path.isdir(trash) else []
    try:
        with open(os.path.join(workdir, "mock.out"), "rb") as f:
            mock_log = f.read().decode("utf-8", errors="replace")
    except OSError:
        mock_log = ""
    return {"label": label, "rc": p.returncode, "secs": dt, "log": log,
            "sha": got_sha, "target": target, "trashed": trashed,
            "failed_left": [t for t in trashed if t.endswith(".failed")],
            "mock_log": mock_log, "outdir": outdir}


def count_multi_conn(log):
    """把日志里出现过的**并发数**按时间顺序抽出来。

    注意不能只数"多线程分段下载:N 个连接" —— 那只在每次**尝试开局**打印；
    尝试内部的升降档打印的是"[自动] ... 降到 N 个连接"/"扩到 N 线程"。
    第一版漏了这点，于是"连接数一路往下"的断言永远只看到一个 8。
    """
    seq = []
    for line in log.splitlines():
        for anchor, tail, unit in (("多线程分段下载:", "个连接", "个连接"),
                                   ("降到 ", "个连接", "个连接"),
                                   ("扩到 ", "线程", "线程")):
            if anchor in line and tail in line:
                try:
                    part = line.split(anchor, 1)[1]
                    seq.append(int(part.split(unit, 1)[0].strip()))
                except (IndexError, ValueError):
                    pass
                break
    return seq


def report(r):
    print("      %s：退出码 %d，耗时 %.1fs" % (r["label"], r["rc"], r["secs"]))
    print("        开局连接数序列 = %s" % (count_multi_conn(r["log"]) or "(未走多线程)"))
    print("        sha 一致 = %s" % (r["sha"] == EXPECT_SHA))
    print("        降档次数 = %d，回退单线程 = %s，丢进度的隔离 = %s"
          % (r["log"].count("降到 "), "回退单线程" in r["log"],
             r["failed_left"] or "无"))
    if r["rc"] != 0:
        print("        失败时的日志尾部：")
        for line in [x for x in r["log"].splitlines() if x.strip()][-8:]:
            print("          | " + line[:118])


def test_e2e():
    print()
    print("── 2. 端到端：单通道源站（mock --slots 2 --rate-limit 700）")
    work = tempfile.mkdtemp(prefix="cf_serial_")
    try:
        r = run_case("修复后", CLAWFETCH, os.path.join(work, "new"), free_port())
        report(r)

        log = r["log"]
        # 先说清 mock 自己有没有坏 —— 否则 mock 的异常会伪装成被测代码的问题
        # （真踩过：CFG 上漏了 slot_wait，handler 抛 AttributeError 直接掐连接，
        #   看起来像"零字节超时"，其实是测试基建坏了）
        check("mock 服务器自身没有异常（测试基建可信）",
              "Exception occurred" not in r["mock_log"],
              r["mock_log"].strip().splitlines()[-1][:60] if "Exception occurred" in r["mock_log"] else "")
        check("下载成功（退出码 0）", r["rc"] == 0, "rc=%d" % r["rc"])
        check("文件逐字节一致（SHA-256 相同）", r["sha"] == EXPECT_SHA)
        check("确实复现了源站过载（日志里出现 502）", "502" in log,
              "%d 次" % log.count("502"))
        check("识别出'并发饿死'并主动降档", "降到 " in log,
              "降档 %d 次" % log.count("降到 "))
        seq = count_multi_conn(log)
        check("连接数是一路往下走的（不是越开越多）",
              len(seq) >= 2 and seq[-1] < seq[0], "序列 %s" % seq)
        check("真的退到了少数连接（≤4）", bool(seq) and seq[-1] <= 4,
              "最终 %s" % (seq[-1] if seq else "?"))
        check("没有走「丢掉进度重下」的老路（无回退单线程）", "回退单线程" not in log)
        check("没有把断点移进隔离区（进度全程保留）", not r["failed_left"],
              str(r["failed_left"]))
        check("文件落在目标路径且大小正确",
              os.path.exists(r["target"]) and os.path.getsize(r["target"]) == SIZE)

        # 对照：修复前的代码，同一场景
        print()
        print("  对照（仅报告，不断言）：修复前的代码跑同一场景")
        old_src = subprocess.run(
            ["git", "show", "HEAD:modules/clawfetch/clawfetch.py"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if old_src.returncode != 0:
            print("      跳过：拿不到 git HEAD 版本（%s）"
                  % old_src.stderr.decode("utf-8", "replace").strip()[:80])
        else:
            old_path = os.path.join(work, "clawfetch_old.py")
            with open(old_path, "wb") as f:
                f.write(old_src.stdout)
            ro = run_case("修复前", old_path, os.path.join(work, "old"), free_port())
            report(ro)
            print("      → 差异：修复前 %s，修复后 %s"
                  % ("失败" if ro["rc"] != 0 else "成功", "成功"))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    print("=" * 68)
    print("clawfetch 并发自适应回归（离线，不联网）")
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
