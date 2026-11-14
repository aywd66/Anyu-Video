# -*- coding: utf-8 -*-
r"""任务中心进度解析：付费跳过那一桶的契约测试。

`[进度] 条目 N/M | 成功 x 失败 y 跳过 z 付费 p` 是任务卡的唯一数据源
（``bridge_server._RE_DY_ITEM``）。付费/会员内容是 CENC 密文、本地解不开，
必须单列成 ``paid``，**不能算进 failed** —— 否则一次批量会被几十条注定下不来
的作品染红（2026-10-23 那次 750 条里 22 条就是这么来的）。

同时锁住向后兼容：旧日志没有「付费」这一段，解析器必须照常工作。

跑法：
    runtime\douyin-venv\Scripts\python.exe modules\douyin\webui\tests\test_task_progress_paid.py
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # webui/

import bridge_server as B                          # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("[%s] %s %s" % ("OK " if ok else "FAIL", name, detail))
    if not ok:
        failures.append(name)


def _progress_of(lines):
    fd, path = tempfile.mkstemp(suffix=".log")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        task = {
            "id": "t1", "kind": "douyin", "status": "running",
            "log_path": path,
            "created_at": "2026-10-23T10:00:00+08:00",
        }
        return B.task_progress(task)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# 1. 新格式：付费单列，失败不被污染
p = _progress_of([
    "[进度] 条目 543/750 | 成功 521 失败 0 跳过 0 付费 22",
])
check("新格式解析出付费计数", p.get("paid") == 22, "paid=%s" % p.get("paid"))
check("付费不算进失败", p.get("failed") == 0, "failed=%s" % p.get("failed"))
check("成功/总数照旧", (p.get("done"), p.get("total")) == (521, 750),
      "done=%s total=%s" % (p.get("done"), p.get("total")))

# 2. 旧格式（没有「付费」段）必须照旧可解析 —— 历史日志还要能看
p = _progress_of([
    "[进度] 条目 10/750 | 成功 8 失败 2 跳过 0",
])
check("旧格式仍可解析", (p.get("done"), p.get("failed"), p.get("paid")) == (8, 2, 0),
      "done=%s failed=%s paid=%s" % (p.get("done"), p.get("failed"), p.get("paid")))

# 3. 只有 i/N 的极简行（更早的版本）也不能崩
p = _progress_of([
    "[进度] 条目 3/10",
])
check("极简进度行可解析", p.get("total") == 10 and p.get("done") == 3,
      "total=%s done=%s" % (p.get("total"), p.get("done")))

# 4. 结尾汇总表里的 Paid 行
p = _progress_of([
    "=== Overall Summary ===",
    "│ Total        │   750 │",
    "│ Success      │   728 │",
    "│ Failed       │     0 │",
    "│ Skipped      │     0 │",
    "│ Paid         │    22 │",
])
check("汇总表解析 Paid", (p.get("failed"), p.get("paid")) == (0, 22),
      "failed=%s paid=%s" % (p.get("failed"), p.get("paid")))

print()
if failures:
    print("失败 %d 项：%s" % (len(failures), "、".join(failures)))
    sys.exit(1)
print("全部通过")
