# -*- coding: utf-8 -*-
"""
启动台窗口壳（launcher_win.py）
================================
五个 .bat 入口的统一桌面窗口。做三件事：

  1. 确保桥接服务在本机 8518 端口上跑着（没跑就拉起，跑着就复用）；
  2. 用原生应用窗口打开 http://127.0.0.1:8518/#/launch/<tool>；
  3. 窗口关掉就结束，不留后台残留。

为什么用 `msedge.exe --app=` 而不是 WebView2 / pywebview
--------------------------------------------------------
- WebView2 的 COM 宿主需要 WebView2Loader.dll，而本机只装了**运行时**
  （EdgeWebView\\Application\\<ver>\\，里面没有这个 SDK DLL）。自己用 ctypes
  手搓 COM 宿主意味着手写 vtable、回调接口与消息泵 —— 几百行易碎代码，
  换来的只是「窗口标题栏样式略有不同」。
- pywebview 需要额外 pip 安装，会打破"整个工具箱目录拷走即可用"这条硬约束
  （见项目记忆：运行时便携性不能破坏）。
- msedge.exe 是 Windows 10/11 的**系统自带组件**，`--app=` 模式开出来的是
  真正的原生窗口：没有标签栏、没有地址栏、没有菜单，任务栏图标与标题都可控，
  用户感知上与桌面程序无异。零新增依赖，且不会因某次 Edge 升级而失效。

回退链（任一环节缺失都如实报错，不静默失败）
--------------------------------------------
  msedge.exe → chrome.exe → 系统默认浏览器（普通标签页）
最后一级仍能打开同一页面，只是失去"应用窗口"的外观。

用法
----
    pythonw launcher_win.py <tool>
    tool ∈ {toolbox, douyin, kuaishou, live, direct}，缺省 toolbox。
"""
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))          # .../webui
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))   # 工具箱根目录
PORT = 8518
BASE = "http://127.0.0.1:%d" % PORT

# 桥接服务就在本文件旁边 —— HERE 已经是 webui/，不要再拼一遍 modules\douyin\webui
BRIDGE = os.path.join(HERE, "bridge_server.py")
BRIDGE_PY = os.path.join(ROOT, "runtime", "douyin-venv", "Scripts", "pythonw.exe")
BRIDGE_PY_FALLBACK = os.path.join(ROOT, "runtime", "douyin-venv", "Scripts", "python.exe")

VALID_TOOLS = ("toolbox", "douyin", "kuaishou", "live", "direct", "ytdlp")

# 各工具对应的工作台页标题（窗口标题用；与启动台内的工具名保持一致）
#
# live 用「直播流录制工具」而不是「直播录制器」：这条入口（启动直播录制器.bat）
# 打开的页面现在就是该工具自己的门面，标题应当与桌面录制器窗口自称的一致
# （main_window.py 里是「直播流自动录制工具」），也与页面 h1 一致。
# 同一功能在标题栏、页面、桌面窗口三处叫同一个名字，用户才不会以为是三个东西。
TITLES = {
    "toolbox":  "视频下载工具箱",
    "douyin":   "抖音下载器",
    "kuaishou": "快手一键下载器",
    "live":     "直播流录制工具",
    "direct":   "程序下载",
    "ytdlp":    "yt-dlp 万能下载",
}

# 浏览器候选（顺序即优先级）：Chrome 优先（2026-11-08 用户指定），回落系统自带 Edge
BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# 独立的用户数据目录：与用户日常浏览器配置隔离，
# 避免把工具箱的窗口混进他们的标签页会话（也就不会出现"关了浏览器把工具箱也关了"）。
PROFILE_DIR = os.path.join(ROOT, ".data", "launcher-profile")

NOWINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) if os.name == "nt" else 0


def port_open(port=PORT, timeout=0.4):
    """端口是否已被监听（不区分是谁，交给 /api/bridge/health 做归属确认）。"""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False
    finally:
        s.close()


def health_ok():
    """确认 8518 上跑的确实是本工程的桥接服务。

    只看端口通不通是不够的：别的程序也可能占 8518，
    那样窗口会打开一个完全无关的页面。用 /api/bridge/health 认领。

    注意必须**显式禁用代理**：本机（以及不少企业/沙箱环境）会把
    HTTP_PROXY 指向本地代理，urllib 默认会把 127.0.0.1 的请求也送进代理，
    结果拿到 502 而不是真正的响应 —— 于是健康检查假阴性，
    窗口壳会误判「8518 被外来程序占用」而拒绝启动。这个坑在联调时真踩过。
    """
    try:
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(BASE + "/api/bridge/health", timeout=3) as r:
            body = r.read(400).decode("utf-8", "replace")
        return '"status"' in body or "ok" in body.lower()
    except Exception:
        return False


def ensure_bridge():
    """确保桥接服务在跑。返回 (ok, message)。"""
    if port_open() and health_ok():
        return True, "复用已在运行的桥接服务"

    if port_open() and not health_ok():
        return False, ("端口 %d 被其它程序占用，且不是本工程的桥接服务。\n"
                       "请先关闭占用该端口的程序后重试。" % PORT)

    if not os.path.exists(BRIDGE):
        return False, "找不到桥接服务脚本：\n%s" % BRIDGE

    py = BRIDGE_PY if os.path.exists(BRIDGE_PY) else BRIDGE_PY_FALLBACK
    if not os.path.exists(py):
        return False, ("找不到抖音模块运行时：\n%s\n\n"
                       "请先确认工具箱运行目录完整（runtime\\douyin-venv）。" % BRIDGE_PY)

    try:
        # 独立进程 + 无控制台：桥接服务要在窗口关掉之后继续活着，
        # 否则下次开别的工具又要重启一次（冷启动要好几秒）。
        subprocess.Popen(
            [py, BRIDGE],
            cwd=os.path.join(ROOT, "modules", "douyin"),
            creationflags=NOWINDOW | DETACHED,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        return False, "无法启动桥接服务：%s" % e

    # 等它起来：冷启动要导入 aiohttp/yaml 并建索引，给足 20 秒
    deadline = time.time() + 20
    while time.time() < deadline:
        if port_open() and health_ok():
            return True, "桥接服务已启动"
        time.sleep(0.4)
    return False, "桥接服务启动超时（20 秒内未就绪）。\n请改用「启动工具箱.bat」查看详细报错。"


def pick_browser():
    for p in BROWSERS:
        if os.path.exists(p):
            return p
    return None


def open_window(url, title):
    """以原生应用窗口打开 url。返回 (ok, message)。"""
    exe = pick_browser()
    if not exe:
        # 最后一级回退：系统默认浏览器（普通标签页，失去应用窗口外观）
        try:
            os.startfile(url)
            return True, "已用系统默认浏览器打开（未找到 Edge / Chrome）"
        except Exception as e:
            return False, "无法打开浏览器：%s" % e

    try:
        os.makedirs(PROFILE_DIR, exist_ok=True)
    except Exception:
        pass

    args = [
        exe,
        "--app=" + url,
        # 独立配置目录：不污染用户日常浏览器会话
        "--user-data-dir=" + PROFILE_DIR,
        # 应用窗口不要再开自带的新标签页/首次运行向导
        "--no-first-run",
        "--no-default-browser-check",
        # 窗口尺寸：给一个能放下工作台顶部栏 + 内容区的合理默认值
        "--window-size=1280,860",
    ]
    try:
        subprocess.Popen(args, creationflags=NOWINDOW)
        return True, "已打开 %s 窗口" % title
    except Exception as e:
        return False, "打开窗口失败：%s" % e


def _has_user_session():
    """当前进程是否处于可交互的桌面会话（有人能点那个弹窗）。

    自动化脚本调本文件时不希望弹窗把调用方挂死 —— MessageBoxW 是模态的，
    没人点就一直不返回。无交互会话下只写 stderr + 退出码。
    """
    if os.environ.get("LAUNCHER_NONINTERACTIVE") == "1":
        return False
    if sys.stdout is not None and sys.stdout.isatty():
        return False   # 从终端直接跑：走标准错误输出即可，别弹窗
    try:
        import ctypes
        # 有交互窗口站才认为有人能看见弹窗
        return bool(ctypes.windll.user32.GetProcessWindowStation())
    except Exception:
        return False


def fatal(title, msg):
    """无控制台环境下的兜底报错（pythonw 下 print 无处可去）。

    与 toolbox.py 的 _fatal 同口径：直接调 user32，刻意不依赖任何 GUI 库 ——
    因为 GUI 栈本身很可能就是失败原因。

    但模态弹窗只在「确认有人能点」时才用；否则会把自动化调用挂死。
    """
    to_stderr = (sys.stderr is not None) and not _has_user_session()
    if to_stderr:
        try:
            sys.stderr.write("%s\n%s\n" % (title, msg))
            sys.stderr.flush()
        except Exception:
            pass
        return
    try:
        import ctypes
        # MB_SETFOREGROUND(0x10000) 让弹窗浮到最前，避免被别的窗口盖住以为没反应
        ctypes.windll.user32.MessageBoxW(None, str(msg), str(title), 0x10 | 0x10000)
        return
    except Exception:
        pass
    try:
        sys.stderr.write("%s\n%s\n" % (title, msg))
    except Exception:
        pass


def main(argv):
    # pythonw 启动时 stdout/stderr 都是 None，先兜住再谈别的
    try:
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass

    tool = (argv[1] if len(argv) > 1 else "toolbox").strip().lower()
    if tool not in VALID_TOOLS:
        fatal("启动台 · 参数有误",
              "未知的工具标识：%s\n\n可用：%s" % (tool, "、".join(VALID_TOOLS)))
        return 2

    ok, msg = ensure_bridge()
    if not ok:
        fatal("启动台 · 无法连接桥接服务", msg)
        return 1

    url = "%s/#/launch/%s" % (BASE, tool)
    title = TITLES.get(tool, "视频下载工具箱")
    ok, msg = open_window(url, title)
    if not ok:
        fatal("启动台 · 无法打开窗口", msg)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
