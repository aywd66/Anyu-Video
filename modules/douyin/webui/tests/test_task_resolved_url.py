# -*- coding: utf-8 -*-
r"""任务中心「复制链接」取真实直链的离线测试（不需要网络、不产生下载）。

背景：直链页提交的常常**不是文件地址，而是一个取件码式的页面地址**
（例如媒体服务的 /download/<id>）。这类地址会随服务端的扫描任务过期而失效，
用户从任务中心复制出去打开，看到的只有一句
    scan task not found or expired
—— 复制了个废链接。

真正能下的那条，clawfetch 在探测阶段就解析出来并写进了任务日志：
    [探测] 已从中转页解析到直链:https://host/stream/xxxx
`task_resolved_urls()` 把它读回来，任务中心的复制按钮认它。

这套断言钉住三件事：
  1. 解析得到 → 取回的是**直链**（不是当初粘进去的 /download/ 地址）；
  2. 没解析过 / 非直链任务 → 返回空，前端回退到原始 url（不许瞎编）；
  3. 已结束的任务算一次就缓存，不随每个 3s 轮询重读日志。

跑法：
    runtime\douyin-venv\Scripts\python.exe modules\douyin\webui\tests\test_task_resolved_url.py
"""
import asyncio
import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # webui/

import bridge_server as B                          # noqa: E402

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  [OK]   %s -> %s" % (name, got))
    else:
        FAIL += 1
        print("  [FAIL] %s -> %s（期望 %s）" % (name, got, want))


def write_log(text):
    fd, path = tempfile.mkstemp(suffix=".log")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def make_task(tid, kind, log_text, status="success"):
    return {"id": tid, "kind": kind, "status": status,
            "log_path": write_log(log_text) if log_text is not None else None,
            "created_at": "2026-10-03T12:17:07+08:00", "url": "https://host/download/abc"}


# 与真实任务日志同形（取自 .tasks/db96df5f391.log）
LOG_RESOLVED = """# 直链任务 dtest1
# URL: https://host/download/08be1749871b72f3
# 连接模式: 单连接（兼容弱源站）
# 创建: 2026-10-03T12:17:07+08:00

[代理] auto(沿用系统/环境设置:无代理,直连)
clawfetch v1.4.2 -- 一条命令的直链下载器
目标:https://host/download/08be1749871b72f3
台账:E:\\zl\\download_ledger.jsonl
[探测] 正在检查链接可用性...
[探测] HTTP 200(HEAD)| 文件名:(服务器未提供,将取 URL 末段) | 大小:服务器未报告
[探测] 该链接返回的是网页而非文件,正在解析中转页...
[探测] 已从中转页解析到直链:https://host/stream/08be1749871b72f3
[保存] E:\\zl\\candy.mp4
[尝试 1/4] 开始下载
"""

LOG_PLAIN_DIRECT = """# 直链任务 dtest2
# URL: https://httpbin.org/bytes/1024

[探测] HTTP 200(GET)| 文件名:1024 | 大小:1.00 KB | 断点续传:支持
[保存] E:\\zl\\1024
[完成] 退出码 0
"""

LOG_BATCH_RESOLVED = """# 直链任务 dtest3
# 批量模式：2 个链接
#   1. https://host/download/aaa
#   2. https://host/download/bbb

[批量] 共 2 个链接，模式：同时 2 个
[探测] 已从中转页解析到直链:https://host/stream/aaa
[探测] 已从中转页解析到直链:https://host/stream/bbb
[探测] 已从中转页解析到直链:https://host/stream/aaa
[完成] 退出码 0
"""

print("=" * 62)
print("任务中心 · 复制真实直链")
print("=" * 62)

t1 = make_task("dtest1", "direct", LOG_RESOLVED)
B.TASKS["dtest1"] = t1
got = B.task_resolved_urls(t1)
check("中转页任务取回真实直链", got, ["https://host/stream/08be1749871b72f3"])
check("  取回的不是当初粘进去的取件码地址",
      got and got[0].endswith("/download/08be1749871b72f3"), False)

t2 = make_task("dtest2", "direct", LOG_PLAIN_DIRECT)
B.TASKS["dtest2"] = t2
check("本来就是直链、没解析过 -> 空（前端回退原始 url）", B.task_resolved_urls(t2), [])

t3 = make_task("dtest3", "direct", LOG_BATCH_RESOLVED)
B.TASKS["dtest3"] = t3
check("批量任务按顺序取回、重复出现只留一条",
      B.task_resolved_urls(t3),
      ["https://host/stream/aaa", "https://host/stream/bbb"])

t4 = make_task("btest1", "douyin",
               "# 抖音任务\n[探测] 已从中转页解析到直链:https://host/stream/不该被取\n")
B.TASKS["btest1"] = t4
check("非直链任务（抖音主页）不认中转页那行", B.task_resolved_urls(t4), [])

t5 = make_task("dtest5", "direct", None)
B.TASKS["dtest5"] = t5
check("没有日志文件也不炸", B.task_resolved_urls(t5), [])

# 缓存：已结束的任务算一次就不再读日志（任务中心每 3s 轮询一次）
os.remove(t1["log_path"])
check("已结束任务命中缓存，日志删了也不影响", B.task_resolved_urls(t1),
      ["https://host/stream/08be1749871b72f3"])

# 运行中的任务不缓存 —— 解析还没写进日志时为空，写进来后要能立刻看到
t6 = make_task("dtest6", "direct", "# 直链任务\n[探测] 正在检查链接可用性...\n",
               status="running")
B.TASKS["dtest6"] = t6
check("运行中：还没解析到 -> 空", B.task_resolved_urls(t6), [])
with open(t6["log_path"], "a", encoding="utf-8") as f:
    f.write("[探测] 已从中转页解析到直链:https://host/stream/late\n")
check("运行中：解析写进日志后立刻可见（不缓存）", B.task_resolved_urls(t6),
      ["https://host/stream/late"])

# 接口层：/api/bridge/tasks 必须把它带出去，否则前端拿不到
payload = json.loads(asyncio.run(B.h_tasks(None)).body.decode("utf-8"))
by_id = {t["id"]: t for t in payload["tasks"]}
check("h_tasks 给直链任务带 resolved_urls", by_id["dtest1"]["resolved_urls"],
      ["https://host/stream/08be1749871b72f3"])
check("h_tasks 不给非直链任务带这个字段", "resolved_urls" in by_id["btest1"], False)
check("内部缓存字段不外泄", "_resolved_cache" in by_id["dtest1"], False)

for k in ("dtest1", "dtest2", "dtest3", "dtest5", "dtest6", "btest1"):
    t = B.TASKS.pop(k, None)
    if t and t.get("log_path") and os.path.exists(t["log_path"]):
        os.remove(t["log_path"])

print()
print("=" * 62)
print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
