# -*- coding: utf-8 -*-
r"""任务终态判定的离线测试（不需要网络、不产生下载）。

背景：`h_cancel` 用 taskkill 终止下载进程，退出码必然非 0；而 `watch_task`
原先无条件按退出码判定终态，于是「用户主动取消」会被显示成「失败」。
批量更新跑一半想停下时，看到的应当是「已取消」。

另有一条同样会骗人的：快手守卫刹车时刻意回**退出码 0**（「守卫是保护，
不是依赖」），原先会被判成「已完成」—— 界面上绿油油的成功，实际一个文件
都没下。2026-09-29 用户报「快手下载不了」就是这个形态。所以退出码 0 时
还要再看一眼日志里有没有刹车标记。

这套断言把四种终态钉住，防止以后有人把 canceled / braked 分支删掉。

跑法：
    runtime\douyin-venv\Scripts\python.exe modules\douyin\webui\tests\test_task_status.py
"""
import asyncio
import io
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


class FakeProc:
    """只提供 watch_task 需要的那一个方法。"""

    def __init__(self, code):
        self.code = code

    def wait(self):
        return self.code


def run_case(task_id, initial_status, exit_code, log_text=None):
    task = {
        "id": task_id,
        "status": initial_status,
        "config_path": None,        # 跳过临时配置清理
        "exit_code": None,
        "finished_at": None,
        "log_path": None,
    }
    tmp = None
    if log_text is not None:
        fd, tmp = tempfile.mkstemp(suffix=".log")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(log_text)
        task["log_path"] = tmp
    B.TASKS[task_id] = task
    try:
        asyncio.run(B.watch_task(task_id, FakeProc(exit_code)))
        t = B.TASKS[task_id]
        return t["status"], t["exit_code"], t.get("brake_reason", "")
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


BRAKE_LEASE = """# 快手任务 ktest
# 模式: all

[1] 解析链接 ...
[4] 限量批次清单 400 个 → E:\\kssp\\works_list_batch.json
[guard] 今日额度：收集 400/400、详情 0/400 | 已连续撞墙 1 次 | 另一个批量任务在跑（PID 11448，2 分钟）
[guard] 桶 bc8a957464c7 已经有一个快手批量任务在跑（PID 11448 的「batch」，已跑 2 分钟） —— 同一个桶同一时刻只跑一个。
[guard] 原因不是保守，是必要：预算是「读-判断-写」，同一桶的两个进程会
[guard] 确实要让同一桶并行（你清楚自己在承担什么）：设 KS_ALLOW_MULTI=1。
[完成] 退出码 0（守卫主动刹车，不是错误）
[完成] 退出码 0
"""

BRAKE_BUDGET = """# 快手任务 ktest2
[guard] 今日额度已用完（收集上限 400、详情上限 400）。这是**故意**的：唯一真正触发快手风控的动作就是「一口气要几千个」。
[完成] 退出码 0（守卫主动刹车，不是错误）
"""

NORMAL_OK = """# 快手任务 ktest3
[dl 1/3] 早安 OK
[dl 2/3] 晚安 OK
[完成] 退出码 0
"""

print("=" * 62)
print("任务终态判定")
print("=" * 62)

s, code, _ = run_case("t_ok", "running", 0)
check("正常退出(0) -> 已完成", s, "success")
check("  退出码如实记录", code, 0)

s, _, _ = run_case("t_bad", "running", 1)
check("异常退出(1) -> 失败", s, "failed")

s, code, _ = run_case("t_cancel", "canceled", 1)
check("已取消的任务不被改写成失败", s, "canceled")
check("  退出码仍然记录（便于排查）", code, 1)

s, _, _ = run_case("t_cancel0", "canceled", 0)
check("取消后进程恰好返回 0 也保持已取消", s, "canceled")

print()
print("-- 守卫刹车（退出码 0，但一个文件都没下）--")
s, code, why = run_case("t_brake1", "running", 0, BRAKE_LEASE)
check("租约冲突被识别为「守卫刹车」", s, "braked")
check("  原因指向「另一个批量任务在跑」", "批量任务在跑" in why, True)
check("  退出码仍是 0（如实记录）", code, 0)

s, _, why = run_case("t_brake2", "running", 0, BRAKE_BUDGET)
check("额度用尽被识别为「守卫刹车」", s, "braked")
check("  原因指向「今日额度已用完」", "额度已用完" in why, True)

s, _, why = run_case("t_normal", "running", 0, NORMAL_OK)
check("日志里没有刹车标记 -> 仍是已完成", s, "success")
check("  不给它编造刹车原因", why, "")

s, _, _ = run_case("t_brake_cancel", "canceled", 0, BRAKE_LEASE)
check("已取消的任务即使有刹车标记也保持已取消", s, "canceled")

# 清理测试用的内存条目
for k in ("t_ok", "t_bad", "t_cancel", "t_cancel0",
          "t_brake1", "t_brake2", "t_normal", "t_brake_cancel"):
    B.TASKS.pop(k, None)

print()
print("=" * 62)
print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
