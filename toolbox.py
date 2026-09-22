# -*- coding: utf-8 -*-
"""
视频下载工具箱 — 统一菜单入口
================================
合并自三个原项目：
  1. 抖音高清下载器  → modules/douyin   （原 E:\\录屏\\dysp\\dyxz）
  2. 快手主页测试下载 → modules/kuaishou（原 E:\\录屏\\spxz）
  3. 程序下载        → modules/clawfetch（原工作区 DELIVERY/clawfetch，直链下载器）

只依赖 Python 3 标准库（tkinter），由 启动工具箱.bat 用工具箱自带运行时启动。
每个功能调用对应模块的启动脚本，模块之间互不依赖，可单独运行。
"""
import os
import subprocess
import sys
import threading

try:
    import tkinter as tk
    from tkinter import messagebox, scrolledtext
    TK_OK = True
except Exception:
    TK_OK = False

HERE = os.path.dirname(os.path.abspath(__file__))
MODULES = os.path.join(HERE, "modules")
RUNTIME = os.path.join(HERE, "runtime")


def _py(name):
    return os.path.join(RUNTIME, name, "Scripts", "python.exe")


def _pyw(name):
    return os.path.join(RUNTIME, name, "Scripts", "pythonw.exe")


def _open(path):
    try:
        os.startfile(path)
    except Exception as e:
        messagebox.showerror("错误", "无法打开:\n%s\n%s" % (path, e))


class Toolbox:
    def __init__(self, root):
        self.root = root
        root.title("视频下载工具箱")
        root.geometry("860x600")
        root.minsize(760, 500)

        tk.Label(root, text="视频下载工具箱", font=("Microsoft YaHei", 16, "bold")).pack(pady=(12, 2))
        tk.Label(root, text="抖音高清下载 · 快手主页下载 · 程序(直链)下载 — 三合一", font=("Microsoft YaHei", 9),
                 fg="#666").pack(pady=(0, 8))

        body = tk.Frame(root)
        body.pack(fill="both", expand=True, padx=10)
        # 左侧功能按钮区
        left = tk.Frame(body)
        left.pack(side="left", fill="y", padx=(0, 8))

        def btn(parent, text, cmd, tip=""):
            b = tk.Button(parent, text=text, font=("Microsoft YaHei", 10), anchor="w",
                          width=30, padx=12, pady=7, command=cmd)
            b.pack(fill="x", pady=3)
            if tip:
                _tip(b, tip)
            return b

        sep = lambda: tk.Frame(left, height=8).pack()

        tk.Label(left, text="— 抖音（模块 1）—", fg="#0a2540", font=("Microsoft YaHei", 9, "bold")).pack(anchor="w", pady=(2, 2))
        btn(left, "▶ 抖音下载器（交互命令行）", self.dy_cli, "粘贴抖音链接批量下载视频/图集；支持登录、增量、多模式")
        btn(left, "▶ 抖音 WebUI（浏览器界面）", self.dy_webui, "本地网页界面，查看库、发起下载（127.0.0.1:8518）")
        btn(left, "打开抖音下载目录", lambda: _open(os.path.join(HERE, "downloads", "douyin")))
        sep()
        tk.Label(left, text="— 快手（模块 2）—", fg="#0a2540", font=("Microsoft YaHei", 9, "bold")).pack(anchor="w", pady=(2, 2))
        btn(left, "▶ 快手一键下载器（图形窗口）", self.ks_gui, "弹窗粘贴快手分享链接，自动解析主页并批量下载")
        btn(left, "▶ 快手扫码登录（刷新登录态）", self.ks_login, "弹出浏览器扫码，刷新快手 Cookie 登录态")
        btn(left, "打开快手下载目录", lambda: _open(os.path.join(HERE, "data", "kuaishou")))
        sep()
        tk.Label(left, text="— 程序下载（模块 3）—", fg="#0a2540", font=("Microsoft YaHei", 9, "bold")).pack(anchor="w", pady=(2, 2))
        btn(left, "▶ 直链下载（输入链接）", self.cf_download, "clawfetch 直链下载器：断点续传/重试/SHA-256 校验")
        btn(left, "▶ 直链解析（只解析不下载）", self.cf_resolve, "把分享中转页解析成真实直链，可粘到 IDM/aria2")
        btn(left, "打开直链下载目录", lambda: _open(os.path.join(HERE, "downloads", "direct")))
        sep()
        btn(left, "📄 使用说明（必读）", self.open_docs, "打开使用说明文档目录")
        btn(left, "❌ 退出", root.destroy)

        # 右侧日志区
        right = tk.Frame(body)
        right.pack(side="left", fill="both", expand=True)
        tk.Label(right, text="运行日志：", anchor="w").pack(fill="x")
        self.log = scrolledtext.ScrolledText(right, font=("Consolas", 9), state="disabled")
        self.log.pack(fill="both", expand=True)

        self._check_env()

    # ---------- 日志 ----------
    def say(self, msg):
        def _append():
            self.log.config(state="normal")
            self.log.insert("end", msg + "\n")
            self.log.see("end")
            self.log.config(state="disabled")
        self.root.after(0, _append)

    def _check_env(self):
        ok1 = os.path.exists(_py("douyin-venv"))
        ok2 = os.path.exists(_py("kuaishou-env"))
        ok3 = os.path.exists(os.path.join(MODULES, "clawfetch", "clawfetch.py"))
        rows = [
            ("抖音运行时", ok1, _py("douyin-venv")),
            ("快手运行时", ok2, _py("kuaishou-env")),
            ("clawfetch 模块", ok3, os.path.join(MODULES, "clawfetch", "clawfetch.py")),
        ]
        self.say("── 环境自检 ──")
        for name, ok, path in rows:
            self.say("[%s] %s  %s" % ("OK " if ok else "缺失", name, path))
        self.say("")

    # ---------- 抖音 ----------
    def dy_cli(self):
        self._run_script(
            "抖音下载器",
            [_py("douyin-venv"), os.path.join(MODULES, "douyin", "launcher.py")],
            cwd=os.path.join(MODULES, "douyin"),
        )

    def dy_webui(self):
        self._run_script(
            "抖音 WebUI",
            [_py("douyin-venv"), os.path.join(MODULES, "douyin", "webui", "bridge_server.py")],
            cwd=os.path.join(MODULES, "douyin"),
            after=lambda: self._open_browser("http://127.0.0.1:8518"),
        )

    # ---------- 快手 ----------
    def ks_gui(self):
        pyw = _pyw("kuaishou-env")
        exe = pyw if os.path.exists(pyw) else _py("kuaishou-env")
        # GUI 程序：不进日志管道，直接新进程弹出窗口
        try:
            subprocess.Popen([exe, os.path.join(MODULES, "kuaishou", "tools", "快手一键下载器.pyw")],
                             cwd=os.path.join(MODULES, "kuaishou", "tools"))
            self.say("快手一键下载器窗口已启动（若被安全软件拦截请放行）")
        except Exception as e:
            messagebox.showerror("错误", "启动失败: %s" % e)

    def ks_login(self):
        self._run_script(
            "快手扫码登录",
            [_py("kuaishou-env"), "-u", os.path.join(MODULES, "kuaishou", "tools", "kuaishou_login.py"), "600"],
            cwd=os.path.join(MODULES, "kuaishou", "tools"),
        )

    # ---------- clawfetch ----------
    def _ask_str(self, title, label):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("680x150")
        win.transient(self.root)
        tk.Label(win, text=label, font=("Microsoft YaHei", 10), wraplength=640, justify="left").pack(pady=(14, 6), padx=14)
        e = tk.Entry(win, font=("Consolas", 10))
        e.pack(fill="x", padx=14)
        e.focus_set()
        result = {"v": None}

        def ok(event=None):
            result["v"] = e.get().strip()
            win.destroy()

        def cancel():
            win.destroy()

        bf = tk.Frame(win)
        bf.pack(pady=10)
        tk.Button(bf, text="确定", width=10, command=ok).pack(side="left", padx=6)
        tk.Button(bf, text="取消", width=10, command=cancel).pack(side="left", padx=6)
        e.bind("<Return>", ok)
        win.grab_set()
        self.root.wait_window(win)
        return result["v"]

    def cf_download(self):
        urls = self._ask_str("直链下载", "输入下载链接（多个链接用空格分隔）:")
        if not urls:
            return
        self._run_script(
            "直链下载",
            [sys.executable.replace("pythonw.exe", "python.exe") if sys.executable.endswith("pythonw.exe") else sys.executable,
             os.path.join(MODULES, "clawfetch", "clawfetch.py")] + urls.split(),
            cwd=os.path.join(MODULES, "clawfetch"),
            after=lambda: self.say("提示: 文件保存在 downloads\\direct（如用 -o 参数则按参数保存）"),
        )

    def cf_resolve(self):
        url = self._ask_str("直链解析", "输入要解析的链接（只显示真实直链与大小，不下载）:")
        if not url:
            return
        exe = sys.executable.replace("pythonw.exe", "python.exe") if sys.executable.endswith("pythonw.exe") else sys.executable
        self._run_script("直链解析", [exe, os.path.join(MODULES, "clawfetch", "clawfetch.py"), url, "--resolve"],
                         cwd=os.path.join(MODULES, "clawfetch"))

    # ---------- 通用执行 ----------
    def _run_script(self, title, cmd, cwd=None, after=None):
        self.say("── 启动 %s ──" % title)
        self.say("$ %s" % " ".join(str(c) for c in cmd))
        window_title = "TOOLBOX:%s" % title

        def worker():
            try:
                creation = 0
                if os.name == "nt":
                    creation = subprocess.CREATE_NEW_CONSOLE
                proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8", errors="replace",
                                        creationflags=creation)
                for line in proc.stdout:
                    self.say(line.rstrip())
                proc.wait()
                self.say("── %s 退出（码 %d）──" % (title, proc.returncode))
            except Exception as e:
                self.say("[错误] %s 启动失败: %s" % (title, e))
            if after:
                after()
        threading.Thread(target=worker, daemon=True).start()

    def _open_browser(self, url):
        import webbrowser
        threading.Timer(2.5, lambda: webbrowser.open(url)).start()

    def open_docs(self):
        _open(os.path.join(HERE, "docs"))


def console_menu():
    """无 tkinter 环境的 CLI 兑底菜单"""
    here = os.path.dirname(os.path.abspath(__file__))
    actions = {
        "1": ("抖音下载器（命令行）", lambda: subprocess.call([
            os.path.join(here, "runtime", "douyin-venv", "Scripts", "python.exe"),
            os.path.join(here, "modules", "douyin", "launcher.py")], cwd=os.path.join(here, "modules", "douyin"))),
        "2": ("快手一键下载器（GUI）", lambda: subprocess.call([
            os.path.join(here, "runtime", "kuaishou-env", "Scripts", "pythonw.exe"),
            os.path.join(here, "modules", "kuaishou", "tools", "快手一键下载器.pyw")], cwd=os.path.join(here, "modules", "kuaishou", "tools"))),
        "3": ("快手扫码登录", lambda: subprocess.call([
            os.path.join(here, "runtime", "kuaishou-env", "Scripts", "python.exe"), "-u",
            os.path.join(here, "modules", "kuaishou", "tools", "kuaishou_login.py"), "600"], cwd=os.path.join(here, "modules", "kuaishou", "tools"))),
        "4": ("程序下载（直链）", _cli_direct),
        "5": ("打开使用说明", lambda: os.startfile(os.path.join(here, "docs"))),
        "0": ("退出", lambda: sys.exit(0)),
    }
    while True:
        print("\n===== 视频下载工具箱 =====")
        for k, (name, _) in actions.items():
            print("  %s. %s" % (k, name))
        choice = input("选择: ").strip()
        if choice in actions:
            actions[choice][1]()


def _cli_direct():
    here = os.path.dirname(os.path.abspath(__file__))
    url = input("输入下载链接: ").strip()
    if url:
        subprocess.call([sys.executable, os.path.join(here, "modules", "clawfetch", "clawfetch.py"),
                         url, "-o", os.path.join(here, "downloads", "direct")])


def _tip(widget, text):
    def enter(_):
        t = tk.Toplevel(widget)
        t.wm_overrideredirect(True)
        t.wm_geometry("+%d+%d" % (widget.winfo_rootx() + widget.winfo_width() + 8, widget.winfo_rooty() + 4))
        tk.Label(t, text=text, bg="#ffffe0", relief="solid", borderwidth=1,
                 font=("Microsoft YaHei", 9), justify="left", wraplength=380).pack()
        widget._tipwin = t

    def leave(_):
        t = getattr(widget, "_tipwin", None)
        if t:
            t.destroy()

    widget.bind("<Enter>", enter)
    widget.bind("<Leave>", leave)


def main():
    # 控制台 pythonw 场景下 stdout 可能为 None
    try:
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass
    if TK_OK:
        root = tk.Tk()
        Toolbox(root)
        root.mainloop()
    else:
        console_menu()


if __name__ == "__main__":
    main()
