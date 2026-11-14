# -*- coding: utf-8 -*-
"""重启桥接服务（127.0.0.1:8518）。

为什么需要这个脚本：**「启动工具箱.vbs」的语义是 ensure-running** ——
服务已经在跑就直接复用，双击它并不会重启。所以改完 `bridge_server.py`
（或任何被它 import 的模块）之后，如果不显式停掉旧进程，跑的还是旧代码，
现象是"改了没生效"，很容易误判成改错了地方。

用法（任意 python 均可，脚本只用标准库）：
    python ops/restart-bridge.py            # 停掉旧的，起一个新的
    python ops/restart-bridge.py --status   # 只看是谁在监听，不动手
    python ops/restart-bridge.py --env KS_MAX_COLLECT_PER_DAY=800
                                            # 顺带给服务（及其子进程）带一个环境变量

判别新旧代码是否真的生效，最省事的探针是拿这次新增的接口试一下
（旧代码 404、新代码 400/200），比比对文件时间靠谱。

## 关于 `--env`：为什么需要它

快手的风控守卫（`modules/kuaishou/tools/ks_guard.py`）按自然日给「收集 / 详情」
各设一个上限，用满就当天不再跑批量 —— 这是**故意的**刹车，不是 bug。
但它自己在提示里也写了「确实要今天继续，可显式放宽：KS_MAX_COLLECT_PER_DAY」。
守卫是在**子进程**里跑的，子进程从服务继承环境变量，所以放宽的唯一姿势就是
「让服务带着这个变量重启」。

以前只能靠 `KS_MAX_COLLECT_PER_DAY=800 python ops/restart-bridge.py` 这种
一次性 shell 语法 —— 关掉终端就没人记得做过什么。做成参数后，重启日志里会印出
「本次额外环境变量：…」，事后可查。

⚠️ 这是**当天生效、重启即失效**的临时放宽；不要写进 .bat / .vbs，那等于把刹车拆了。
"""
import argparse
import os
import subprocess
import sys
import time

TB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE = os.path.join(TB, "modules", "douyin", "webui", "bridge_server.py")
CWD = os.path.join(TB, "modules", "douyin")          # CLI 与 bridge 都以它为基准解析相对路径
LOG = os.path.join(TB, "logs", "bridge.out.log")
PYW = os.path.join(TB, "runtime", "douyin-venv", "Scripts", "pythonw.exe")
PORT = 8518

# DETACHED_PROCESS | CREATE_NO_WINDOW：桥接服务要脱离当前控制台活下来，
# 且不能弹黑窗（工具箱整体是 pythonw 无窗口启动的，弹窗很扎眼）。
DETACHED = 0x00000008 | 0x08000000


def _run(cmd):
    # ⚠️ netstat / taskkill 在中文 Windows 上输出 GBK，用 text=True（UTF-8）
    #    解码会直接抛 UnicodeDecodeError，然后你拿到的是 None，看不出原因。
    return subprocess.run(cmd, capture_output=True).stdout.decode("gbk", "replace")


def listeners():
    pids = set()
    for ln in _run(["netstat", "-ano"]).splitlines():
        if (":%d" % PORT) in ln and "LISTENING" in ln:
            pids.add(ln.split()[-1])
    return pids


# ---------- 直播录制守护：重启前后各核对一次（只读，不碰它）----------
LR_PORT = 8519


def recorder_snapshot():
    """读录制守护的状态。返回 {ok, pid, recording, live} ；取不到返回 {ok: False}。

    ⚠️ 只发 **GET /status**，绝不 POST：任何写操作都可能惊动正在进行的录制。
    """
    import json
    import urllib.request
    try:
        # 显式清空代理：HTTP_PROXY 会让 localhost 请求走代理而失败
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        raw = opener.open("http://127.0.0.1:%d/status" % LR_PORT, timeout=4).read()
        d = json.loads(raw.decode("utf-8"))
        c = d.get("counts") or {}
        return {"ok": bool(d.get("ok")), "pid": d.get("pid"),
                "recording": int(c.get("recording") or 0),
                "live": int(c.get("live") or 0),
                "uptime": float(d.get("uptime") or 0)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:120]}


def recorder_guard(before, phase):
    """核对录制守护有没有被这次重启波及。返回 True = 一切正常。"""
    after = recorder_snapshot()
    if not before.get("ok"):
        # 重启前就没在跑（或读不到）：不用管，也别报成「被杀了」。
        return True
    if not after.get("ok"):
        print("  ⚠️ [%s] 录制守护 8519 读不到状态了（之前 pid=%s，正在录制 %d 个）。"
              % (phase, before.get("pid"), before.get("recording", 0)))
        print("     请立刻检查直播录制：可能已停止。")
        return False
    if before.get("pid") != after.get("pid"):
        print("  ⚠️ [%s] 录制守护 pid 变了：%s -> %s（被重启过，录制可能中断过）"
              % (phase, before.get("pid"), after.get("pid")))
        return False
    if after.get("recording", 0) < before.get("recording", 0):
        print("  ⚠️ [%s] 正在录制的房间数从 %d 降到 %d"
              % (phase, before["recording"], after["recording"]))
    else:
        print("  [OK] 录制守护未受影响：pid=%s 不变，正在录制 %d 个（已跑 %.1f 小时）"
              % (after["pid"], after.get("recording", 0), after.get("uptime", 0) / 3600.0),
              "（%s）" % phase)
    return True



def _ensure_bridge_via_launcher():
    """借 `启动工具箱.bat` 同款路径拉起桥接，复用 launcher_win.ensure_bridge()。

    为什么不自己 Popen：本脚本原先自己 Popen，实测在部分宿主环境下进程会在
    几秒后无声消失（端口起来过、/health 通过、随后进程没了、日志无报错）。
    而 `ensure_bridge()` 拉起来的服务能长期存活 —— 它是日常在用的那条路径。
    同一条路已经证明可靠，就不要在这里重写一遍。
    """
    import importlib
    try:
        webui = os.path.join(TB, "modules", "douyin", "webui")
        if webui not in sys.path:
            sys.path.insert(0, webui)
        L = importlib.import_module("launcher_win")
        ok, msg = L.ensure_bridge()
        return bool(ok), str(msg)
    except Exception as e:  # noqa: BLE001
        return False, "调用 launcher_win.ensure_bridge 失败: %s" % e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="只报告监听进程，不重启")
    ap.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                    help="额外传给服务的环境变量（可重复）。子进程会继承，"
                         "所以这是放宽快手守卫当日额度的唯一姿势。")
    args = ap.parse_args()

    env = dict(os.environ)
    extra = []
    for item in args.env:
        if "=" not in item:
            print("--env 需要 KEY=VALUE 形式，收到：%s" % item)
            return 2
        k, v = item.split("=", 1)
        k = k.strip()
        if not k:
            print("--env 的 KEY 不能为空：%s" % item)
            return 2
        env[k] = v
        extra.append("%s=%s" % (k, v))

    old = listeners()
    print("当前监听 %d 的进程: %s" % (PORT, "、".join(sorted(old)) or "无"))
    if extra:
        print("本次额外环境变量：%s" % "、".join(extra))
    if args.status:
        return 0
    if not os.path.exists(PYW):
        print("找不到 pythonw：%s" % PYW)
        return 2

    # ⚠️⚠️ 这里的 taskkill **绝不能加 `/T`**（2026-10-03 复核确认）。
    #   `/T` = 连同整棵进程树一起杀。而直播录制的守护进程
    #   （modules/live_recorder/daemon.py，监听 8519）是**独立的进程树** ——
    #   实测它与桥接 8518 没有任何父子关系，且已连续运行数天。
    #   一旦这里变成 `/T`，重启工具箱就会顺手把正在进行的直播录制一起干掉，
    #   而用户以为只是重启了个界面。
    #
    #   下面 recorder_guard() 会在动手前实测「录制还活着吗」，
    #   打完再核对一次；真出意外时能立刻说清，而不是默默录没了。
    rec_before = recorder_snapshot()
    if rec_before.get("ok"):
        print("直播录制守护 8519：pid=%s，正在录制 %d 个（已跑 %.1f 小时）"
              % (rec_before["pid"], rec_before["recording"], rec_before["uptime"] / 3600.0))
    else:
        print("直播录制守护 8519：未在运行或读不到状态（不影响本次重启）")

    for pid in sorted(old):
        print("  停止 %s：%s" % (pid, _run(["taskkill", "/PID", pid, "/F"]).strip()))
    if old:
        time.sleep(1.2)          # 等端口真正释放，否则新进程会 bind 失败

    # ⚠️ 拉起走 **`启动工具箱.bat` 那条同款路径**（launcher_win.ensure_bridge），
    #   不用本脚本自己 Popen（2026-10-03 实测踩到）。
    #   本脚本原来的 Popen 组合（DETACHED | CREATE_NO_WINDOW）**看起来**没问题，
    #   实测在某些宿主环境下拉起来的桥接会在几秒后**无声消失**：
    #   端口起来过、`/health` 也通过，但进程没了，日志里**一行报错都没有** ——
    #   典型的「被父进程 / 作业对象连带清理」。而 `ensure_bridge()` 拉起来的
    #   桥接能长期存活（日常就是这么起的）。
    #   既然同一条代码路径已经证明可靠，就不要自己重写一遍。
    #   `--env` 仍然生效：先写进 os.environ，ensure_bridge 的子进程会继承。
    os.environ.update(env)
    ok, message = _ensure_bridge_via_launcher()
    if not ok:
        print("拉起失败：%s" % message)
        recorder_guard(rec_before, "重启后")
        return 1
    print("已拉起（launcher_win.ensure_bridge）：%s（日志 %s）" % (message, LOG))

    for i in range(40):
        time.sleep(1)
        now = listeners()
        if now:
            print("服务就绪，监听进程 %s（用时 %ds）" % ("、".join(sorted(now)), i + 1))
            recorder_guard(rec_before, "重启后")
            return 0
    print("40 秒内没起来，看日志：%s" % LOG)
    recorder_guard(rec_before, "重启后")
    return 1


if __name__ == "__main__":
    sys.exit(main())
