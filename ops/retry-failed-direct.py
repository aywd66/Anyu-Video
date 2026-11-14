#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补齐「上次失败」的直链下载 —— 已经下好的自动跳过，不重下一遍。

背景（2026-11-01）：11 条 trycloudflare 直链，成功 6 条、失败 5 条（全是 502），
随后那条临时隧道整条离线（530 Origin DNS error）。

要分清两件事：
  · **文件 ID 是稳定的**（c79f1ef7db376309 永远是那个文件）；
  · **隧道域名不稳定** —— cloudflared 每次重开都会换一个新域名。
所以换域名时不用重贴 11 条链接，`--host` 指一下新域名即可。

用法：
    python ops/retry-failed-direct.py --dry-run      # 只看状态，不下载
    python ops/retry-failed-direct.py                # 隧道活着就立刻补下载
    python ops/retry-failed-direct.py --host xxx.trycloudflare.com
    python ops/retry-failed-direct.py --wait 900     # 最多等 15 分钟，隧道一回来就下

判定「已下载」看两处，都必须成立：台账里该 URL 有 success 记录 + 该文件此刻还在磁盘。
只看台账会把「下过但被删掉」误判成已完成。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

try:                                    # Windows 控制台 cp936 下别因为符号炸掉
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TOOLBOX_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE = "http://127.0.0.1:8518"

# 2026-11-01 那批 11 条（用户贴的是 12 条，首尾重复）。前 6 条已落盘，
# 脚本会按台账 + 磁盘自动跳过；后 5 条是当时 502 失败、至今没下到的。
DEFAULT_HOST = "ratios-george-officers-vsnet.trycloudflare.com"
ALL_IDS = [
    "9e7548537904c01e",   # 已下
    "64c6a1d4b98e1905",   # 已下
    "a50997e048260278",   # 已下
    "7c9af58b97a3bd52",   # 已下
    "294332b6be6069be",   # 已下
    "bb2c5b22b40c4d93",   # 已下
    "c79f1ef7db376309",   # 502 失败
    "d33c2f1a04558b89",   # 502 失败
    "45aa64cf52f8ec8f",   # 502 失败
    "9401efad4f8055f4",   # 502 失败
    "c05611e150b88138",   # 502 失败
]

CODE_ZH = {
    200: "可下载",
    206: "可下载（分段）",
    404: "源站已无此文件",
    403: "源站拒绝访问",
    502: "源站错误（服务器暂时不可用）",
    503: "源站过载/维护",
    504: "源站超时",
    530: "隧道离线（cloudflared 未连接）",
}


def direct_out_dir():
    """直链产物根：跟网页「直链下载」用同一个配置，不另立一套。"""
    p = os.path.join(TOOLBOX_ROOT, "data", "output_dirs.json")
    try:
        with open(p, encoding="utf-8") as f:
            v = json.load(f).get("direct")
        if v:
            return v
    except (OSError, ValueError):
        pass
    return r"E:\zl"


def done_map(outdir):
    """已成功且文件仍在磁盘的 {文件ID: 路径}。"""
    path = os.path.join(outdir, "download_ledger.jsonl")
    done = {}
    if not os.path.isfile(path):
        return done
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("status") != "success":
                continue
            fp = r.get("file_path")
            url = r.get("url") or ""
            if fp and url and os.path.isfile(fp):
                done[url.rsplit("/", 1)[-1]] = fp
    return done


def probe(url, timeout=15):
    """发一个 1KB 的 Range 请求：只看「能不能下」，不拉整个文件。

    直连（不使用环境里的 https_proxy）—— 这条隧道是公网地址，
    走本地代理只会多一层不确定性。
    """
    req = urllib.request.Request(url, headers={
        "Range": "bytes=0-1023",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            r.read(1024)
            return r.status, r.headers.get("Content-Range") or r.headers.get("Content-Length") or ""
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:                                  # 网络层错误
        return None, str(e)[:90]


def submit(urls):
    """交给网页那套任务队列：一个批量任务、同域串行、进度/取消/重试都在界面上。"""
    body = json.dumps({"urls": urls, "segments": 1}).encode("utf-8")
    req = urllib.request.Request(BRIDGE + "/api/bridge/direct/download", data=body,
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": "HTTP %s: %s" % (e.code, e.read()[:200].decode("utf-8", "replace"))}
    except Exception as e:
        return {"error": "连不上桥接服务（%s）：%s" % (BRIDGE, e)}


def collect(host):
    host = (host or DEFAULT_HOST).strip().rstrip("/")
    if host.startswith("http"):
        host = host.split("://", 1)[1]
    return [("https://%s/download/%s" % (host, i)) for i in ALL_IDS]


def main():
    ap = argparse.ArgumentParser(description="补齐上次失败的直链下载（已完成的自动跳过）")
    ap.add_argument("--host", default=DEFAULT_HOST, help="隧道域名（换隧道时改这里）")
    ap.add_argument("--wait", type=int, default=0, metavar="SEC", help="最多等多少秒，隧道回来就下")
    ap.add_argument("--interval", type=int, default=15, metavar="SEC", help="等待时多久探一次")
    ap.add_argument("--dry-run", action="store_true", help="只报状态，不提交下载")
    args = ap.parse_args()

    outdir = direct_out_dir()
    done = done_map(outdir)
    urls = collect(args.host)

    print("产物目录：%s" % outdir)
    print("待补链接：%d 条，域名 %s\n" % (len(urls), args.host))

    skip = [u for u in urls if u.rsplit("/", 1)[-1] in done]
    todo = [u for u in urls if u.rsplit("/", 1)[-1] not in done]

    if skip:
        print("已下载，跳过 %d 条：" % len(skip))
        for u in skip:
            fp = done[u.rsplit("/", 1)[-1]]
            print("  ✓ %-18s %7.0f MB  %s" % (u.rsplit("/", 1)[-1],
                                              os.path.getsize(fp) / 1048576.0,
                                              os.path.basename(fp)))
        print()

    if not todo:
        print("没有需要补的链接 —— 全部已下载。")
        return 0

    deadline = time.time() + max(0, args.wait)
    while True:
        print("探活 %d 条…" % len(todo))
        ready, bad = [], []
        for u in todo:
            code, extra = probe(u)
            if code is None:
                label = "网络错误（%s）" % (extra or "连不上")
            else:
                label = CODE_ZH.get(code, "HTTP %s" % code)
            print("  %-18s %-5s %s" % (u.rsplit("/", 1)[-1], code, label))
            (ready if code in (200, 206) else bad).append(u)

        if not bad:
            break
        if time.time() >= deadline:
            print("\n源站当前不可用：%d 条可下、%d 条不行。" % (len(ready), len(bad)))
            print("换隧道域名后重跑本脚本（--host 新域名），或加 --wait 让它在后台等。")
            return 2
        time.sleep(max(5, args.interval))

    if args.dry_run:
        print("\n[dry-run] 这 %d 条可以下载，未提交。" % len(ready))
        return 0

    print("\n提交到任务队列（单连接、同域串行）…")
    res = submit(ready)
    if res.get("error"):
        print("提交失败：%s" % res["error"])
        return 3
    task = res.get("task") or {}
    print("已入队：任务 %s，%d 条链接" % (task.get("id"), len(ready)))
    print("进度看网页「任务中心」，或直接看日志：%s" % task.get("log_path", "webui/.tasks/<id>.log"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
