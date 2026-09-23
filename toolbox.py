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
import re
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


# 启动子进程时统一隐藏控制台窗口。
# 工具箱自身以 pythonw 无窗口方式运行，如果子进程不隐藏窗口，桌面上就会冒出黑窗口。
# 起因：_run_script 曾同时使用 stdout=PIPE 与 CREATE_NEW_CONSOLE，输出去向是界面日志窗格，
# 却还留下一个空白控制台；且那个窗口被误关时会连带杀掉子进程（CTRL_CLOSE_EVENT）。
# 已逐个核实被启动的模块都不读 stdin，隐藏控制台不影响功能。
NOWINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _open(path):
    try:
        os.startfile(path)
    except Exception as e:
        messagebox.showerror("错误", "无法打开:\n%s\n%s" % (path, e))


class Toolbox:
    # 「明亮商务风」主题（与 WebUI tokens.css 同源）
    C_BG = "#FAF9F6"        # 暖白底
    C_CARD = "#FFFFFF"      # 卡片白
    C_PRIMARY = "#0D9488"   # 青碧主色
    C_PRIMARY_DARK = "#0F766E"
    C_PRIMARY_LIGHT = "#F0FDFA"
    C_TEXT = "#334155"
    C_HEADING = "#1E293B"
    C_MUTED = "#64748B"
    C_BORDER = "#E2E8F0"

    def __init__(self, root):
        self.root = root
        root.title("视频下载工具箱")
        root.geometry("940x640")
        root.minsize(860, 520)
        root.configure(bg=self.C_BG)

        tk.Label(root, text="视频下载工具箱", font=("Microsoft YaHei", 16, "bold"),
                 bg=self.C_BG, fg=self.C_HEADING).pack(pady=(12, 2))
        tk.Label(root, text="抖音高清下载 · 快手主页下载 · 程序(直链)下载 · 直播自动录制 — 四合一",
                 font=("Microsoft YaHei", 9),
                 bg=self.C_BG, fg=self.C_MUTED).pack(pady=(0, 8))

        body = tk.Frame(root, bg=self.C_BG)
        body.pack(fill="both", expand=True, padx=10)

        # 左侧功能区 —— 模块按两列网格排布。
        # 为什么不是原来的单列：4 个模块共 14 个功能按钮，单列排下来约 790px 高，
        # 在 760 高的窗口里必须滚动才能看到最后一个模块（已用截图确认），
        # 新加入的模块会正好落在折叠线以下、用户根本发现不了。
        # 两列网格把高度压到约 380px —— 四个模块一屏全部可见。
        # 外层仍套一层可滚动画布，作为「以后继续加模块」的兜底。
        # 「使用说明 / 退出」是全局按钮，钉在底部，永远不需要滚动才能点到。
        left_holder = tk.Frame(body, bg=self.C_BG)
        left_holder.pack(side="left", fill="y", padx=(0, 8))

        footer = tk.Frame(left_holder, bg=self.C_BG)
        footer.pack(side="bottom", fill="x", pady=(6, 0))

        scroll_area = tk.Frame(left_holder, bg=self.C_BG)
        scroll_area.pack(side="top", fill="both", expand=True)
        left_canvas = tk.Canvas(scroll_area, bg=self.C_BG, highlightthickness=0,
                                bd=0, width=556)
        left_bar = tk.Scrollbar(scroll_area, orient="vertical", command=left_canvas.yview)
        left_canvas.configure(yscrollcommand=left_bar.set)
        left_canvas.pack(side="left", fill="both", expand=True)
        left = tk.Frame(left_canvas, bg=self.C_BG)
        _left_win = left_canvas.create_window((0, 0), window=left, anchor="nw")

        def _sync_scroll(_e=None):
            """内容放得下就把滚动条收起来，放不下才显示（避免一条没用的空滚动条）。

            只在「该显示」和「该隐藏」两种状态之间切换，不做无谓的重复 pack，
            所以不会因 canvas 变窄又触发的 Configure 而反复抖动。
            """
            box = left_canvas.bbox("all")
            left_canvas.configure(scrollregion=box)
            need = bool(box) and (box[3] - box[1]) > left_canvas.winfo_height()
            if need:
                if not left_bar.winfo_ismapped():
                    # 必须 before=left_canvas：滚动条得先于画布参与布局分配，
                    # 否则画布 expand=True 会先吃掉全部宽度，把滚动条挤成 0 宽。
                    left_bar.pack(side="right", fill="y", before=left_canvas)
            elif left_bar.winfo_ismapped():
                left_bar.pack_forget()

        left.bind("<Configure>", _sync_scroll)
        left_canvas.bind("<Configure>",
                         lambda e: left_canvas.itemconfigure(_left_win, width=e.width))
        left_canvas.bind("<Configure>", _sync_scroll, add="+")

        col_a = tk.Frame(left, bg=self.C_BG)
        col_b = tk.Frame(left, bg=self.C_BG)
        col_a.grid(row=0, column=0, sticky="nw", padx=(0, 12))
        col_b.grid(row=0, column=1, sticky="nw")

        def _wheel(event):
            """只在指针位于按钮区时吞掉滚轮事件，避免影响右侧日志滚动。"""
            w = root.winfo_containing(event.x_root, event.y_root)
            while w is not None:
                if w in (left_canvas, left):
                    left_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
                    return
                w = getattr(w, "master", None)
        root.bind_all("<MouseWheel>", _wheel)

        def btn(parent, text, cmd, tip=""):
            b = tk.Button(parent, text=text, font=("Microsoft YaHei", 10), anchor="w",
                          width=30, padx=12, pady=6, command=cmd,
                          bg=self.C_CARD, fg=self.C_TEXT, activebackground=self.C_PRIMARY_LIGHT,
                          activeforeground=self.C_PRIMARY_DARK, relief="flat", cursor="hand2",
                          highlightthickness=1, highlightbackground=self.C_BORDER)
            b.pack(fill="x", pady=2)
            if tip:
                _tip(b, tip)
            return b

        def block(parent, title):
            """一个模块区块 = 小标题 + 该模块的按钮，便于按列排布。"""
            box = tk.Frame(parent, bg=self.C_BG)
            box.pack(fill="x", pady=(0, 10))
            tk.Label(box, text=title, fg=self.C_PRIMARY_DARK, bg=self.C_BG,
                     font=("Microsoft YaHei", 9, "bold")).pack(anchor="w", pady=(2, 3))
            return box

        # ---------------- 左列：模块 1 / 模块 3 ----------------
        m1 = block(col_a, "— 抖音（模块 1）—")
        btn(m1, "▶ 抖音下载器（交互命令行）", self.dy_cli, "粘贴抖音链接批量下载视频/图集；支持登录、增量、多模式")
        btn(m1, "▶ 抖音 WebUI（浏览器界面）", self.dy_webui, "本地网页界面，查看库、发起下载（127.0.0.1:8518）")
        btn(m1, "打开抖音下载目录", lambda: _open(os.path.join(HERE, "downloads", "douyin")))

        m3 = block(col_a, "— 程序下载（模块 3）—")
        btn(m3, "▶ 直链下载（输入链接）", self.cf_download, "clawfetch 直链下载器：断点续传/重试/SHA-256 校验")
        btn(m3, "▶ 直链解析（只解析不下载）", self.cf_resolve, "把分享中转页解析成真实直链，可粘到 IDM/aria2")
        btn(m3, "打开直链下载目录", lambda: _open(os.path.join(HERE, "downloads", "direct")))

        # ---------------- 右列：模块 2 / 模块 4 ----------------
        m2 = block(col_b, "— 快手（模块 2）—")
        btn(m2, "▶ 快手一键下载器（图形窗口）", self.ks_gui, "弹窗粘贴快手分享链接，自动解析主页并批量下载")
        btn(m2, "▶ 快手扫码登录（刷新登录态）", self.ks_login, "弹出浏览器扫码，刷新快手 Cookie 登录态")
        btn(m2, "打开快手下载目录", lambda: _open(os.path.join(HERE, "data", "kuaishou")))

        m4 = block(col_b, "— 直播录制（模块 4）—")
        btn(m4, "▶ 直播录制器（图形窗口）", self.lr_gui, "监控快手/抖音直播间，开播自动录制；断流重连、分卷、无损合并")
        btn(m4, "▶ 直播扫码登录（快手/抖音）", self.lr_login, "刷新平台登录态；快手未登录时只能监控推荐流覆盖的房间")
        btn(m4, "🔍 直播录制环境自检", self.lr_selftest, "检查依赖/ffmpeg/配置/解析层；录制异常时先跑这个")
        btn(m4, "打开录像目录", self.lr_open_dir, "打开录制文件根目录（按 平台/昵称/日期 三级分类）")

        btn(footer, "📄 使用说明（必读）", self.open_docs, "打开使用说明文档目录")
        btn(footer, "❌ 退出", root.destroy)

        # 右侧日志区
        right = tk.Frame(body, bg=self.C_BG)
        right.pack(side="left", fill="both", expand=True)
        tk.Label(right, text="运行日志：", anchor="w", bg=self.C_BG, fg=self.C_MUTED).pack(fill="x")
        self.log = scrolledtext.ScrolledText(right, font=("Consolas", 9), state="disabled",
                                             bg=self.C_CARD, fg=self.C_TEXT, relief="flat")
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
        ok4 = os.path.exists(_py("live-recorder-env"))
        ok5 = os.path.exists(os.path.join(MODULES, "live_recorder", "main.py"))
        rows = [
            ("抖音运行时", ok1, _py("douyin-venv")),
            ("快手运行时", ok2, _py("kuaishou-env")),
            ("clawfetch 模块", ok3, os.path.join(MODULES, "clawfetch", "clawfetch.py")),
            ("直播录制运行时", ok4, _py("live-recorder-env")),
            ("直播录制模块", ok5, os.path.join(MODULES, "live_recorder", "main.py")),
        ]
        self.say("── 环境自检 ──")
        for name, ok, path in rows:
            self.say("[%s] %s  %s" % ("OK " if ok else "缺失", name, path))
        self.say("")

    # ---------- 抖音 ----------
    def dy_cli(self):
        """抖音下载器：先在界面里收链接，再用无窗口方式跑。

        为什么不直接把 launcher.py 的交互循环放起来：那个循环靠 input() 读终端，
        而工具箱现在是无控制台启动（pythonw），子进程又用了 CREATE_NO_WINDOW，
        隐藏控制台之后 input() 会立刻 EOF —— 用户根本没法粘贴链接。
        好在 launcher.py 同样接受把链接当命令行参数传（内部走同一个 extract_urls），
        所以改成「界面收文本 → 带参数启动」：既不弹终端，也不丢功能，
        而且一次可以粘多个链接，比原来「提示在界面、输入在黑窗口」更顺。
        """
        text = self._ask_text(
            "抖音下载器",
            "粘贴抖音链接或分享口令（可一次粘多个，用空格或换行分隔）：\n"
            "多行长文本也没关系，程序会自动挑出里面的 http(s) 链接。")
        if not text:
            return
        if not re.search(r"https?://", text, re.IGNORECASE):
            messagebox.showwarning(
                "未识别到链接",
                "这段文本里没有找到 http(s) 链接。\n请确认复制的是完整分享文本或视频地址。")
            return
        # 压成单行再传参：Windows 命令行参数里带换行容易踩坑，
        # 而 launcher.py 的 extract_urls 按 \s 切分，空格与换行等价。
        one_line = re.sub(r"\s+", " ", text).strip()
        self._run_script(
            "抖音下载器",
            [_py("douyin-venv"), os.path.join(MODULES, "douyin", "launcher.py"), one_line],
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
                             cwd=os.path.join(MODULES, "kuaishou", "tools"),
                             creationflags=NOWINDOW)
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
    def _ask_choice(self, title, label, options):
        """单选题小窗口。options 为 [(按钮文字, 返回值)]，关闭窗口视为取消（None）。"""
        win = tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        win.resizable(False, False)
        tk.Label(win, text=label, font=("Microsoft YaHei", 10), wraplength=460,
                 justify="left", bg=self.C_BG, fg=self.C_TEXT).pack(pady=(16, 12), padx=16)
        result = {"v": None}

        def pick(value):
            result["v"] = value
            win.destroy()

        bf = tk.Frame(win, bg=self.C_BG)
        bf.pack(pady=(0, 16), padx=16)
        for text, value in options:
            tk.Button(bf, text=text, width=14, font=("Microsoft YaHei", 10),
                      bg=self.C_CARD, fg=self.C_TEXT, activebackground=self.C_PRIMARY_LIGHT,
                      activeforeground=self.C_PRIMARY_DARK, relief="flat", cursor="hand2",
                      highlightthickness=1, highlightbackground=self.C_BORDER,
                      command=lambda v=value: pick(v)).pack(side="left", padx=5)
        win.grab_set()
        self.root.wait_window(win)
        return result["v"]

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

    def _ask_text(self, title, label, height=6):
        """多行文本输入框。关闭窗口/点取消返回 None。

        比 _ask_str 多的就是「多行」：抖音分享口令经常是好几行的，
        单行 Entry 粘进去会把换行挤掉、内容粘连，导致链接识别失败。
        这里用 Text，并改用 Ctrl+Enter 提交（避免多行输入时按 Enter 就误提交）。
        """
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("680x300")
        win.transient(self.root)
        tk.Label(win, text=label, font=("Microsoft YaHei", 10), wraplength=640,
                 justify="left").pack(pady=(14, 6), padx=14)
        box = tk.Text(win, font=("Consolas", 10), height=height, wrap="word",
                      undo=True)
        box.pack(fill="both", expand=True, padx=14)
        box.focus_set()
        result = {"v": None}

        def ok(event=None):
            result["v"] = box.get("1.0", "end").strip()
            win.destroy()

        def cancel():
            win.destroy()

        bf = tk.Frame(win)
        bf.pack(pady=10)
        tk.Button(bf, text="确定 (Ctrl+Enter)", width=18, command=ok).pack(side="left", padx=6)
        tk.Button(bf, text="取消", width=10, command=cancel).pack(side="left", padx=6)
        box.bind("<Control-Return>", ok)
        win.bind("<Escape>", lambda _e: cancel())
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

    # ---------- 直播录制（模块 4） ----------
    def lr_dir(self):
        return os.path.join(MODULES, "live_recorder")

    def lr_save_dir(self):
        """读取模块配置里的录像根目录；读不到就回落到 ~/LiveRecorder。

        不写死路径的原因：录像目录是用户在「设置」里可改的一项，
        按钮必须跟着用户的实际设置走，否则会打开一个空目录。
        """
        cfg = os.path.join(self.lr_dir(), "config.json")
        try:
            import json
            with open(cfg, "r", encoding="utf-8") as fh:
                path = (json.load(fh).get("settings") or {}).get("save_path")
            if path:
                return os.path.expandvars(os.path.expanduser(str(path)))
        except Exception:
            pass
        return os.path.join(os.path.expanduser("~"), "LiveRecorder")

    def lr_gui(self):
        pyw = _pyw("live-recorder-env")
        exe = pyw if os.path.exists(pyw) else _py("live-recorder-env")
        if not os.path.exists(exe):
            messagebox.showerror("错误", "缺少直播录制运行时：\n%s" % exe)
            return
        # GUI 程序：不进日志管道，直接新进程弹出窗口（同快手一键下载器）
        try:
            subprocess.Popen([exe, os.path.join(self.lr_dir(), "main.py")],
                             cwd=self.lr_dir(), creationflags=NOWINDOW)
            self.say("直播录制器窗口已启动（若被安全软件拦截请放行）")
            self.say("提示: 录像保存在 %s" % self.lr_save_dir())
        except Exception as e:
            messagebox.showerror("错误", "启动失败: %s" % e)

    def lr_login(self):
        platform = self._ask_choice(
            "直播扫码登录",
            "选择要登录的平台：\n"
            "快手对未登录访客关闭了「按房间查询」接口，必须登录后才能监控任意指定主播；\n"
            "抖音通常无需登录，登录可进一步降低风控概率。",
            [("快手", "kuaishou"), ("抖音", "douyin"), ("两个都登录", "")],
        )
        if platform is None:
            return
        cmd = [_py("live-recorder-env"), "-u", os.path.join(self.lr_dir(), "login.py")]
        if platform:
            cmd.append(platform)
        self._run_script("直播扫码登录", cmd, cwd=self.lr_dir())

    def lr_selftest(self):
        self._run_script(
            "直播录制环境自检",
            [_py("live-recorder-env"), os.path.join(self.lr_dir(), "main.py"), "--selftest"],
            cwd=self.lr_dir(),
        )

    def lr_open_dir(self):
        target = self.lr_save_dir()
        if not os.path.isdir(target):
            self.say("录像目录还不存在（尚未录制过）：%s" % target)
            try:
                os.makedirs(target, exist_ok=True)
                self.say("已创建该目录。")
            except Exception as e:
                messagebox.showerror("错误", "无法创建目录:\n%s\n%s" % (target, e))
                return
        _open(target)

    # ---------- 通用执行 ----------
    def _run_script(self, title, cmd, cwd=None, after=None):
        self.say("── 启动 %s ──" % title)
        self.say("$ %s" % " ".join(str(c) for c in cmd))
        window_title = "TOOLBOX:%s" % title

        def worker():
            try:
                proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8", errors="replace",
                                        creationflags=NOWINDOW)
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
        "5": ("直播录制器（图形窗口）", lambda: subprocess.Popen([
            os.path.join(here, "runtime", "live-recorder-env", "Scripts", "pythonw.exe"),
            os.path.join(here, "modules", "live_recorder", "main.py")],
            cwd=os.path.join(here, "modules", "live_recorder"))),
        "6": ("直播扫码登录（快手/抖音）", lambda: subprocess.call([
            os.path.join(here, "runtime", "live-recorder-env", "Scripts", "python.exe"), "-u",
            os.path.join(here, "modules", "live_recorder", "login.py")],
            cwd=os.path.join(here, "modules", "live_recorder"))),
        "7": ("打开使用说明", lambda: os.startfile(os.path.join(here, "docs"))),
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
        tk.Label(t, text=text, bg="#F0FDFA", fg="#0F766E", relief="solid", borderwidth=1,
                 font=("Microsoft YaHei", 9), justify="left", wraplength=380).pack()
        widget._tipwin = t

    def leave(_):
        t = getattr(widget, "_tipwin", None)
        if t:
            t.destroy()

    widget.bind("<Enter>", enter)
    widget.bind("<Leave>", leave)


def _fatal(title, msg):
    """无控制台环境下的最后一道兜底：弹原生对话框，避免进程静默消失。

    以 pythonw.exe 启动时没有控制台，异常信息无处可去，用户只会看到
    「双击了但什么都没发生」。这里直接调 user32 的 MessageBoxW 弹窗，
    刻意不依赖 tkinter —— 因为 tkinter 本身很可能就是失败的原因。
    """
    text = str(msg)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # 0x10 = MB_ICONERROR
        return
    except Exception:
        pass
    try:
        sys.stderr.write("%s\n%s\n" % (title, text))
    except Exception:
        pass


def main():
    # 以 pythonw.exe（无控制台）启动时 sys.stdin / stdout / stderr 全部为 None。
    # 必须在替换 stdout 之前先判断，否则之后分不清「真的没有控制台」还是「已被重定向」。
    headless = sys.stdin is None and sys.stdout is None
    try:
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass

    # 无控制台时任何未捕获异常都会让进程无声退出，统一转成弹窗。
    def _hook(etype, evalue, etb):
        import traceback
        try:
            detail = "".join(traceback.format_exception(etype, evalue, etb))
        except Exception:
            detail = repr(evalue)
        _fatal("视频下载工具箱 · 运行出错", detail)

    sys.excepthook = _hook

    if TK_OK:
        try:
            root = tk.Tk()
        except Exception as e:
            _fatal("视频下载工具箱 · 无法创建窗口",
                   "%s\n\n可能是桌面会话或 tkinter 组件异常。\n"
                   "可改用 启动工具箱.bat 启动以查看完整报错。" % e)
            return
        # tkinter 回调里抛出的异常不会走 sys.excepthook，而是走
        # Tk.report_callback_exception（默认只会打印到 stderr，pythonw 下等于丢弃）。
        # 不接管这一处，点按钮出错时用户仍然什么都看不到。
        root.report_callback_exception = _hook
        Toolbox(root)
        root.mainloop()
    elif headless:
        # 没有 tkinter 又没有控制台：CLI 兜底菜单连输入都读不了，只能明确报错
        _fatal("视频下载工具箱 · 缺少 tkinter",
               "当前 Python 运行时缺少 tkinter 模块，且程序以无控制台方式启动，"
               "无法回退到命令行菜单。\n\n请改用 启动工具箱.bat 双击启动以查看详细报错。")
    else:
        console_menu()


if __name__ == "__main__":
    main()
